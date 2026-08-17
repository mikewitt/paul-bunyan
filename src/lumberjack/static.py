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

#: What the message argument *is*, which decides what survives to runtime.
#:
#: - `literal` — a string constant, so `record.msg` is the template.
#: - `fstring` — an f-string, so the template is rendered away at the call
#:   site and cannot be recovered. `%`, `+` and `.format()` do the same thing
#:   and are deliberately *not* classified here: they need expression analysis
#:   this module does not do, and ruff's G001-G003 already own them.
#: - `parameter` — a bare name that is a parameter of the enclosing function,
#:   which is the signature of a logging wrapper (see `has_stacklevel`).
#: - `other` — anything else. A local variable, a subscript, a call.
MessageKind = Literal["literal", "fstring", "parameter", "other"]

#: Statements that own a body of their own, so a statement count for one body
#: stops at them: a nested loop is a separate body with its own call sites,
#: and a nested `def` does not run when the enclosing body does.
_OWN_BODY: Final = (
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
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
    #: The `def`/`class`/`lambda` line of the enclosing scope, or None at
    #: module scope. `func_name` alone does not identify a scope — one file
    #: can hold two methods of the same name on different classes, and
    #: this package holds two `_depth`s, one in `renderers/progress/sources.py`
    #: and one in `renderers/progress/tasks.py` — so anything grouping by
    #: scope needs this rather than the name.
    func_lineno: int | None
    #: The method called: `debug`, `info`, `log`, … . Not the level a record
    #: ends up with — `.log()` takes that as an argument.
    method: str
    #: The literal message, which under lazy %-formatting is what
    #: `record.msg` holds. None when the first argument is not a string
    #: literal — an f-string, a variable, a concatenation — in which case the
    #: template does not survive to runtime either and there is nothing to
    #: compare against.
    template: str | None
    #: What the message argument is. `template` is non-None exactly when this
    #: is `"literal"`; the other values say *why* there is no template, which
    #: is the difference between advice worth giving and advice that is wrong.
    message_kind: MessageKind
    #: The call passes `stacklevel=`. True also when it forwards `**kwargs`,
    #: which may carry one: unknown is reported as present, so a consumer
    #: warning about a missing `stacklevel=` never warns about a call that
    #: might already have it.
    has_stacklevel: bool
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
    #: The last line the statement spans, so a report can say "lines 145-146"
    #: rather than pointing at a `for` whose body is the interesting part.
    end_lineno: int
    kind: LoopKind
    func_name: str
    #: The enclosing scope's `def`/`class`/`lambda` line — see
    #: `CallSite.func_lineno` for why the name is not enough.
    func_lineno: int | None
    #: 1-based nesting depth within the enclosing scope.
    depth: int
    #: The enclosing loop's `lineno`, or None at depth 1.
    parent: int | None
    #: Statements in this body, counting into `if`/`try`/`with`/`match` blocks
    #: but stopping at a nested loop or `def` — those are bodies of their own.
    #: The one number that says how much work a body does before it repeats,
    #: which is what separates "one log line is plenty" from "one log line for
    #: all of this". It counts statements, not cost: a body of six cheap
    #: assignments and a body of six network calls look identical here.
    body_statements: int
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
    #: The file imports `logging` somewhere — `import logging`, `import
    #: logging.handlers`, `from logging import …`, at any depth.
    #:
    #: The one cheap corroboration available for the receiver-blind match in
    #: `_MESSAGE_ARG` (issue #61). Measured over this venv's installed
    #: packages: 566 recognised call sites, of which 110 are not loggers at
    #: all (`parser.error`, `builder.error`, `errors.warning`). Restricting to
    #: files that import `logging` keeps 429 of the 456 real ones and drops
    #: all but 2 of the 110 — 94% of the signal for 2% of the noise.
    #:
    #: Reported rather than applied. The display wants every call site it can
    #: get, because a *missed* one corrupts sibling ordinals; the linter wants
    #: the opposite trade, because a false one costs its credibility. Same
    #: fact, two consumers, opposite thresholds.
    imports_logging: bool


# --------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------


class _LogCall(NamedTuple):
    """A `Call` node recognised as logging, reduced to what matters."""

    lineno: int
    method: str
    message: ast.expr
    has_stacklevel: bool


class _Ctx(NamedTuple):
    """Lexical context threaded down the walk."""

    func_name: str
    func_lineno: int | None
    #: Parameter names of the enclosing function, for `MessageKind`: a bare
    #: name that is a parameter is a message the caller supplied.
    params: frozenset[str]
    loops: tuple[int, ...]
    conditional: bool


class _Found(NamedTuple):
    log_call: _LogCall
    ctx: _Ctx


_MODULE_CTX: Final = _Ctx(
    func_name=MODULE_SCOPE,
    func_lineno=None,
    params=frozenset(),
    loops=(),
    conditional=False,
)


def message_arg_index(method: str) -> int | None:
    """Which positional argument carries the message, or None if not a log call.

    Public for the linter, which suggests replacement call sites. `logger.log()`
    takes the level first, so a suggestion built by interpolating a method name
    into `log.{method}("…")` is code a reader would paste and break. Reading the
    index rather than special-casing the name keeps that right for whatever
    `_MESSAGE_ARG` grows.
    """
    return _MESSAGE_ARG.get(method)


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
        # `kw.arg is None` is `**kwargs`, which may carry a `stacklevel` this
        # walk cannot see. Reporting it as present is the quiet direction.
        has_stacklevel=any(
            kw.arg == "stacklevel" or kw.arg is None for kw in call.keywords
        ),
    )


def _template(message: ast.expr) -> str | None:
    """The literal template, or None when the call site does not have one."""
    if isinstance(message, ast.Constant) and isinstance(message.value, str):
        return message.value
    return None


def _message_kind(message: ast.expr, params: frozenset[str]) -> MessageKind:
    """Classify the message argument. See `MessageKind` for what is left out."""
    if isinstance(message, ast.Constant) and isinstance(message.value, str):
        return "literal"
    if isinstance(message, ast.JoinedStr):
        return "fstring"
    if isinstance(message, ast.Name) and message.id in params:
        return "parameter"
    return "other"


def _params(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> frozenset[str]:
    args = node.args
    names = {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    for extra in (args.vararg, args.kwarg):
        if extra is not None:
            names.add(extra.arg)
    return frozenset(names)


def _imports_logging(node: ast.Import | ast.ImportFrom) -> bool:
    """`import logging`, `import logging.handlers`, `from logging import …`."""
    if isinstance(node, ast.ImportFrom):
        # `from . import logging` has module None and is somebody else's
        # `logging`, so a relative import never counts however it is spelled.
        return node.level == 0 and (node.module or "").split(".")[0] == "logging"
    return any(alias.name.split(".")[0] == "logging" for alias in node.names)


def _body_statements(body: list[ast.stmt]) -> int:
    """Statements in one body, stopping at anything with a body of its own."""
    total = 0
    for stmt in body:
        total += 1
        if not isinstance(stmt, _OWN_BODY):
            total += _nested_statements(stmt)
    return total


def _nested_statements(node: ast.AST) -> int:
    # Descends through non-statement nodes too — `ExceptHandler` and
    # `match_case` are neither statements nor expressions, and their bodies
    # belong to the block that contains them.
    total = 0
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.stmt):
            total += 1
            if isinstance(child, _OWN_BODY):
                continue
        total += _nested_statements(child)
    return total


class _Walker:
    """Depth-first, source-order collection of call sites and loops.

    Source order matters and is free: two call sites are visited in the order
    they appear in the file whatever their nesting, so grouping the results by
    innermost loop afterwards yields each body already in textual order.
    """

    def __init__(self) -> None:
        self.found: list[_Found] = []
        self.loops: dict[int, Loop] = {}
        self.imports_logging = False

    def visit(self, node: ast.AST, ctx: _Ctx) -> None:
        if isinstance(node, ast.Import | ast.ImportFrom):
            self.imports_logging = self.imports_logging or _imports_logging(node)
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
            ctx = _Ctx(
                func_name=node.name,
                func_lineno=node.lineno,
                # A class body has no parameters, and a call in one is not a
                # wrapper forwarding anything.
                params=frozenset() if isinstance(node, ast.ClassDef) else _params(node),
                loops=(),
                conditional=False,
            )
        elif isinstance(node, ast.Lambda):
            ctx = _Ctx(
                func_name="<lambda>",
                func_lineno=node.lineno,
                params=_params(node),
                loops=(),
                conditional=False,
            )
        elif isinstance(node, ast.If | ast.Try | ast.TryStar | ast.Match):
            ctx = ctx._replace(conditional=True)

        for child in ast.iter_child_nodes(node):
            self.visit(child, ctx)

    def _visit_loop(self, node: ast.For | ast.AsyncFor | ast.While, ctx: _Ctx) -> None:
        inner = (*ctx.loops, node.lineno)
        self.loops[node.lineno] = Loop(
            lineno=node.lineno,
            # Optional only for hand-built nodes; parsed source always sets it.
            end_lineno=node.end_lineno or node.lineno,
            kind=_LOOP_KINDS[type(node)],
            func_name=ctx.func_name,
            func_lineno=ctx.func_lineno,
            depth=len(inner),
            parent=ctx.loops[-1] if ctx.loops else None,
            body_statements=_body_statements(node.body),
        )
        # What repeats gets the inner context. The body always does. A
        # `while` **test** does too — it is re-evaluated before every
        # iteration, so a call there fires once per pass and belongs to the
        # loop. `For.iter` does not: it is evaluated once before the loop
        # starts. Neither does a `for … else` clause, which runs once after.
        #
        # Getting the `while` test wrong is not a case of claiming less: the
        # call fires per iteration at runtime, so attributing it outside the
        # loop makes static analysis *contradict* what the records show, and
        # consumers use static as a veto over runtime inference.
        #
        # `conditional` resets for the body because the flag describes a
        # branch *inside this body*; a loop nested under an `if` is itself
        # conditional, but its own body is not.
        repeats = {id(stmt) for stmt in node.body}
        if isinstance(node, ast.While):
            repeats.add(id(node.test))
        body = repeats
        body_ctx = ctx._replace(loops=inner, conditional=False)
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
                func_lineno=item.ctx.func_lineno,
                method=item.log_call.method,
                template=_template(item.log_call.message),
                message_kind=_message_kind(item.log_call.message, item.ctx.params),
                has_stacklevel=item.log_call.has_stacklevel,
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
        imports_logging=walker.imports_logging,
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
    "MessageKind",
    "analyze_file",
    "clear_cache",
    "template_matches",
]
