"""Rung 3: `track()` where the count is known, so the bar is exact.

`track()` mirrors `tqdm`. The difference is that the total here was *stated*
rather than inferred, so the bar is determinate from the first frame and the
final one is the real count — progress ticks are sampled, but `end` is not.
"""

import lumberjack

lumberjack.init(output_mode="rich", flush_interval=0.01, dump_last_n=0)

for _ in lumberjack.track(range(50), name="ingest", total=50):
    pass

with lumberjack.task("reconcile") as outer:
    outer.subtask("compare").end()

lumberjack.flush()
