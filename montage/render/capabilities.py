"""What this ffmpeg build can actually do, measured rather than looked up.

The animation table in `docs/montage-service.md` §7.2 decides what the editor is
allowed to offer: a property that cannot be driven by an expression must not get
a keyframe track. That table began as a reading of the ffmpeg documentation, and
the documentation describes a moving target — Debian bookworm, a Windows build
and somebody's local 4.x are three different filter sets with three different
option lists.

So nothing here is declared. Every construction is handed to the ffmpeg that is
actually installed, rendered, and judged on what came out the other end.

The distinction a bare exit code cannot make is the one that matters:

* **rejected** — the filter refused the construction. Loud, and therefore cheap:
  the render fails and somebody looks at it.
* **frozen** — the filter accepted the construction and then rendered the same
  picture for every frame. Silent, and therefore expensive: the render succeeds
  and the animation is simply missing. `zoompan`'s usual accumulator idiom does
  exactly this, which is the reason this module renders pixels instead of
  reading exit codes.

Telling those apart needs the frames, so every picture check decodes to raw RGB
and counts how many frames differ. The source is `smptebars`, which does not
move, so any difference between frames is the filter's doing and nothing else's.

The result is what `GET /api/capabilities` serves (§2.3): the editor asks once at
startup and stops offering what this build cannot deliver.

Nothing here touches the media tree — every input is synthesised by `lavfi`.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from montage.render import filters
from montage.render.probe import ffmpeg_exe

log = logging.getLogger(__name__)

# What a check asks of ffmpeg.
PICTURE = "picture"     # render frames, count the ones that differ
SOUND = "sound"         # render PCM, measure how long it came out
RUN = "run"             # exit code only: does this build accept the construction
TIMING = "timing"       # what the construction costs on a real canvas

# What came back. The first three are the answers the editor needs.
SMOOTH = "smooth"       # a different picture on every probed frame
STEPPED = "stepped"     # changes, but holds still for runs of frames
FROZEN = "frozen"       # accepted and rendered nothing: the silent failure
REJECTED = "rejected"   # ffmpeg refused the construction
OK = "ok"               # non-picture check that ran

# A construction that animates is worth offering as a keyframe track; one that
# only steps is worth offering as an on/off switch; the rest are not offerable.
OFFERABLE = (SMOOTH, STEPPED, OK)

SAMPLE_RATE = 8000
BYTES_PER_SAMPLE = 2

# Timing runs are short enough that the scheduler shows up in the number, so
# each is measured a few times and the best is taken. The minimum is the right
# statistic for a wall-time benchmark: it is the run least contaminated by
# everything else on the machine, and it is the one that reproduces.
TIMING_REPEATS = 3


@dataclass(frozen=True)
class Check:
    """One construction, and how to judge what it produced."""

    key: str
    label: str
    construction: str
    graph: str
    kind: str = PICTURE
    # lavfi sources, in the order the graph refers to them as [0:v], [1:v], ...
    sources: tuple[str, ...] = ("smptebars=size=64x64:rate=12:duration=1",)
    width: int = 64
    height: int = 64
    frames: int = 12
    # SOUND only: seconds the input runs for, so the check can say what the
    # filter did to the length rather than just that it ran.
    seconds: float = 2.0
    note: str = ""


@dataclass(frozen=True)
class Finding:
    """What one check found on this build."""

    key: str
    label: str
    construction: str
    verdict: str
    detail: str = ""
    frames: int = 0
    distinct: int = 0
    seconds: float = 0.0
    # Wall time relative to a bare passthrough of the same size and length.
    # `rotate` being expensive is a claim worth a number rather than a word.
    cost: float = 0.0
    note: str = ""

    @property
    def offerable(self) -> bool:
        return self.verdict in OFFERABLE


@dataclass(frozen=True)
class Report:
    """Everything this build answered, in the order it was asked."""

    build: str = ""
    ffmpeg: str = ""
    findings: tuple[Finding, ...] = ()
    ok: bool = True
    detail: str = ""

    def by_key(self, key: str) -> Finding | None:
        return next((f for f in self.findings if f.key == key), None)

    def as_dict(self) -> dict:
        """The payload `GET /api/capabilities` returns."""
        return {
            "ok": self.ok,
            "build": self.build,
            "ffmpeg": self.ffmpeg,
            "detail": self.detail,
            "capabilities": {
                f.key: {
                    "label": f.label,
                    "construction": f.construction,
                    "verdict": f.verdict,
                    "offerable": f.offerable,
                    "detail": f.detail,
                    "note": f.note,
                }
                for f in self.findings
            },
        }


# A source that does not move. Any difference between two output frames is
# then the filter's doing, which is the whole basis of the picture checks.
BARS = "smptebars=size=64x64:rate=12:duration=1"
PATCH = "color=c=red:size=16x16:rate=12:duration=1"
FULL_PATCH = "color=c=red:size=64x64:rate=12:duration=1"
TONE = "sine=frequency=440:duration=2"
NOISE = "anoisesrc=duration=2:amplitude=0.3"

# zoompan is measured on a canvas with enough pixels for a slow zoom to show.
# At 64x64 a 0.15%-per-frame zoom is sub-pixel and every build looks frozen —
# which would have recorded a real defect for the wrong reason.
PAN_W, PAN_H, PAN_N = 360, 640, 24
PAN_SRC = f"smptebars=size={PAN_W}x{PAN_H}:rate={PAN_N}:duration=1"
# A layer for the same canvas, at the same rate. A slower source would update
# every second frame and the verdict would read "stepped" about the rates
# rather than about the filter.
PAN_LAYER = f"smptebars=size=128x128:rate={PAN_N}:duration=1"

ANIMATION: tuple[Check, ...] = (
    Check(
        key="position",
        label="позиция x, y",
        construction="overlay=x='…t…':y='…t…'",
        graph="[0:v][1:v]overlay=x='4+40*t':y='4+40*t'",
        sources=(BARS, PATCH),
    ),
    Check(
        key="rotation",
        label="поворот",
        construction="rotate=a='…t…'",
        graph="[0:v]rotate=a='0.8*t':fillcolor=black",
    ),
    Check(
        key="crop",
        label="обрезка",
        construction="crop=w:h:x='…t…':y='…t…'",
        graph="[0:v]crop=32:32:x='4+28*t':y=0,scale=64:64",
        note="x/y пересчитываются на каждом кадре сами; опции eval у фильтра нет",
    ),
    Check(
        key="crop_eval_frame",
        label="обрезка через eval=frame",
        construction="crop=…:eval=frame",
        graph="[0:v]crop=32:32:x='4+28*t':y=0:eval=frame,scale=64:64",
    ),
    Check(
        key="color",
        label="цвет, яркость",
        construction="eq=…:eval=frame",
        graph="[0:v]eq=brightness='-0.4+0.8*t':eval=frame",
    ),
    Check(
        key="scale",
        label="масштаб",
        construction="zoompan=z='…on…'",
        graph=f"[0:v]zoompan=z='1+0.4*on/{PAN_N}':d=1:s={PAN_W}x{PAN_H}:fps={PAN_N}",
        sources=(PAN_SRC,),
        width=PAN_W,
        height=PAN_H,
        frames=PAN_N,
        note="выражение пишется через on — номер выходного кадра, а не через секунды",
    ),
    Check(
        key="scale_accumulator",
        label="масштаб через накопитель zoom",
        construction="zoompan=z='min(zoom+0.0015,1.5)'",
        graph=f"[0:v]zoompan=z='min(zoom+0.0015,1.5)':d=1:s={PAN_W}x{PAN_H}:fps={PAN_N}",
        sources=(PAN_SRC,),
        width=PAN_W,
        height=PAN_H,
        frames=PAN_N,
    ),
    Check(
        key="scale_eval_frame",
        label="масштаб через scale",
        construction="scale=w='…t…':eval=frame",
        graph=(
            f"[0:v]scale=w='{PAN_W//4}+{PAN_W//2}*t':h=-2:eval=frame,"
            f"pad={PAN_W}:{PAN_H}:0:0:black"
        ),
        sources=(PAN_SRC,),
        width=PAN_W,
        height=PAN_H,
        frames=PAN_N,
        note="размер слоя меняется на каждом кадре, а не только на старте",
    ),
    Check(
        key="scale_and_move",
        label="масштаб вместе с позицией",
        construction="scale=…:eval=frame + overlay=x='…w…'",
        graph=(
            "[1:v]scale=w='8+40*t':h=-2:eval=frame[l];"
            "[0:v][l]overlay=x='4+40*t-w/2':y='32-h/2'"
        ),
        sources=(BARS, PATCH),
        note="overlay видит w/h слоя на текущем кадре, поэтому центрировать можно им же",
    ),
    Check(
        key="rotation_box_frame",
        label="поворот с коробкой по кадру",
        construction="rotate=…:ow=rotw(…t…)",
        graph=(
            "[1:v]format=rgba,rotate=a='0.6*t':ow=rotw(0.6*t):oh=roth(0.6*t):c=none[l];"
            "[0:v][l]overlay=x='(W-w)/2':y='(H-h)/2'"
        ),
        sources=(BARS, PATCH),
    ),
    Check(
        key="rotation_box_max",
        label="поворот с коробкой на предельный угол",
        construction="rotate=a='…t…':ow=rotw(A):oh=roth(A):c=none",
        graph=(
            "[1:v]format=rgba,rotate=a='0.6*t':ow=rotw(0.6):oh=roth(0.6):c=none[l];"
            "[0:v][l]overlay=x='(W-w)/2':y='(H-h)/2'"
        ),
        # A detailed layer rather than a flat one, and a canvas big enough for
        # the first degree of turn to move a pixel: on a 16×16 patch the first
        # two frames come out identical and the verdict would say "stepped"
        # about the measurement rather than about ffmpeg.
        sources=(PAN_SRC, PAN_LAYER),
        width=PAN_W,
        height=PAN_H,
        frames=PAN_N,
        note="ow/oh считаются один раз на старте, где t ещё нет — коробку берём по "
             "максимальному углу кривой",
    ),
    Check(
        key="opacity_fade",
        label="прозрачность на вход/выход",
        construction="fade=alpha=1",
        graph="[1:v]format=rgba,fade=t=in:st=0:d=1:alpha=1[l];[0:v][l]overlay",
        sources=(BARS, FULL_PATCH),
    ),
    Check(
        key="opacity_curve",
        label="прозрачность произвольной кривой",
        construction="geq=a='…T…'",
        graph=(
            "[1:v]format=rgba,"
            "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='255*(0.1+0.8*T)'[l];"
            "[0:v][l]overlay"
        ),
        sources=(BARS, FULL_PATCH),
    ),
    Check(
        key="opacity_colorchannelmixer",
        label="прозрачность через colorchannelmixer",
        construction="colorchannelmixer=aa='…t…':eval=frame",
        graph="[1:v]format=rgba,colorchannelmixer=aa='0.1+0.8*t':eval=frame[l];[0:v][l]overlay",
        sources=(BARS, FULL_PATCH),
    ),
    Check(
        key="blur",
        label="размытие выражением",
        construction="gblur=sigma='…t…'",
        graph="[0:v]gblur=sigma='2*t'",
    ),
    Check(
        key="blur_boxblur",
        label="размытие boxblur выражением",
        construction="boxblur='…t…'",
        graph="[0:v]boxblur='2*t'",
    ),
    Check(
        key="blur_stepped",
        label="размытие ступенькой",
        construction="gblur=sigma=N:enable='between(t,…)'",
        graph="[0:v]gblur=sigma=6:enable='between(t,0.3,0.7)'",
    ),
    Check(
        key="gate",
        label="включение слоя на окно",
        construction="overlay=…:enable='between(t,…)'",
        graph="[0:v][1:v]overlay=enable='between(t,0.3,0.7)'",
        sources=(BARS, PATCH),
    ),
    Check(
        key="speed_ramp",
        label="speed ramp",
        construction="setpts='…PTS…'",
        graph="[0:v]setpts='PTS*PTS/TB/60'",
        kind=RUN,
        note="разбирается, но setpts двигает только картинку — звук за ней не идёт",
    ),
)

# §7.1's chain and the build-dependent traps of §10, settled the same way.
CHAIN: tuple[Check, ...] = (
    Check(
        key="xfade",
        label="xfade на совпадающих входах",
        construction="xfade=transition=fade",
        graph="[0:v][1:v]xfade=transition=fade:duration=0.5:offset=0.4",
        kind=RUN,
        sources=(BARS, BARS),
    ),
    Check(
        key="xfade_size_mismatch",
        label="xfade при разном размере",
        construction="xfade на 64x64 и 32x32",
        graph="[0:v][1:v]xfade=transition=fade:duration=0.5:offset=0.4",
        kind=RUN,
        sources=(BARS, "smptebars=size=32x32:rate=12:duration=1"),
    ),
    Check(
        key="xfade_fps_mismatch",
        label="xfade при разном fps",
        construction="xfade на 12 и 25 fps",
        graph="[0:v][1:v]xfade=transition=fade:duration=0.5:offset=0.4",
        kind=RUN,
        sources=(BARS, "smptebars=size=64x64:rate=25:duration=1"),
    ),
    Check(
        key="xfade_format_mismatch",
        label="xfade при разном формате",
        construction="xfade после format=gray",
        graph="[1:v]format=gray[g];[0:v][g]xfade=transition=fade:duration=0.5:offset=0.4",
        kind=RUN,
        sources=(BARS, BARS),
    ),
    Check(
        key="atempo_double",
        label="atempo=2.0",
        construction="atempo=2.0",
        graph="[0:a]atempo=2.0",
        kind=SOUND,
        sources=(TONE,),
    ),
    Check(
        key="atempo_triple",
        label="atempo=3.0 одним фильтром",
        construction="atempo=3.0",
        graph="[0:a]atempo=3.0",
        kind=SOUND,
        sources=(TONE,),
    ),
    Check(
        key="atempo_slow",
        label="atempo=0.4 одним фильтром",
        construction="atempo=0.4",
        graph="[0:a]atempo=0.4",
        kind=SOUND,
        sources=(TONE,),
    ),
    Check(
        key="atempo_chain",
        label="atempo цепочкой",
        construction="atempo=0.5,atempo=0.8",
        graph="[0:a]atempo=0.5,atempo=0.8",
        kind=SOUND,
        sources=(TONE,),
    ),
    Check(
        key="loudnorm",
        label="loudnorm в один проход",
        construction="loudnorm=I=-16:TP=-1.5:LRA=11",
        graph="[0:a]loudnorm=I=-16:TP=-1.5:LRA=11",
        kind=SOUND,
        sources=(TONE,),
    ),
    Check(
        key="sidechaincompress",
        label="музыка под речь",
        construction="sidechaincompress",
        graph="[0:a][1:a]sidechaincompress=threshold=0.05:ratio=8",
        kind=SOUND,
        sources=(NOISE, TONE),
    ),
    Check(
        key="subtitles",
        label="прожиг субтитров",
        construction="subtitles=…:fontsdir=…",
        graph="[0:v]subtitles='{ass}'",
        kind=RUN,
    ),
    Check(
        key="drawtext",
        label="текст фильтром",
        construction="drawtext=text=…",
        graph="[0:v]drawtext=text='probe':fontcolor=white:x=4:y=4",
        kind=RUN,
    ),
)

# "Expensive" is not a fact until it is a number, and the number only appears on
# a canvas where the filter costs more than starting the process does. These run
# at the size this project actually renders, and are reported as a multiple of a
# bare passthrough of the same length.
COST_W, COST_H, COST_N = 1080, 1920, 12
COST_SRC = f"smptebars=size={COST_W}x{COST_H}:rate={COST_N}:duration=1"


def _cost(key: str, label: str, construction: str, graph: str) -> Check:
    return Check(
        key=key, label=label, construction=construction, graph=graph,
        kind=TIMING, sources=(COST_SRC,),
        width=COST_W, height=COST_H, frames=COST_N,
    )


COST: tuple[Check, ...] = (
    _cost("cost_rotation", "поворот", "rotate=a='…t…'",
          "[0:v]rotate=a='0.8*t':fillcolor=black"),
    _cost("cost_opacity_curve", "прозрачность кривой", "geq=a='…T…'",
          "[0:v]format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='255*(0.1+0.8*T)'"),
    _cost("cost_scale", "масштаб", "zoompan=z='…on…'",
          f"[0:v]zoompan=z='1+0.4*on/{COST_N}':d=1:s={COST_W}x{COST_H}:fps={COST_N}"),
    _cost("cost_scale_eval", "масштаб через scale", "scale=w='…t…':eval=frame",
          f"[0:v]scale=w='{COST_W // 2}+{COST_W // 4}*t':h=-2:eval=frame,"
          f"pad={COST_W}:{COST_H}:0:0:black"),
    _cost("cost_blur", "размытие", "gblur=sigma=6",
          "[0:v]gblur=sigma=6"),
    _cost("cost_look", "look целиком", "eq,unsharp,vignette,format",
          "[0:v]eq=contrast=1.1,unsharp=5:5:0.8,vignette,format=yuv420p"),
    _cost("cost_subtitles", "прожиг субтитров", "subtitles=…",
          "[0:v]subtitles='{ass}'"),
)


CHECKS: tuple[Check, ...] = ANIMATION + CHAIN + COST

# A minimal subtitle file, so the libass check exercises the real code path
# rather than the filter's presence in `-filters`.
ASS = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, Alignment
Style: Default,Sans,16,&H00FFFFFF,2

[Events]
Format: Layer, Start, End, Style, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,probe
"""


_ADDRESS = re.compile(r" @ 0x[0-9a-f]+")
_TAG = re.compile(r"^(\[[^]]+\]\s*)+")


def reason(stderr: bytes) -> str:
    """The one line of ffmpeg's complaint worth recording.

    Two things are stripped and nothing else is touched — the words are the
    evidence and paraphrasing them would put us back to describing ffmpeg
    instead of asking it:

    * filter instance addresses, which differ on every run, and a detail that
      differs on every run cannot be compared against the last one;
    * the `[Parsed_gblur_0]` tags, which name a filter the row already names.
    """
    for raw in stderr.decode("utf-8", "replace").splitlines():
        line = _TAG.sub("", _ADDRESS.sub("", raw)).strip()
        if line:
            return line
    return "no output"


def command(ffmpeg: str, check: Check, graph: str) -> list[str]:
    """The ffmpeg run one check needs, which differs by what it has to measure.

    A picture check needs the frames themselves; a timing check must not pipe
    them, because at 1080x1920 the copying costs more than the filter does.
    """
    argv = [ffmpeg, "-hide_banner", "-v", "error", "-nostdin"]
    for source in check.sources:
        argv += ["-f", "lavfi", "-i", source]
    argv += ["-filter_complex", graph]

    if check.kind == SOUND:
        argv += ["-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    elif check.kind == PICTURE:
        argv += ["-frames:v", str(check.frames),
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    else:
        argv += ["-frames:v", str(check.frames), "-f", "null", "-"]
    return argv


def split_frames(blob: bytes, width: int, height: int) -> list[bytes]:
    """Raw rgb24 output cut into frames, dropping any partial tail."""
    size = width * height * 3
    return [blob[i * size:(i + 1) * size] for i in range(len(blob) // size)]


def judge(frames: list[bytes]) -> str:
    """How a sequence of rendered frames answers "does this animate?".

    One distinct frame means the construction was accepted and did nothing —
    the failure that does not announce itself. Everything changing means a
    keyframe track is safe to offer. Anything between is a step function, good
    for an on/off switch and misleading as a curve.
    """
    distinct = len(set(frames))
    if distinct <= 1:
        return FROZEN
    return SMOOTH if distinct == len(frames) else STEPPED


def _measure(ffmpeg: str, check: Check, graph: str, timeout: float) -> tuple[int, bytes, bytes, float]:
    started = time.monotonic()
    try:
        done = subprocess.run(
            command(ffmpeg, check, graph), capture_output=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return 1, b"", b"timed out", time.monotonic() - started
    except OSError as exc:  # ffmpeg vanished between the lookup and the run
        return 1, b"", str(exc).encode(), time.monotonic() - started
    return done.returncode, done.stdout, done.stderr, time.monotonic() - started


def _best(ffmpeg: str, check: Check, graph: str,
          timeout: float) -> tuple[int, float]:
    """Exit code and the fastest of several runs of the same construction."""
    results = [_measure(ffmpeg, check, graph, timeout)
               for _ in range(TIMING_REPEATS)]
    failed = next((r for r in results if r[0] != 0), None)
    return (failed[0], failed[3]) if failed else (0, min(r[3] for r in results))


def _baseline(ffmpeg: str, check: Check, timeout: float, cache: dict) -> float:
    """Wall time for the same frames with no filtering, for comparison.

    The bare run has to end in the same sink as the check it is compared with:
    piping 74MB of raw 1080x1920 frames costs more than most filters do, and a
    baseline that pays for it reports filters as faster than doing nothing.

    Best of several taken, like the check it is divided into: the first run at
    a new frame size pays for allocations the later ones find already made.
    """
    key = (check.kind, check.width, check.height, check.frames)
    if key not in cache:
        bare = Check(
            key="baseline", label="", construction="", graph="[0:v]null",
            kind=check.kind, sources=(check.sources[0],), width=check.width,
            height=check.height, frames=check.frames,
        )
        cache[key] = max(_best(ffmpeg, bare, bare.graph, timeout)[1], 1e-6)
    return cache[key]


def _examine(ffmpeg: str, check: Check, graph: str, timeout: float, cache: dict) -> Finding:
    code, out, err, elapsed = _measure(ffmpeg, check, graph, timeout)
    base = dict(key=check.key, label=check.label,
                construction=check.construction, note=check.note)

    if check.kind == RUN:
        return Finding(**base, verdict=OK if code == 0 else REJECTED,
                       detail="" if code == 0 else reason(err))

    if check.kind == SOUND:
        samples = len(out) // BYTES_PER_SAMPLE
        if code != 0 or not samples:
            return Finding(**base, verdict=REJECTED, detail=reason(err))
        return Finding(**base, verdict=OK, seconds=samples / SAMPLE_RATE)

    if check.kind == TIMING:
        if code != 0:
            return Finding(**base, verdict=REJECTED, detail=reason(err))
        _, best = _best(ffmpeg, check, graph, timeout)
        return Finding(**base, verdict=OK,
                       cost=best / _baseline(ffmpeg, check, timeout, cache))

    frames = split_frames(out, check.width, check.height)
    if code != 0 or not frames:
        return Finding(**base, verdict=REJECTED, detail=reason(err))
    return Finding(**base, verdict=judge(frames),
                   frames=len(frames), distinct=len(set(frames)))


def _build(ffmpeg: str, timeout: float) -> str:
    try:
        done = subprocess.run([ffmpeg, "-version"], capture_output=True,
                              text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return "unknown"
    first = done.stdout.splitlines()[0] if done.stdout else ""
    return first.replace("ffmpeg version ", "").split(" Copyright")[0].strip() or "unknown"


def probe(checks: tuple[Check, ...] = CHECKS, *, timeout: float = 60.0) -> Report:
    """Run every construction through the installed ffmpeg.

    Never raises. A build that cannot be found or cannot run produces a report
    that says so, because the editor asking what is available must get an
    answer either way.
    """
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        return Report(ok=False, detail="no ffmpeg on this machine")

    cache: dict = {}
    findings: list[Finding] = []
    with tempfile.TemporaryDirectory(prefix="ffprobe-caps-") as workdir:
        ass = Path(workdir) / "probe.ass"
        ass.write_text(ASS, encoding="utf-8")
        escaped = filters.path(str(ass))
        for check in checks:
            graph = check.graph.replace("{ass}", escaped)
            findings.append(_examine(ffmpeg, check, graph, timeout, cache))

    report = Report(build=_build(ffmpeg, timeout), ffmpeg=ffmpeg,
                    findings=tuple(findings))
    log.info("ffmpeg %s: %d of %d constructions usable", report.build,
             sum(1 for f in findings if f.offerable), len(findings))
    return report


@lru_cache(maxsize=1)
def capabilities() -> Report:
    """The probe, run once per process — what `GET /api/capabilities` answers.

    Cached for the same reason `encoders.is_usable` is: the answer cannot
    change while the process lives, and two dozen ffmpeg runs is not something
    to repeat on every request.
    """
    return probe()


# What each verdict means where it is read: the doc table, and the editor.
PHRASE = {
    SMOOTH: "анимируется",
    STEPPED: "только ступенькой",
    FROZEN: "принимается и не анимирует",
    REJECTED: "отвергается",
    OK: "работает",
}


def _fact(finding: Finding) -> str:
    """One cell: the verdict, plus whichever measurement produced it."""
    if finding.verdict == REJECTED:
        return f"{PHRASE[REJECTED]} — {finding.detail}" if finding.detail else PHRASE[REJECTED]
    phrase = PHRASE.get(finding.verdict, finding.verdict)
    if finding.frames:
        phrase += f", {finding.distinct}/{finding.frames} кадров"
    elif finding.seconds:
        phrase += f", {finding.seconds:.2f} с"
    elif finding.cost:
        phrase = f"×{finding.cost:.1f} к пустому проходу"
    return f"{phrase}; {finding.note}" if finding.note else phrase


def render_markdown(report: Report) -> str:
    """The §7.2 tables, as facts about this build rather than about the manual."""
    groups = (
        ("| Свойство | Конструкция | Факт |\n| --- | --- | --- |", ANIMATION,
         lambda f: f"| {f.label} | `{f.construction}` | {_fact(f)} |"),
        ("| Конструкция цепочки | Факт |\n| --- | --- |", CHAIN,
         lambda f: f"| {f.label} | {_fact(f)} |"),
        (f"| Что | Конструкция | Цена на {COST_W}×{COST_H} |\n| --- | --- | --- |", COST,
         lambda f: f"| {f.label} | `{f.construction}` | {_fact(f)} |"),
    )
    lines = [f"Замерено на `{report.build}`.", ""]
    for header, checks, row in groups:
        keys = {c.key for c in checks}
        lines += [header]
        lines += [row(f) for f in report.findings if f.key in keys]
        lines += [""]
    return "\n".join(lines).rstrip() + "\n"


def render_text(report: Report) -> str:
    """What passed, for a person watching the probe run."""
    if not report.ok:
        return f"ffmpeg capabilities: {report.detail}"

    width = max(len(f.label) for f in report.findings)
    lines = [f"ffmpeg {report.build}", f"  {report.ffmpeg}", ""]
    lines += [
        f"  {'+' if f.offerable else '-'} {f.label:<{width}}  {_fact(f)}"
        for f in report.findings
    ]
    usable = sum(1 for f in report.findings if f.offerable)
    lines += ["", f"  {usable} of {len(report.findings)} constructions usable here"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m app.adapters.media.capabilities",
        description="Run every construction in docs/montage-service.md §7.2 "
                    "through the installed ffmpeg and report what survived.",
    )
    shape = parser.add_mutually_exclusive_group()
    shape.add_argument("--json", action="store_true",
                       help="the payload GET /api/capabilities serves")
    shape.add_argument("--markdown", action="store_true",
                       help="the §7.2 tables, ready to paste into the doc")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="seconds allowed per construction (default: 60)")
    args = parser.parse_args(argv)

    report = probe(timeout=args.timeout)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    elif args.markdown:
        print(render_markdown(report))
    else:
        print(render_text(report))
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
