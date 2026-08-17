"""Row order: parents before their children, and never a count deciding it.

The rule this implements is **"rows move on structural change, never on
counts."** The original form was "bars never move once placed", which was right
about the thing it was defending against — a row that jumps around as counts
overtake each other is unreadable — and over-applied. Banning reordering by a
*continuously changing* metric does not imply structure may never be
re-laid-out, and refusing to re-lay-out was wrong permanently: an inner loop
always qualifies for a bar before the loop that encloses it, so first-qualified
order puts every nested row under whatever unrelated row happened to precede
it.

So the order here is a pure function of *structure* — who contains whom, and
who qualified first among siblings. Nothing in it reads a count, a rate or a
percentage, which is what makes recomputing it every poll safe: the answer only
changes when the structure does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from lumberjack.schema import SourceKey


def depth_first_order(
    order: Sequence[SourceKey],
    parent_of: Mapping[SourceKey, SourceKey | None],
) -> list[SourceKey]:
    """`order` re-laid-out so every row follows the row it sits inside.

    `order` is the arrival order — first-qualified — and it survives wherever
    structure says nothing: roots keep it, and so do siblings under one parent.
    That is deliberate. Arrival order is a poor reason to indent one row under
    another, and a perfectly good reason to draw two unrelated loops in the
    order they started.

    A parent naming a row that is not in `order` is treated as no parent at
    all, which covers the ordinary case of a child qualifying before its
    enclosing loop has: the child sits at the top level until the parent shows
    up, and then moves under it.

    Cycles are possible against real data and are handled rather than
    prevented. Containment pairings are frozen once believed, but periods keep
    moving — freeze A as B's parent, let A slow past B, and a later poll can
    freeze B as A's parent. Anything the walk cannot reach from a root is
    appended in arrival order, so a cycle costs two oddly-placed rows instead
    of two missing ones.
    """
    placed = set(order)
    children: dict[SourceKey | None, list[SourceKey]] = {}
    for key in order:
        parent = parent_of.get(key)
        if parent is None or parent == key or parent not in placed:
            parent = None
        children.setdefault(parent, []).append(key)

    result: list[SourceKey] = []
    # Reversed because the stack pops from the end, and siblings must come off
    # it in the order they arrived. Nothing can be pushed twice: `order` holds
    # each key once, so each appears in exactly one child list.
    stack: list[SourceKey] = list(reversed(children.get(None, ())))
    while stack:
        key = stack.pop()
        result.append(key)
        stack.extend(reversed(children.get(key, ())))
    seen = set(result)
    result.extend(key for key in order if key not in seen)
    return result
