"""Record a demo scenario as an animated GIF.

    uv run --with pillow --with pyte --with fonttools \
        python scripts/record_demo.py pipeline

(`fonttools` is real: `_load_faces` reads each font's cmap to know which
face covers which codepoint, and it is not a Pillow dependency — the CI
recording failed the first time precisely because this line used to omit
it.)

The README's sample output is ASCII pasted from a run, which cannot show the
thing that matters: bars advancing, the heartbeat ticking, a nested bar
filling and resetting. This produces the moving version.

## Why it goes through a terminal emulator

A live display works by moving the cursor and overwriting, so the raw pty
byte stream is not a sequence of frames — it is edits to a screen. Stripping
the ANSI out of it (what the earlier one-off scripts did) yields every
intermediate line ever printed, in order, which is emphatically not what a
viewer saw. `pyte` is a real terminal emulator: feed it the same bytes and
ask it for the screen contents, and you get what was actually on screen.

Colour comes from the same place, per character, so the output is the real
palette rather than a monochrome approximation of it.

## Timing

The pty is read in chunks with a timestamp each, and a frame is emitted on a
fixed wall-clock grid rather than per chunk — a chunk boundary is an artifact
of pipe buffering and has nothing to do with what changed on screen. Frames
identical to their predecessor are collapsed into a longer delay on the one
before, which is what keeps a mostly-static display from costing hundreds of
duplicate frames.
"""

from __future__ import annotations

import argparse
import os
import pty
import select
import sys
import time
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont

#: A fallback chain, because no single font on a stock box covers this
#: display. DejaVu Sans Mono is the nicer face and has the box-drawing
#: characters the bars are made of, but **not** the braille the heartbeat
#: glyph uses — verified against the cmap, since a missing glyph renders as
#: a tofu box that has ink and so fools any "did it draw anything" check.
#: FreeMono covers braille and is used only for what DejaVu lacks.
FONT_SIZE = 16
FACES = [
    (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    ),
    (
        "/usr/share/fonts/truetype/freefont/FreeMono.ttf",
        "/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf",
    ),
]

#: A dark palette, since that is what a terminal running this looks like.
#: pyte reports colours by xterm name or as a hex string.
BACKGROUND = "#12141a"
FOREGROUND = "#d6d9e0"
NAMED = {
    "black": "#2a2e39",
    "red": "#e06c75",
    "green": "#98c379",
    "yellow": "#e5c07b",
    "blue": "#61afef",
    "magenta": "#c678dd",
    "cyan": "#56b6c2",
    "white": "#d6d9e0",
    "brightblack": "#5c6370",
    "brightred": "#e06c75",
    "brightgreen": "#98c379",
    "brightyellow": "#e5c07b",
    "brightblue": "#61afef",
    "brightmagenta": "#c678dd",
    "brightcyan": "#56b6c2",
    "brightwhite": "#ffffff",
    "default": FOREGROUND,
}


def _colour(name: str, fallback: str) -> str:
    if name in NAMED:
        return NAMED[name]
    # pyte hands back a bare 6-digit hex for 256-colour and truecolour.
    if len(name) == 6:
        try:
            int(name, 16)
        except ValueError:
            return fallback
        return f"#{name}"
    return fallback


def capture(
    argv: list[str], cols: int, rows: int, fps: int, stop_at: str = ""
) -> list[tuple[float, str]]:
    """Run `argv` under a pty and return (timestamp, screen-snapshot) pairs.

    The snapshot is pyte's internal buffer rendered to a string, so two frames
    compare equal only when the screen genuinely did not change.

    `stop_at` is checked here rather than over the finished list, and that is
    not an optimisation. The marker is on screen only until enough output
    scrolls past to push it off, so a post-hoc scan of the last frame finds
    nothing and the recording runs to the end anyway.
    """
    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    frames: list[tuple[float, str]] = []
    interval = 1.0 / fps

    pid, fd = pty.fork()
    if pid == 0:  # child
        os.environ["COLUMNS"], os.environ["LINES"] = str(cols), str(rows)
        os.environ["TERM"] = "xterm-256color"
        # rich suppresses colour when it thinks nothing can show it.
        os.environ["FORCE_COLOR"] = "1"
        # And without this it picks a *standard* 8-colour system, which
        # flattens the pulsing bars into solid slabs. The pulse is a gradient
        # across the bar, so colour depth is the difference between what this
        # records and what the terminal actually shows.
        os.environ["COLORTERM"] = "truecolor"
        # Launching the demo under a pty is the entire job here, and the argv
        # is a fixed list built from `sys.executable` and a path derived from
        # `__file__` — no shell, nothing user-supplied anywhere near it.
        os.execvp(argv[0], argv)  # noqa: S606  # nosec B606

    start = last = time.monotonic()
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], interval)
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                stream.feed(data.decode("utf-8", "replace"))
                if stop_at and any(stop_at in line for line in screen.display):
                    frames.append((time.monotonic() - start, _snapshot(screen)))
                    return frames
            now = time.monotonic()
            if now - last >= interval:
                frames.append((now - start, _snapshot(screen)))
                last = now
    finally:
        os.close(fd)
        os.waitpid(pid, 0)
    frames.append((time.monotonic() - start, _snapshot(screen)))
    return frames


def _snapshot(screen: pyte.Screen) -> str:
    """A comparable rendering of the screen, characters and colours both.

    Four NUL-separated fields per cell, and the trailing separator matters:
    without it a cell's bold flag runs straight into the next cell's
    character and the whole row parses one field out of step.
    """
    out = []
    for y in range(screen.lines):
        row = screen.buffer[y]
        for x in range(screen.columns):
            ch = row[x]
            out.append(f"{ch.data}\x00{ch.fg}\x00{ch.bg}\x00{int(ch.bold)}\x00")
        out.append("\n")
    return "".join(out)


def _load_faces() -> tuple[list[tuple[ImageFont.FreeTypeFont, ...]], list[set[int]]]:
    """The fallback chain, each face paired with the codepoints it covers."""
    from fontTools.ttLib import TTFont

    fonts, coverage = [], []
    for regular, bold in FACES:
        fonts.append(
            (
                ImageFont.truetype(regular, FONT_SIZE),
                ImageFont.truetype(bold, FONT_SIZE),
            )
        )
        chars: set[int] = set()
        for table in TTFont(regular, fontNumber=0)["cmap"].tables:
            chars |= set(table.cmap)
        coverage.append(chars)
    return fonts, coverage


def _face_for(char: str, coverage: list[set[int]]) -> int:
    cp = ord(char)
    for i, chars in enumerate(coverage):
        if cp in chars:
            return i
    return 0


def render(
    snapshot: str,
    cols: int,
    rows: int,
    fonts: list[tuple[ImageFont.FreeTypeFont, ...]],
    coverage: list[set[int]],
    caption: str = "",
) -> Image.Image:
    """One screen snapshot as an image.

    Cells are grouped into runs sharing a colour, weight and face, and each
    run is drawn as a single string. Drawing character by character at an
    integer cell width leaves a hairline gap between glyphs — invisible in
    prose, obvious across a progress bar made of `━`, which turns into a
    dashed line.
    """
    cw = fonts[0][0].getlength("M")
    ch = FONT_SIZE + 3
    pad = 12
    # The caption gets its own row *below* the content rather than being
    # drawn over the last one: the crop already sized `rows` to what the
    # display used, and provenance must not cover pixels it attests to.
    extra = ch + 4 if caption else 0
    img = Image.new(
        "RGB", (int(cols * cw) + pad * 2, rows * ch + pad * 2 + extra), BACKGROUND
    )
    draw = ImageDraw.Draw(img)
    if caption:
        draw.text(
            (pad, pad + rows * ch + 4),
            caption,
            font=fonts[0][0],
            fill=NAMED["brightblack"],
        )

    for y, line in enumerate(snapshot.split("\n")[:rows]):
        cells = line.split("\x00")
        # Runs of cells sharing a style, collected as plain data and drawn
        # afterwards. A closure over the loop variables would be shorter and
        # is the classic late-binding trap, so the two phases stay separate.
        merged: list[tuple[int, int, str, str, str, int, list[str]]] = []
        for x in range(cols):
            base = x * 4
            if base + 3 >= len(cells):
                break
            data, fg, bg, is_bold = cells[base : base + 4]
            char = data or " "
            face = _face_for(char, coverage)
            style = (fg, bg, is_bold, face)
            if merged and merged[-1][2:6] == style:
                merged[-1] = (*merged[-1][:1], x + 1, *style, [*merged[-1][6], char])
            else:
                merged.append((x, x + 1, *style, [char]))

        for begin, stop, fg, bg, is_bold, face, chars in merged:
            px, py = pad + begin * cw, pad + y * ch
            if bg != "default":
                draw.rectangle(
                    [px, py, px + (stop - begin) * cw, py + ch],
                    fill=_colour(bg, BACKGROUND),
                )
            text = "".join(chars)
            if text.strip():
                draw.text(
                    (px, py),
                    text,
                    font=fonts[face][1 if is_bold == "1" else 0],
                    fill=_colour(fg, FOREGROUND),
                )
    return img


def _scenario_names(root: Path) -> list[str]:
    """The demo's own scenario list, imported rather than restated here.

    Without it a typo is recorded rather than reported: the child starts, prints
    argparse's usage message and exits, and the pty faithfully turns that into a
    GIF. Importing keeps the two lists from drifting, which a copy would not.
    """
    sys.path.insert(0, str(root / "examples"))
    try:
        import demo
    finally:
        sys.path.pop(0)
    return [scenario.name for scenario in demo.SCENARIOS]


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument(
        "scenario", default="pipeline", nargs="?", choices=_scenario_names(root)
    )
    parser.add_argument("--out", default=None, help="output .gif path")
    parser.add_argument("--cols", type=int, default=88)
    parser.add_argument("--rows", type=int, default=14)
    parser.add_argument("--fps", type=int, default=10)
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=0.0,
        help="trim the recording to this length; 0 keeps all of it",
    )
    parser.add_argument(
        "--stop-at",
        default="=== ",
        help=(
            "end the recording at the first frame containing this text. "
            "Defaults to the demo's post-shutdown summary header: the live "
            "display is the subject, and the summary that scrolls after it "
            "both lengthens the loop and stretches the frame to the full "
            "terminal height. Pass an empty string to keep everything."
        ),
    )
    parser.add_argument(
        "--caption",
        default="",
        help=(
            "a dim provenance line rendered under every frame, outside the "
            "recorded area. CI passes the source commit, so the published "
            "gif states in its own pixels which trunk commit it shows — a "
            "claim that survives caching proxies, downloads and hotlinks, "
            "where a caption in surrounding markup would not."
        ),
    )
    parser.add_argument(
        "--require",
        default="",
        help=(
            "fail unless some frame's text contains this string. CI passes "
            "the bar glyph '━': a recording can complete perfectly while "
            "filming the *plain* renderer's scrolling fallback — that is "
            "what a missing `rich` degrades to, by design — and no file-size "
            "or exit-code check can tell that gif from the real display. "
            "This one shipped once."
        ),
    )
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else root / "docs" / f"demo-{args.scenario}.gif"
    out.parent.mkdir(parents=True, exist_ok=True)

    frames = capture(
        [sys.executable, str(root / "examples" / "demo.py"), args.scenario],
        args.cols,
        args.rows,
        args.fps,
        args.stop_at,
    )
    if args.require and not any(
        args.require in "".join(line.split("\x00")[0::4])
        for _, snap in frames
        for line in snap.split("\n")
    ):
        print(
            f"no frame ever contained {args.require!r} — the live display "
            "did not draw (is rich installed in this environment?)",
            file=sys.stderr,
        )
        return 1
    if args.max_seconds:
        frames = [f for f in frames if f[0] <= args.max_seconds]
    if args.stop_at and frames:
        # The frame that first showed the marker is the one to drop.
        frames = frames[:-1] or frames

    # Collapse runs of identical screens: hold the one frame for longer rather
    # than writing the same pixels again. A display that is mostly static
    # between ticks would otherwise dominate the file size.
    kept: list[tuple[str, float]] = []
    for i, (at, snap) in enumerate(frames):
        nxt = frames[i + 1][0] if i + 1 < len(frames) else at + 1.0 / args.fps
        hold = max(nxt - at, 1.0 / args.fps)
        if kept and kept[-1][0] == snap:
            kept[-1] = (snap, kept[-1][1] + hold)
        else:
            kept.append((snap, hold))

    # Crop to the rows anything ever occupied. The pty needs enough height
    # that rich does not crop the display itself, but a fixed height then
    # leaves a band of dead background under a short run — and how tall the
    # display gets is a property of the scenario, not something to guess at
    # per invocation.
    used = 0
    for snap, _ in kept:
        for y, line in enumerate(snap.split("\n")[: args.rows]):
            if "".join(line.split("\x00")[0::4]).strip():
                used = max(used, y + 1)
    height = max(used, 1)

    fonts, coverage = _load_faces()
    images = [
        render(snap, args.cols, height, fonts, coverage, args.caption)
        for snap, _ in kept
    ]
    if not images:
        print("nothing captured", file=sys.stderr)
        return 1
    # The last frame lingers, so the loop does not snap away from the result.
    durations = [max(int(d * 1000), 40) for _, d in kept]
    durations[-1] = max(durations[-1], 1800)

    images[0].save(
        out,
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    size = out.stat().st_size
    try:
        shown: Path | str = out.relative_to(root)
    except ValueError:  # --out pointed somewhere outside the repo
        shown = out
    print(f"{shown}  {len(images)} frames  {size / 1024:,.0f} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
