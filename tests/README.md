# The test tiers

Three tiers, and only two of them make a claim. The point is a small surface
that can be reviewed carefully and changed reluctantly, not a taxonomy for its
own sake — so a tier is defined by a rule a machine can check, never by how
important a test felt when it was written. Judgement decays; `test_tier1_rules.py`
does not.

| Tier | Where | Claim | Checked by |
|---|---|---|---|
| **1 — acceptance** | `tests/tier1/` | This is what the package is *for*, and how a user gets it | `tests/tier1/test_tier1_rules.py` |
| **2 — contract** | `pytestmark = pytest.mark.tier2`, files stay put | A component's public API behaves as specified | marker + SLF001-clean |
| **3 — internals** | everything else, unmarked | Whatever it says | nothing |

**Unmarked is tier 3.** Only claims are validated, so drift runs downward: a
test written carelessly claims nothing and gets nothing, rather than sliding
into a tier someone believes was reviewed.

## Tier 1

A parent test in `tests/tier1/` launches a standalone script from
`tests/tier1/scripts/` as a real child process and asserts on its stdout,
stderr and exit code. Nothing else.

**The scripts are the artifact.** Each one reads as a program a developer might
actually write — `import lumberjack`, `init()`, a loop, some logging — and a
reviewer reading the directory should come away knowing how to use the package.
That is the second job tier 1 does: it is the API's executable specification,
so a call that reads badly here is a finding about the API, not about the test.

Enforced, with a deliberate violation of each proven to fail:

- **A script may import `lumberjack` and the standard library, nothing else** —
  and `lumberjack` bare, never a submodule. `from lumberjack.store import …` is
  the tell that a script wants something the published surface does not offer.
- **A parent never imports `lumberjack` at all.** This is the rule the tier rests
  on. A parent that imports the package can quietly become an in-process test
  wearing acceptance clothing — asserting on objects instead of on what a user
  would see — and the tier stops meaning anything.
- **Nothing private** on either side; **no `monkeypatch`** in a parent.
- **Launch through `script_runner.child_env()`**, which sets
  `COVERAGE_PROCESS_START`. A parent that builds its own environment stops
  measuring the child, silently: the run still passes and only the coverage
  number moves.

`test_tier1_rules.py` lives *inside* `tests/tier1/`, because a guard outside the
protected path is the cheap way in.

### Two facts every tier-1 assertion rests on

Both measured, both easy to trip over:

- **`dump_last_n=0`, unless the dump is the subject.** Left on, the `atexit`
  diagnostic replays the tail of the store *after* the display comes down, so
  a script that logs 50 things prints 50 lines after its own bar. Every clean
  frame assertion here depends on it being off.
- **A piped `Live` prints the final frame only.** With `output_mode="rich"`
  forced onto a non-TTY stderr, rich does not repeat frames — which is why
  `re.findall(r"processing item \d+", stderr) == ["processing item 199"]` is a
  stable assertion rather than a race. This is rich's behaviour, not ours: if a
  rich upgrade changes it, every stderr assertion here moves at once.

### Why `loop_to_bar.py` and `scripts/loop_then_exit.py` both exist

They are not a duplicate that someone forgot to merge. `loop_then_exit.py` is a
fixture with environment knobs for exit-path plumbing, and it needs a
file-backed store the parent reopens *after the child dies* — with the final
flush disabled, the record count is only knowable then, which the tier-1
`current_store()`-to-stdout protocol structurally cannot do. It also imports
`SQLiteRecordStore`, which tier 1 forbids. One is plumbing; one is documentation.

### What tier 1 deliberately does not cover

- **`shutdown()` and restoring the root logger.** Exercised incidentally — the
  process exits — but never asserted on. It is lifecycle, not the product.
- **TTY detection.** Every script forces `output_mode` explicitly. Detection →
  rich is covered only by `demo-gif.yml` under a real pty, and closing that gap
  is its own problem.
- **`wrapped`, `bursty`, `silent`** (see `examples/demo.py --list`). The first
  two are shapes the design documents as handled *badly*; pinning them here
  would freeze known-wrong behaviour as the acceptance contract, and tier 1 is
  meant to be expensive to change. `silent`'s contract is that the heartbeat
  *stops*, which a single final frame cannot witness.
- **Exact totals from inference.** Principle 10 licenses the display to be
  imprecise, so tier 1 asserts that a determinate bar appears, never that
  timing produced an exact number.

### Slow is a separate axis

`@pytest.mark.slow` is orthogonal to the tiers — a test can be tier 1 and
slow, and conflating "how important" with "how long" is how a tiering scheme
rots. `pyproject.toml` deselects `slow` from the default run, so `pytest` stays
around eleven seconds; the `slow-tests` CI job selects them with `-m slow`.

`tests/tier1/test_slow_shapes.py` is the whole of it today, and everything in
it is slow irreducibly: `MIN_LEGIBLE_PERIOD` is 1.0s and deliberately not
settable from the public API, and an inferred total has to hold across
consecutive real polls. Its assertions are loose about numbers and strict
about shape — the inferred total is asserted as a band around 20, not as 20,
because Principle 10 licenses the display to be imprecise and a test
demanding precision would fail for being right.

That job runs on one ubuntu leg rather than the matrix. Measured: a windows
leg spends 65s provisioning before running anything and bills at double,
where ubuntu spends 1s — so six copies would cost more than the rest of CI
and learn nothing, since what these test is lumberjack's timing rather than
the platform's.

## Tier 2

Component contracts driven through a component's public API — `RecordStore`
conformance, handler capture, output-mode detection, tracking semantics.
Fabricated inputs (`make_row`, `make_task_row`) are fine here; that is the
difference from tier 1, which may not fabricate anything.

Marked file-wide with `pytestmark = pytest.mark.tier2`, never per-function: the
private-access check is per-file, so a half-marked file would be uncheckable.
If half a file qualifies, the file is tier 3 until someone splits it.

`test_tier2_rules.py` runs `ruff --select SLF001 --isolated` over every marked
file. **`--isolated` is load-bearing**: `pyproject.toml` exempts SLF001 for
`tests/**` — rightly, since tier 3 unit-tests private functions on purpose —
and `per-file-ignores` applies even to a CLI `--select`, so without it the
check reports zero and proves nothing. That is not hypothetical; it reported
"all checks passed" over 31 real findings while they were being counted.

Currently marked: `test_store`, `test_handler`, `test_detect`,
`test_render_plain`, `test_otel`, `test_tracking`, `test_plan`,
`test_recording_renderer`.

### The display, in three layers

Display coverage is split across three files and the split is load-bearing —
each catches what the others structurally cannot:

| file | claim | needs `rich` |
|---|---|---|
| `test_plan.py` | the decision is right | no |
| `test_display_parity.py` | rich holds exactly what was decided | yes |
| `test_render_progress.py`, `test_render_tasks.py` | rich *draws* it | yes |

Parity cannot catch a wrong decision, because the painter faithfully applies
whatever it is handed: filling a determinate row from the run count instead of
the cycle position passes parity, and passed all 606 tests before the split.
And no frame-text assertion can catch a lost total withdrawal, because
`(completed=10, total=None)` and `(completed=10, total=5)` render byte-identically
once rich clamps the second.

`tests/recording_renderer.py` is a `Renderer` that keeps frames instead of
drawing them, for tests that want the display's answer as a number. **It
reimplements nothing** — same models, same `plan_frame()` — and
`test_it_records_what_the_rich_renderer_paints` drives it and the real renderer
over one store and compares, so a recorder that grew its own opinion fails.
Nothing may move out of the rich-gated files onto it: six things are rich's
alone, and `src/lumberjack/renderers/plan.py`'s docstring names them.

**`test_schema.py` is a deliberate omission.** Its
`test_insert_columns_and_created_table_agree` reads the real table through
`sqlite_store._conn` to run `PRAGMA table_info`, and there is no public way to
ask a store what its table looks like. That is a genuine contract test wearing
a private access, and the honest options are to split the file or to leave it
tier 3. It is tier 3 until someone wants to split it — which is the rule
working, not an exception to it.

## Tier 3

Everything else. Private access is allowed and expected — `test_benchmark.py`
unit-tests a script's private functions, `test_progress_layout.py` pins rich's
own internals as an upgrade canary. No marker, no rule, no work.

## Changing a tier-1 test

**A failing tier-1 test means the change is wrong, not the test.** That is the
entire value of a tier: it is the one place where "make CI green" is not a
licence to edit the assertion. If you believe a tier-1 test is genuinely wrong,
say so in the pull request and change it as its own commit with its own
argument — never in the commit that made it fail.

That is an instruction, and an instruction is the weakest of the three things
available: instruct, gate the merge, detect after the fact. The middle one is
`.github/workflows/tier1-guard.yml`, which fails any pull request whose diff
touches **both** `tests/tier1/**` and `src/**` unless it carries the
`tier-1 change` label. The rule and its reasoning live in
`scripts/tier1_guard.py`; `tests/test_tier1_guard.py` drives it.

**The label is the mechanism, not the check.** Applying one needs triage
rights on the repository, so the way past the gate is outside the working
tree — an agent can write any file it likes and cannot label its own pull
request. The check is only a check; adding it to trunk's required-check
ruleset is what makes it binding, and until someone does that it is
advisory. Its `name:` is frozen from that moment, for the reason CLAUDE.md
records about `bare install (no rich)`.

Editing tier 1 is not forbidden by any of this. It is made visible, and
routed through a second pair of eyes.

**Two things it does not catch, stated so nobody mistakes it for complete.**
A pull request that hollows out a tier-1 assertion and touches nothing under
`src/` passes — rarer, and the purest form of the attack. And nothing in the
repository stops someone with triage rights from labelling their own change;
that is what the label being a *record* rather than a lock is for.
