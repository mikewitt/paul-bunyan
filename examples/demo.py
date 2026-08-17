"""Log-stream shapes, and what lumberjack currently makes of them.

Run it on a real terminal to see the point:

    uv run python examples/demo.py                  # the pipeline scenario
    uv run python examples/demo.py --list           # every shape available
    uv run python examples/demo.py sequence         # one shape on its own
    uv run python examples/demo.py --all            # all of them, in order

To see what it replaces, force the plain renderer and watch the same run
scroll by:

    LUMBERJACK_OUTPUT_MODE=plain uv run python examples/demo.py

    # PowerShell:
    $env:LUMBERJACK_OUTPUT_MODE="plain"; uv run python examples/demo.py

None of the worker functions know lumberjack exists. They call stdlib
`logging`, and the display is a property of how the *application* was
configured — which is the entire premise.

## What this file is for

Two jobs, and the second one is why it is structured as scenarios.

**It demonstrates the package.** The `pipeline` scenario is the original demo
and still the one to show someone: four threads, ~2000 records, and a nested
loop that infers its own total with no instrumentation anywhere.

**It is the fixture for designing the display.** Each scenario is a *shape* of
log stream — not a feature demo but a test case for the question "what should
a person see here?". Several of them are shapes lumberjack currently handles
badly or not at all, and they are here precisely for that: you cannot argue
about how something should look without being able to run it.

So every scenario states three things:

- **the stream** — what gets logged, factually
- **today** — what lumberjack does with it now, observed rather than hoped
- **should be** — what it is supposed to do instead

All eight `should be` lines are decided, which makes this file the readable
form of the display spec: the prose lives in "What the display is for" in
CLAUDE.md. Most of it is now built — one row per inferred loop, counting
iterations, labelled by template, laid out by containment and collapsing when
quiet (#8, #43, #56) — and what remains is #53's intra-iteration row and
#54's polish. If a `today` line stops being true, this file has caught a
change: update it, because those lines are observations rather than hopes.

## How to log so this works

The shapes below are also the advice, and it is the **middle rung of the value
ladder**: not "drop it in and accept what you have", not "learn our API", but
*log idiomatically and the display gets much better for free*. Everything here
is ordinary good practice that makes a log file worth reading even with
lumberjack uninstalled, which is what makes it a reasonable thing to ask for.
The instrumentation linter (#40) is meant to say which of these a given
codebase is missing, so nobody has to read this list.

- **Leave the `logger.debug` lines in, and add more.** Density is input
  quality. A loop that logs once per iteration is a bar; a loop that logs
  nothing is invisible, and no amount of inference recovers it.
- **Log inside the body, not around it.** A line before and after a loop says
  it started and finished. A line *in* it says how fast it is going.
- **A line per phase of a slow body is worth more than one line per body.**
  See `sequence`: five lines in a three-second iteration can say where you
  are within it; one line can only say it happened.
- **Announce each stage of a multi-stage program.** One `log.info("stage 2:
  parsing records")` per stage is what lets a finished stage collapse and the
  current one be named. See `phases` — a program without those lines gets no
  collapse signal, and that is deliberately not worked around.
- **Do not build a logging wrapper without `stacklevel=`.** Identity is the
  source location, so a shim makes every call site in your program look like
  one line. See `wrapped`.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import lumberjack

log = logging.getLogger("pipeline")

#: Slow enough that bars visibly move rather than finishing instantly.
TICK = 0.004


# --------------------------------------------------------------------------
# The scenarios. Each is an ordinary program that logs; none is lumberjack
# aware. Keep them that way — the moment a scenario calls track() it stops
# being a test of inference.
# --------------------------------------------------------------------------


def extract(count: int) -> None:
    for i in range(count):
        log.debug("fetched row %d from source table", i)
        time.sleep(TICK)


def transform(count: int) -> None:
    for i in range(count):
        log.debug("normalized record %d", i)
        if i == count // 2:
            # WARNING and above is never collapsed — it prints above the bars,
            # because the one line you actually need to see must not be hidden
            # by the thing that hides noise.
            log.warning("row %d had a null timestamp; defaulting to epoch", i)
        time.sleep(TICK * 1.7)


def load(count: int) -> None:
    for i in range(count):
        log.debug("wrote batch %d to warehouse", i)
        time.sleep(TICK * 2.5)


def reconcile(batches: int, rows: int) -> None:
    """A genuinely nested loop, still saying nothing about lumberjack.

    Nothing here declares a total, a name, or a hierarchy. The inner line
    fires `rows` times between consecutive firings of the outer one, and that
    ratio is both the evidence the loops are nested and the length of the
    inner one — so after a couple of outer iterations the inner line stops
    being a counter and becomes a real bar that fills, resets, and fills
    again.
    """
    for batch in range(batches):
        log.debug("reconciling batch %d", batch)
        for row in range(rows):
            log.debug("compared row %d against ledger", row)
            time.sleep(TICK * 1.25)


def run_pipeline() -> None:
    workers = [
        threading.Thread(target=extract, args=(700,), name="extract"),
        threading.Thread(target=transform, args=(450,), name="transform"),
        threading.Thread(target=load, args=(300,), name="load"),
        threading.Thread(target=reconcile, args=(24, 20), name="reconcile"),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


def run_sequence() -> None:
    """A slow loop whose body narrates its own stages.

    The shape that motivates #53. All five lines are one loop body — the source
    says so outright, and the display now draws them as one row counting
    iterations. There is still no ratio to take, so no total, so no bar that
    fills.

    But the information is plainly there: reaching "committing" means this
    iteration is nearly done. That is ordinal position within a cycle, which
    the AST already reports (`static.CallSite.position`) and nothing yet draws.
    """
    for batch in range(6):
        log.debug("batch %d: opening connection", batch)
        time.sleep(0.30)
        log.debug("batch %d: fetching manifest", batch)
        time.sleep(0.30)
        log.debug("batch %d: validating checksums", batch)
        time.sleep(0.45)
        log.debug("batch %d: writing output", batch)
        time.sleep(0.30)
        log.debug("batch %d: committing", batch)
        time.sleep(0.15)


def run_siblings() -> None:
    """One fast loop, several call sites in its body.

    The shape that motivated #8, and the smallest case that shows why display
    unit is not identity unit. These lines are one loop by any reasonable
    reading, and a person wants one row for it. Source location is the right
    *identity* — exact, no inference — and was the wrong *display unit*.

    Scale it up and it was the 800-bar problem: nothing here is granular
    because the code is badly written, it was granular because the display
    inherited its unit from the grouping key. Keep the four lines; they are
    what makes the merge worth testing.
    """
    for row in range(400):
        log.debug("row %d: parsed", row)
        log.debug("row %d: schema validated", row)
        log.debug("row %d: enriched from cache", row)
        log.debug("row %d: emitted downstream", row)
        time.sleep(TICK * 2)


def run_oneshot() -> None:
    """Startup narration: every line fires exactly once.

    The shape that motivates #54. A source needs repetition to have a period,
    so none of this draws anything at all — the display is empty while the
    program is plainly working, which is indistinguishable from hung.

    This is also the honest half of the matplotlib finding: a cold `savefig`
    logs its font-cache setup and then goes silent for the seconds that
    actually render. Libraries narrate boundaries, not work.
    """
    log.info("reading configuration from /etc/pipeline.toml")
    time.sleep(0.4)
    log.info("connecting to warehouse at db.internal:5432")
    time.sleep(0.6)
    log.info("negotiated protocol version 3")
    time.sleep(0.3)
    log.info("warming schema cache")
    time.sleep(0.8)
    log.info("registered 14 table mappings")
    time.sleep(0.4)
    log.info("ready")


def run_silent() -> None:
    """Says it started, does three seconds of work, says it finished.

    The case no amount of inference can rescue, and the reason a heartbeat has
    to stay honest rather than inventing activity: there is genuinely nothing
    in the stream between the two lines. Whatever the display does here, it
    must not claim progress it cannot see.
    """
    log.info("rendering 2.4M points at dpi=200")
    time.sleep(3.0)
    log.info("wrote figure.png")


def run_bursty() -> None:
    """A loop whose iterations vary wildly in length.

    Retirement is 10x the measured period, and promotion needs a ratio stable
    across consecutive polls, so this is the shape that makes both misbehave:
    a long pause looks like the loop ended, and the next burst resurrects it.

    Principle 10 says a display that is wrong here is acceptable. This
    scenario exists so "acceptable" is something you can look at rather than
    something asserted in a docstring.

    A fixed stall pattern rather than a seeded RNG. A design fixture is
    argued over across runs and machines, so it should emit the *same* stream
    every time; what matters is the shape — mostly quick, occasionally a long
    pause — and a literal set says that more plainly than a distribution does.
    """
    stalls = {3, 11, 19, 28, 35}
    for item in range(40):
        log.debug("processing item %d", item)
        # 12% of iterations take 40x the rest, which is what breaks an average.
        time.sleep(0.8 if item in stalls else 0.02)


def run_phases() -> None:
    """Four loops in sequence, each finishing before the next starts.

    The shape of most scripts. Each loop retires as the next begins, so by the
    end there are four finished rows and one live one — and which is *now* is
    legible: the live rows sort above the quiet ones, and a quiet row collapses
    to a mark rather than holding a full-width bar.

    A retired bar is marked idle in place rather than deleted, deliberately —
    deleting it would empty the final frame.

    Written as four functions rather than a loop over a table of stages, and
    that is not style. Identity is the source location, so a data-driven
    version puts every stage on one `log.debug` line and they become a single
    source — one row, no retirement, nothing to see. Four stages in a program
    means four call sites, and this scenario is only honest if it has them.
    """

    def discover() -> None:
        for i in range(60):
            log.debug("found input file %d", i)
            time.sleep(0.012)

    def parse() -> None:
        for i in range(200):
            log.debug("parsed record %d", i)
            time.sleep(0.006)

    def join() -> None:
        for i in range(120):
            log.debug("joined row %d against reference data", i)
            time.sleep(0.010)

    def write() -> None:
        for i in range(40):
            log.debug("wrote partition %d", i)
            time.sleep(0.020)

    for number, (label, stage) in enumerate(
        [
            ("discovering input files", discover),
            ("parsing records", parse),
            ("joining against reference data", join),
            ("writing partitions", write),
        ],
        start=1,
    ):
        log.info("stage %d: %s", number, label)
        stage()


def _log_via_wrapper(message: str, *args: object) -> None:
    """A logging shim of the kind people write, missing `stacklevel=2`."""
    log.debug(message, *args)


def run_wrapped() -> None:
    """Three unrelated loops routed through one logging helper.

    Identity is the source location, so every one of these collapses onto the
    single `log.debug` line inside `_log_via_wrapper` — one source, three
    loops' worth of records, and a period that is the interleaving of all
    three rather than any real one.

    The fix is one keyword (`stacklevel=2`) in the wrapper, which is why this
    is documented as a limitation with a diagnostic rather than solved by
    inference (#37). It is here so the failure is visible rather than
    theoretical.
    """

    def worker(name: str, count: int, pace: float) -> None:
        for i in range(count):
            _log_via_wrapper("%s: item %d", name, i)
            time.sleep(pace)

    threads = [
        threading.Thread(target=worker, args=("alpha", 200, 0.008), name="alpha"),
        threading.Thread(target=worker, args=("beta", 120, 0.014), name="beta"),
        threading.Thread(target=worker, args=("gamma", 80, 0.021), name="gamma"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """One shape of log stream, and the design question it poses.

    `today` is observed behaviour, not intent — if it stops being true, the
    scenario has caught a change and the text is the thing to update. `should`
    is the open question, and `TBD` in it is honest rather than lazy.
    """

    name: str
    stream: str
    today: str
    should: str
    run: Callable[[], None]

    @property
    def settled(self) -> bool:
        return "TBD" not in self.should


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="pipeline",
        stream="Four threads. Three flat loops and one genuinely nested pair.",
        today=(
            "Five rows, each labelled with its message template — `fetched row "
            "… from source table`. The nested inner line infers its own total "
            "(20/20) from the ratio to its parent, is lexically corroborated by "
            "the source, and is drawn indented directly beneath that parent "
            "(#43). The other four pulse: nothing in the stream says how long "
            "they are."
        ),
        should=(
            "This, and it is the reason this is still the demo to show someone. "
            "The remaining gap is the outer loops' totals, which are 'ideally "
            "but unlikely' to be inferable — they stay pulses until someone "
            "wraps a range in `track()`, and that gap is deliberate."
        ),
        run=run_pipeline,
    ),
    Scenario(
        name="sequence",
        stream="A slow loop; its body logs five distinct stages, once each.",
        today=(
            "One row for the loop — `demo.py:163 run_sequence()` — pulsing, "
            "counting iterations (6, not the 30 records), ticking once per "
            "~1.5s. The five call sites merged (#8). No sub-iteration progress "
            "yet: that is the second row, and it is #53."
        ),
        should=(
            "Three rows. A **pulsing** bar for the outer loop — its total is "
            "'ideally but unlikely' to be inferable, so it stays a pulse and "
            "becomes determinate only if someone wraps the range in `track()`. "
            "Beneath it a **determinate** bar for position within the current "
            "iteration, ticked by the body's call sites in order (#53). "
            "Optionally a third line showing the most recent message. The "
            "stage *name* comes from `record.msg` — the template, which stdlib "
            "keeps separate from the data whenever the call uses lazy "
            "%-formatting (ruff's G001-G004 enforce exactly that). So "
            "'batch %d: validating checksums' is a stable label per source "
            "location with no parsing of rendered text and no hints config. "
            "An f-string at the call site destroys the template, and then "
            "there is only a position."
        ),
        run=run_sequence,
    ),
    Scenario(
        name="siblings",
        stream="One fast loop with four call sites in its body.",
        today=(
            "One row, `demo.py:188 run_siblings()`, reading **400 iterations** "
            "— the rows the code operated on, not the 1600 log calls that "
            "described them. The four call sites are the identity underneath "
            "it, and the store and the exit summary still report all 1600."
        ),
        should=(
            "This (#8). Every call site here says `row %d`, so the row is the "
            "thing making progress; that four lines narrate each one is the "
            "author's choice and not something anybody asked about. 1600 was "
            "the accidental number. No intra-iteration row: at 120/s it would "
            "be unreadable, which is the legibility criterion deciding "
            "correctly that the extra row is not earned."
        ),
        run=run_siblings,
    ),
    Scenario(
        name="oneshot",
        stream="Six startup lines, each firing exactly once over ~2.5s.",
        today=(
            "One row, and no bars: no source repeats, so none earns one. The "
            "session heartbeat counts every record, times the arrival rate "
            "from the records' own timestamps, and carries the newest line "
            "beside it — `6 events · 2.0/s   ready` (#54)."
        ),
        should=(
            "A session heartbeat driven by arrival rate, with the most recent "
            "line beside it (#54); singletons may optionally render the same "
            "way. It must not imply progress toward an end it cannot see. This "
            "is also the clearest case for the instrumentation linter (#40): "
            "there is little here to work with, and saying so is more useful "
            "than inventing a display."
        ),
        run=run_oneshot,
    ),
    Scenario(
        name="silent",
        stream="One line, three seconds of real work, one more line.",
        today=(
            "A heartbeat that arrives with the first line and then stops. "
            "Every redraw through the three silent seconds — a dozen or so at "
            "200ms apiece — draws the identical row: same frame, same count, "
            "same message, because the frame is an index a record moves and "
            "not a clock. It advances once, when the second line lands, and "
            "the rate then reads `3.0s each` (#54)."
        ),
        should=(
            "The heartbeat **stops moving**, and says nothing else — no 'idle' "
            "label, no progress. The absence is the message. This is the "
            "honesty test for #54: a spinner that keeps turning on wall-clock "
            "would be claiming liveness nobody observed. A developer seeing a "
            "stopped heartbeat is being told to log more, which is the right "
            "prompt rather than a display failure to paper over."
        ),
        run=run_silent,
    ),
    Scenario(
        name="bursty",
        stream="One loop, 80% fast iterations and 20% multi-second stalls.",
        today=(
            "A row that collapses to a mark during a stall and comes back on "
            "the next burst, with a rate that swings by an order of magnitude."
        ),
        should=(
            "Annotate the rate as erratic when variance is high — `~3/s "
            "(erratic)` — since an average of 20ms and 1.2s iterations "
            "describes no real iteration. Cheap from what the model already "
            "keeps, and explicitly low priority: minutiae next to the rest."
        ),
        run=run_bursty,
    ),
    Scenario(
        name="phases",
        stream="Four loops in sequence, each finishing before the next starts.",
        today=(
            "A pulsing `stage …: …` row for the sequence, with the running "
            "stage indented beneath it — also pulsing. The announcement line "
            "is a slow repeating source and period ordering still reads it as "
            "an enclosing loop, but the AST says the stage's loop is top-level "
            "in another function, so the ratio's total (49 for a loop that runs "
            "40 times) is withheld and the row claims nothing. Finished stages "
            "collapse: no bar, a mark, their iteration count and `idle`, sorted "
            "below the live rows most-recent-first."
        ),
        should=(
            "This. The remaining piece is the *label*: 'stage …: …' is the "
            "template with its specifiers substituted, where 'Stage 4 of 4' "
            "would need the data back. Telling 'A encloses B' from 'A precedes "
            "B' is not solvable from logs alone and is not solved here — it is "
            "*refused*, which is the honest form. A program with no "
            "announcement line gets no sequence row at all and is deliberately "
            "not worked around; the linter (#40) should point out the missing "
            "line, since adding it is idiomatic logging rather than a "
            "lumberjack idiom."
        ),
        run=run_phases,
    ),
    Scenario(
        name="wrapped",
        stream="Three unrelated loops, all routed through one logging helper.",
        today=(
            "One row, labelled `…: item …` after the wrapper's own template. "
            "Every call site collapses onto the wrapper's `log.debug` line, and "
            "its period is the interleaving of three loops."
        ),
        should=(
            "Nothing, for now. It is bad practice, it is documented as such, "
            "and it is explicitly not a priority — #37 stays open and "
            "unscheduled. The one-keyword fix (`stacklevel=2`) belongs in the "
            "user's wrapper, and inference should not chase it."
        ),
        run=run_wrapped,
    ),
)

BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _wrap(text: str, width: int, indent: str) -> Iterator[str]:
    line: list[str] = []
    length = 0
    for word in text.split():
        if length + len(word) + len(line) > width and line:
            yield indent + " ".join(line)
            line, length = [], 0
        line.append(word)
        length += len(word)
    if line:
        yield indent + " ".join(line)


def _report(scenario: Scenario) -> None:
    """Print what the run actually produced, after the display is down.

    Read out of the store rather than tracked alongside it: the point of the
    package is that the store is lossless whatever the display did, and a
    summary that kept its own counters would not be demonstrating that.
    """
    lumberjack.flush()
    store = lumberjack.current_store()
    if store is None:
        raise RuntimeError("init() ran, but no store was found")
    records = store.recent(n=None)
    by_source = sorted(store.count_by_source().items(), key=lambda kv: -kv[1])

    by_thread: dict[str, int] = {}
    for record in records:
        by_thread[record.thread_name] = by_thread.get(record.thread_name, 0) + 1
    above_debug = [r for r in records if r.level_no >= logging.WARNING]

    # Everything the summary needs is now in local variables, so the display
    # comes down *before* a line of it is printed. A program's own output and
    # a live redraw must not share a terminal: lumberjack deliberately leaves
    # stdout alone, so nothing is there to interleave the two politely, and
    # printing over a live frame is how a summary ends up shredded.
    lumberjack.shutdown()

    print(f"\n=== {scenario.name} ===")
    for label, text in (
        ("stream", scenario.stream),
        ("today", scenario.today),
        ("should be", scenario.should),
    ):
        wrapped = list(_wrap(text, 66, " " * 12))
        print(f"  {label:<9} {wrapped[0].strip()}")
        for line in wrapped[1:]:
            print(line)

    plural = "" if len(by_source) == 1 else "s"
    print(f"\n  {len(records)} records over {len(by_source)} source location{plural}")
    for source, count in by_source:
        print(f"    {source.func_name:<22} line {source.lineno:<5} {count:>5} records")
    if len(by_thread) > 1:
        print("\n  by worker (attributed at write time, never inferred):")
        for name, count in sorted(by_thread.items(), key=lambda kv: -kv[1]):
            print(f"    {name:<22} {count:>5} records")
    if above_debug:
        print(f"\n  shown above the bars, and still stored: {len(above_debug)}")
        for record in above_debug:
            print(f"    {record.level_name} {record.message}")


def _run(scenario: Scenario) -> None:
    # One session per scenario: each gets a clean store, so the counts below
    # describe that shape rather than everything run so far.
    lumberjack.init()
    try:
        scenario.run()
    finally:
        _report(scenario)


def _list() -> None:
    print("Log-stream shapes. Run one with: python examples/demo.py <name>\n")
    for scenario in SCENARIOS:
        mark = " " if scenario.settled else "*"
        print(f" {mark} {scenario.name:<10} {scenario.stream}")
    if any(not scenario.settled for scenario in SCENARIOS):
        print("\n  * = the display's answer here is still an open question.")
    print("\n  Every scenario states the stream, what lumberjack does with it")
    print("  today, and what it should do instead. Run one to read all three.")
    print("  See 'What the display is for' in CLAUDE.md for the criterion.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "scenario",
        nargs="?",
        default="pipeline",
        choices=[s.name for s in SCENARIOS],
        help="which shape to run (default: pipeline)",
    )
    parser.add_argument("--list", action="store_true", help="describe every shape")
    parser.add_argument("--all", action="store_true", help="run all of them, in order")
    args = parser.parse_args(argv)

    if args.list:
        _list()
        return 0
    for scenario in SCENARIOS if args.all else [BY_NAME[args.scenario]]:
        _run(scenario)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
