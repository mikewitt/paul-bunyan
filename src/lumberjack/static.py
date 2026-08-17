"""Static structure extraction: what the source says, before anything is inferred.

The runtime model reconstructs loop structure from *timing* — periods, ratios
between them, scoped to one worker. The source code already states that
structure exactly, and a short `ast` walk recovers it: which call sites share a
loop body, which loops nest inside which, what order a body runs in, and what
template each call site emits. All of it keyed on `file:lineno`, which is
already the identity axis (`schema.SourceKey`), so nothing has to be reconciled
with anything.

This module only *reads source*. It never imports, executes or evaluates the
file it analyses, and it holds no opinion about what a display should do with
the answer — it reports structure, and the caller decides.

What it cannot see, and why each silence is the correct failure:

- **Anything without source on disk.** `exec`'d code, generated modules, a
  frozen or zipped importer. `analyze_file()` returns None and the caller falls
  back to runtime inference alone, which is what ships today.
- **Cross-function containment.** A loop calling a function that logs is not
  lexical containment; recovering it needs a call graph, and dynamic dispatch
  defeats that. Saying nothing is right — period ordering *fabricates* this
  relationship (the `phases` false parent in `examples/demo.py`), and a display
  asserting something untrue is worse than one asserting less.
- **Log calls it does not recognise** — see `_MESSAGE_ARG` for the exact rule
  and the false positive it accepts in exchange.
- **Comprehensions and generator expressions**, which are modelled neither as
  loops nor as scopes: a call site inside one is attributed to the enclosing
  function and to the enclosing loops.
- **`break`, `continue` and `return`**, which make a body's textual order an
  upper bound rather than a promise.

And the thing it must never be trusted about: a file that has changed since the
running process imported it. Line numbers move, and `file:lineno` is exact and
therefore brittle. `template_matches()` is the guard, and every consumer owes
it a call before using any of this.
"""

from __future__ import annotations

import ast
import dataclasses
import os
from collections import Counter
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal, NamedTuple

#: Method name -> index of the message argument. `.log()` takes the level
#: first, everything else takes the message first.
#:
#: Deliberately conservative and deliberately *receiver-blind*: `log.debug(…)`,
#: `self._logger.info(…)` and `logging.warning(…)` are all the same shape, and
#: no heuristic on the receiver's name separates a logger from anything else
#: without also missing real call sites. The cost is a false positive —
#: `parser.error("no such file")` is indistinguishable from a log call and
#: takes a slot in its body's ordering. That is a cosmetic miss in a display
#: (Principle 10) where a missed call site would silently corrupt the ordinal
#: position of every sibling after it. The linter weighs it the other way —
#: lumberjack: see issue #61.
#:
#: Invisible on purpose: a bare `debug("x")` imported straight from `logging`,
#: the deprecated `warn`/`fatal` aliases, and any call whose message argument
#: is absent (`log.debug()`, or `log.debug(msg="x")` passed by keyword).
_MESSAGE_ARG: Final[Mapping[str, int]] = MappingProxyType(
    {
        "debug": 0,
        "info": 0,
        "warning": 0,
        "error": 0,
        "critical": 0,
        "exception": 0,
        "log": 1,
    }
)

LoopKind = Literal["for", "async for", "while"]

_LOOP_KINDS: Final[Mapping[type[ast.AST], LoopKind]] = MappingProxyType(
    {
        ast.For: "for",
        ast.AsyncFor: "async for",
        ast.While: "while",
    }
)

#: What `LogRecord.funcName` reads at module level, so this module agrees.
MODULE_SCOPE: Final = "<module>"


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class CallSite:
    """One recognised logging call, as the source describes it.

    `lineno` is what a `LogRecord` from this call will carry, and `func_name`
    is what its `funcName` will read — together with `pathname` they are a
    `schema.SourceKey`, which is why nothing here needs reconciling.
    """

    lineno: int
    func_name: str
    #: The method called: `debug`, `info`, `log`, … . Not the level a record
    #: ends up with — `.log()` takes that as an argument.
    method: str
    #: The literal message, which under lazy %-formatting is what
    #: `record.msg` holds. None when the first argument is not a string
    #: literal — an f-string, a variable, a concatenation — in which case the
    #: template does not survive to runtime either and there is nothing to
    #: compare against.
    template: str | None
    #: Enclosing loops, outermost first, as their `lineno`s. Empty when the
    #: call is not inside a loop. Resets at every function, lambda and class
    #: boundary: a closure defined in a loop body does not run per iteration.
    loop_chain: tuple[int, ...]
    #: The call sits under an `if`, `try` or `match` *within its innermost
    #: loop body*, so it may not fire on every iteration. A consumer ordering
    #: a body should treat that body as having no stable order.
    conditional: bool
    #: 1-based ordinal within the innermost loop body, in textual order, and
    #: the body's size — `position` of `body_size`. Both None exactly when
    #: `loop_chain` is empty, because a body is a loop body here: a
    #: straight-line function body has a textual order too, and extending
    #: this to it is lumberjack: see issue #62.
    position: int | None
    body_size: int | None

    @property
    def loop_lineno(self) -> int | None:
        """The innermost enclosing loop, or None outside any loop."""
        return self.loop_chain[-1] if self.loop_chain else None

    @property
    def depth(self) -> int:
        """Loop nesting depth within the enclosing scope; 0 outside a loop."""
        return len(self.loop_chain)


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Loop:
    """A `for`/`while`/`async for` statement and the call sites in its body.

    Every loop in the file is reported, including one that logs nothing —
    which is not an oversight but the linter's (#40) most useful finding: a
    loop with no call sites is a loop the display cannot see at all.
    """

    lineno: int
    kind: LoopKind
    func_name: str
    #: 1-based nesting depth within the enclosing scope.
    depth: int
    #: The enclosing loop's `lineno`, or None at depth 1.
    parent: int | None
    #: Call sites *directly* in this body — a nested loop's own sites belong
    #: to that loop, not to this one — in textual order.
    call_sites: tuple[CallSite, ...] = ()

    @property
    def stable_order(self) -> bool:
        """Whether ordinal position within this body means anything.

        False as soon as one call site is conditional: the body then emits a
        different sequence depending on which branch runs, and a determinate
        sub-iteration bar built on it would show a wrong percentage rather
        than an imprecise one.
        """
        return all(not site.conditional for site in self.call_sites)


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class FileStructure:
    """Everything one file's source says about its logging, keyed by line."""

    pathname: str
    #: Recognised call sites by `lineno`, in source order.
    call_sites: Mapping[int, CallSite]
    #: Every loop in the file by `lineno`, in source order.
    loops: Mapping[int, Loop]


# --------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------


class _LogCall(NamedTuple):
    """A `Call` node recognised as logging, reduced to what matters."""

    lineno: int
    method: str
    message: ast.expr


class _Ctx(NamedTuple):
    """Lexical context threaded down the walk."""

    func_name: str
    loops: tuple[int, ...]
    conditional: bool


class _Found(NamedTuple):
    log_call: _LogCall
    ctx: _Ctx


_MODULE_CTX: Final = _Ctx(func_name=MODULE_SCOPE, loops=(), conditional=False)


def _log_call(call: ast.Call) -> _LogCall | None:
    """Recognise a logging call, or None. See `_MESSAGE_ARG` for the rule."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    index = _MESSAGE_ARG.get(func.attr)
    if index is None or len(call.args) <= index:
        return None
    # The line the *attribute* ends on, not `Call.lineno`, which is where the
    # whole expression starts. They differ whenever the receiver spans lines
    # (`log\n.debug(…)`, `get_logger(\n).debug(…)`, a backslash continuation),
    # and it is the attribute's line that a LogRecord carries — verified
    # against CPython 3.12, 3.13 and 3.14, which agree. `end_lineno` is
    # Optional only for hand-built nodes; parsed source always sets it.
    return _LogCall(
        lineno=func.end_lineno or func.lineno,
        method=func.attr,
        message=call.args[index],
    )


def _template(message: ast.expr) -> str | None:
    """The literal template, or None when the call site does not have one."""
    if isinstance(message, ast.Constant) and isinstance(message.value, str):
        return message.value
    return None


class _Walker:
    """Depth-first, source-order collection of call sites and loops.

    Source order matters and is free: two call sites are visited in the order
    they appear in the file whatever their nesting, so grouping the results by
    innermost loop afterwards yields each body already in textual order.
    """

    def __init__(self) -> None:
        self.found: list[_Found] = []
        self.loops: dict[int, Loop] = {}

    def visit(self, node: ast.AST, ctx: _Ctx) -> None:
        if isinstance(node, ast.Call):
            log_call = _log_call(node)
            if log_call is not None:
                self.found.append(_Found(log_call=log_call, ctx=ctx))
            # Keep descending: an argument can be another call.
        elif isinstance(node, ast.For | ast.AsyncFor | ast.While):
            self._visit_loop(node, ctx)
            return
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            # A scope boundary resets the loop chain: a closure defined inside
            # a loop body is not called once per iteration, and `funcName` on
            # its records reads the inner name, not the enclosing one.
            ctx = _Ctx(func_name=node.name, loops=(), conditional=False)
        elif isinstance(node, ast.Lambda):
            ctx = _Ctx(func_name="<lambda>", loops=(), conditional=False)
        elif isinstance(node, ast.If | ast.Try | ast.TryStar | ast.Match):
            ctx = ctx._replace(conditional=True)

        for child in ast.iter_child_nodes(node):
            self.visit(child, ctx)

    def _visit_loop(self, node: ast.For | ast.AsyncFor | ast.While, ctx: _Ctx) -> None:
        inner = (*ctx.loops, node.lineno)
        self.loops[node.lineno] = Loop(
            lineno=node.lineno,
            kind=_LOOP_KINDS[type(node)],
            func_name=ctx.func_name,
            depth=len(inner),
            parent=ctx.loops[-1] if ctx.loops else None,
        )
        # Only `body` repeats. The iterable is evaluated once before the loop
        # starts, the `while` condition is the loop's own bookkeeping, and a
        # `for … else` clause runs once after it — so those children keep the
        # enclosing context. `conditional` resets for the body because the
        # flag describes a branch *inside this body*; a loop nested under an
        # `if` is itself conditional, but its own body is not.
        body = {id(stmt) for stmt in node.body}
        body_ctx = _Ctx(func_name=ctx.func_name, loops=inner, conditional=False)
        for child in ast.iter_child_nodes(node):
            self.visit(child, body_ctx if id(child) in body else ctx)


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def _finalize(pathname: str, walker: _Walker) -> FileStructure:
    # Two log calls on one line (`log.debug("a"); log.debug("b")`) produce
    # records that are indistinguishable from each other, so neither can be
    # identified and both are dropped. Refusing an ambiguous site is the same
    # instinct as the drift guard: the display may claim less, never wrong.
    per_line = Counter(item.log_call.lineno for item in walker.found)
    usable = [item for item in walker.found if per_line[item.log_call.lineno] == 1]

    bodies: dict[int | None, list[_Found]] = {}
    for item in usable:
        innermost = item.ctx.loops[-1] if item.ctx.loops else None
        bodies.setdefault(innermost, []).append(item)

    sites: dict[int, CallSite] = {}
    by_loop: dict[int, list[CallSite]] = {}
    for innermost, body in bodies.items():
        for position, item in enumerate(body, start=1):
            site = CallSite(
                lineno=item.log_call.lineno,
                func_name=item.ctx.func_name,
                method=item.log_call.method,
                template=_template(item.log_call.message),
                loop_chain=item.ctx.loops,
                conditional=item.ctx.conditional,
                position=position if innermost is not None else None,
                body_size=len(body) if innermost is not None else None,
            )
            sites[site.lineno] = site
            if innermost is not None:
                by_loop.setdefault(innermost, []).append(site)

    loops = {
        lineno: dataclasses.replace(loop, call_sites=tuple(by_loop.get(lineno, ())))
        for lineno, loop in sorted(walker.loops.items())
    }
    return FileStructure(
        pathname=pathname,
        # Grouping scrambled source order; sorting puts it back, and both maps
        # are proxied because the result is cached and shared between callers.
        call_sites=MappingProxyType(dict(sorted(sites.items()))),
        loops=MappingProxyType(loops),
    )


def _parse(pathname: str) -> FileStructure | None:
    try:
        with open(pathname, "rb") as handle:
            source = handle.read()
        # Bytes rather than text: `ast.parse` then honours a PEP 263 encoding
        # declaration and a BOM exactly as the import machinery did, where
        # reading text would guess the locale's encoding and mangle a
        # non-UTF-8 template — or raise on one.
        tree = ast.parse(source, filename=pathname)
        walker = _Walker()
        walker.visit(tree, _MODULE_CTX)
    except (OSError, SyntaxError, ValueError, RecursionError):
        # Missing, unreadable, a directory, not valid Python, containing null
        # bytes, or nested deeply enough to exhaust the stack. Every one of
        # them means "no structure available", which callers already handle.
        return None
    # Deliberately outside the guard: assembly is bookkeeping over data the
    # parse already validated, so a failure there is this module's bug and
    # should be seen rather than swallowed as a missing file.
    return _finalize(pathname, walker)


# --------------------------------------------------------------------------
# Cache and public entry points
# --------------------------------------------------------------------------


class _CacheEntry(NamedTuple):
    #: (mtime_ns, size). Size is in the token because a coarse filesystem can
    #: land two edits in one mtime tick.
    token: tuple[int, int]
    structure: FileStructure | None


#: One entry per file rather than one per (file, edit): an edited file replaces
#: its entry instead of adding one, so a long-running process cannot grow the
#: cache by editing. Failures are cached too — a file with a syntax error would
#: otherwise be reparsed on every redraw.
#:
#: No lock. Two threads analysing one file at once both parse it and the second
#: overwrites the first with an equivalent result; a lock would buy nothing but
#: the chance to hold one during a file read.
_cache: dict[str, _CacheEntry] = {}


def analyze_file(pathname: str) -> FileStructure | None:
    """Structure for one source file, or None if it cannot be read.

    Never raises. Results are cached against the file's mtime and size, so a
    file is parsed once per process unless it changes on disk.

    Measured on `examples/demo.py` (631 lines): ~6ms for that first parse,
    split about evenly between `ast.parse` and the walk and scaling with file
    length — 25ms for stdlib's 2,345-line `logging/__init__.py`. A cache hit
    is ~8µs, which is almost entirely the `os.stat` that checks freshness.
    Both on the same class of shared box the capture benchmark warns about,
    so treat them as an order of magnitude rather than a figure.

    That ratio is the scheduling constraint: cheap enough to ask once per
    source per redraw, far too expensive to ask once per record. Like the
    rest of the analysis, this belongs on the poll.
    """
    try:
        stat = os.stat(pathname)
    except OSError:
        return None
    token = (stat.st_mtime_ns, stat.st_size)
    entry = _cache.get(pathname)
    if entry is not None and entry.token == token:
        return entry.structure
    structure = _parse(pathname)
    _cache[pathname] = _CacheEntry(token=token, structure=structure)
    return structure


def template_matches(pathname: str, lineno: int, stored_msg: str) -> bool:
    """Whether the file on disk still describes the code that emitted a record.

    **The drift guard, and it is load-bearing.** A record's `lineno` describes
    the code as the running process imported it; the file on disk may have
    been edited since, at which point `file:lineno` points somewhere else
    entirely and every structural claim keyed on it is wrong. `record.msg` is
    the one field that survives both sides of that gap: under lazy
    %-formatting it *is* the literal template, which is exactly what the AST
    reads at `file:lineno`.

    So this compares two structured fields for equality. It is not inference
    from message content — nothing is parsed, matched or masked, and a single
    character of difference is a refusal.

    False means "use nothing from this file", and covers every uncertain case:
    unreadable file, unrecognised line, a call site whose template did not
    survive to runtime (an f-string), and genuine drift. The f-string case is
    the one where a refusal costs something a weaker check could have given —
    `funcName` agrees whether or not a template exists. lumberjack: see issue
    #60.
    """
    structure = analyze_file(pathname)
    if structure is None:
        return False
    site = structure.call_sites.get(lineno)
    return site is not None and site.template == stored_msg


def clear_cache() -> None:
    """Drop every parsed file. For tests, and for a process that edits code."""
    _cache.clear()


__all__ = [
    "MODULE_SCOPE",
    "CallSite",
    "FileStructure",
    "Loop",
    "LoopKind",
    "analyze_file",
    "clear_cache",
    "template_matches",
]
