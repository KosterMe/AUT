"""ffmpeg filter fragments, as strings.

Small, pure, and shared: the clip compiler and the cover renderer frame their
picture exactly the same way, and a cover that crops differently from the video
it belongs to looks like a different video. Keeping the two chains in one place
is what stops them drifting apart.

Nothing here runs a process or reads a file — only configuration.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from montage.config import get_settings


def path(value: str) -> str:
    """A filesystem path as a filter argument.

    Windows drive letters are the reason this exists: an unescaped `C:` reads
    as an option separator inside a filter graph.
    """
    return value.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def flt(value: float) -> str:
    return f"{float(value):.3f}"


def polyline(points: "Sequence[tuple[float, float]]", *, clock: str = "t") -> str:
    """A piecewise-linear function of time, as an ffmpeg expression.

    `clock` is the name the filter gives the current second. Almost everything
    calls it `t`; `geq` calls it `T`, and a curve handed to `geq` under the
    name `t` is not rejected — `t` there is the pixel index — so the wrong one
    renders a picture rather than an error.

    This is the one construction §7.2 measured as actually animating: an
    expression in `t` handed to `overlay`, evaluated on every frame. The curve
    arrives already sampled — easing applied, keyframes resolved — so there is
    one builder here rather than one per easing.

    Held at both ends, and a stretch ends the moment the next one starts:
    `lt(t, …)` means the instant of a jump belongs to the value after it,
    which is the rule `curve.value_at` follows so the editor draws the frame
    that will be rendered.
    """
    usable = [(float(at), float(value)) for at, value in points]
    if not usable:
        return ""
    if len(usable) == 1:
        return flt(usable[0][1])

    expression = flt(usable[-1][1])
    # Built from the end backwards, so each stretch wraps the ones after it.
    for (start, from_value), (end, to_value) in reversed(list(zip(usable, usable[1:]))):
        span = end - start
        if span <= 0:
            # Two points on the same second: a jump, and the `lt` of the
            # stretch before it already decides which side of it we are on.
            continue
        slope = (to_value - from_value) / span
        moving = (
            f"{flt(from_value)}+({flt(slope)})*({clock}-{flt(start)})"
            if slope else flt(from_value)
        )
        expression = f"if(lt({clock},{flt(end)}),{moving},{expression})"
    first = usable[0][0]
    return f"if(lt({clock},{flt(first)}),{flt(usable[0][1])},{expression})"


def painted(paint, canvas, box: "tuple[int, int] | None" = None) -> str:
    """A layer with no file behind it, as a `lavfi` source.

    §7.3 set aside a second renderer for graphics laid over the picture, and
    the two this model can ask for — a flat fill and a line of text — turned
    out not to need one: `color` makes the first, `drawtext` on a transparent
    ground makes the second, and this build has both (trap 59).

    A fill is drawn at canvas size and a caption at the size of its own box,
    and the difference is not tidiness. Everything the layer chain does after
    this scales the source into the layer's rectangle — which is harmless for a
    flat colour and ruinous for type: a full-canvas ground squeezed into a
    rectangle a seventh as tall takes the letters down with it, and the caption
    arrives four pixels high (trap 61). Drawn at the box, the scale is a no-op
    at rest; when the box animates, the type grows with it, which is what a
    growing text box should look like.

    Nothing here interprets the text: it arrives substituted, because an EDL
    still holding `{title}` would be a plan rather than a description.
    """
    if paint.kind == "colour":
        return (
            f"color=c={_colour(paint.colour)}"
            f":s={canvas.width}x{canvas.height}:r={canvas.fps}"
        )

    points = max(8, int(round(canvas.height * paint.size_pct / 100.0)))
    width = max(2, box[0] if box and box[0] else canvas.width)
    # A frame that leaves its height to the aspect ratio has none to take, so
    # the ground is as tall as the type needs and the scale keeps it in
    # proportion from there.
    height = max(2, box[1] if box and box[1] else int(round(points * 1.6)))
    # `format=rgba` on the ground and not later: `color` negotiates its output
    # with whatever comes next, and the next thing is a scale into the layer's
    # box, which settles on yuv420p and throws the alpha away. The transparent
    # ground then arrives as an opaque black rectangle covering whatever the
    # text was meant to sit on (trap 60).
    ground = f"color=c=black@0.0:s={width}x{height}:r={canvas.fps},format=rgba"
    font = _font_file()
    face = f":fontfile='{path(font)}'" if font else ""
    # Centred on the ground, so the layer's own rectangle is what moves it.
    # The outline is the subtitle look's, and it is not decoration: white type
    # on a bright frame is unreadable without it.
    return (
        f"{ground},drawtext=text='{_escaped(paint.text)}'"
        f"{face}:fontsize={points}:fontcolor={_colour(paint.colour)}"
        f":x=(w-text_w)/2:y=(h-text_h)/2"
        f":borderw={max(1, round(points * 0.12))}:bordercolor=black@0.8"
    )


def _colour(value: str) -> str:
    """A `#rrggbb` as ffmpeg writes it."""
    cleaned = (value or "").strip().lstrip("#")
    return f"0x{cleaned}" if cleaned else "black"


def _font_file() -> str | None:
    """The bundled display face, as a file `drawtext` can open.

    `subtitles` speaks to libass, which takes a family name and a directory to
    look in; `drawtext` takes a path. Same font, two ways of naming it.
    """
    directory = fonts_dir()
    if not directory:
        return None
    faces = sorted(Path(directory).glob("*.ttf"))
    return str(faces[0]) if faces else None


def _escaped(text: str) -> str:
    """Text as a `drawtext` argument.

    Four characters have to go: the backslash first or it would escape the
    escapes, then the quote that ends the option, the colon that separates
    options, and the percent sign `drawtext` reads as a strftime directive —
    which is how a caption saying "100%" turns into a caption saying "100" and
    the date.
    """
    out = text.replace("\\", "\\\\")
    for character in ("'", ":", "%", ","):
        out = out.replace(character, "\\" + character)
    return out.replace("\n", "\\n")


def fonts_dir() -> str | None:
    """Directory of bundled display fonts (Oswald, …) for libass to load.

    Points ffmpeg's `subtitles` filter at a font shipped with the repo, so the
    on-screen headline renders identically on Windows and in the Linux
    container — neither has the font installed system-wide.
    """
    configured = get_settings().paths.fonts_dir
    if configured.is_dir():
        return str(configured.resolve())
    bundled = Path(__file__).resolve().parents[3] / "assets" / "fonts"
    return str(bundled) if bundled.is_dir() else None


def fontsdir_arg() -> str:
    directory = fonts_dir()
    return f":fontsdir='{path(directory)}'" if directory else ""


def background(
    width: int, height: int, *, src: str, out: str, divisor: int, radius: float = 24.0
) -> str:
    """The blurred backdrop behind a letterboxed picture.

    Blurring 1080x1920 directly is the single most expensive filter in the
    chain. Shrinking first, blurring, then scaling back up costs roughly
    `divisor` squared less work and looks the same — a heavy blur discards the
    detail the downscale removed anyway. The radius is divided to match, so the
    result keeps the same visual softness.
    """
    if divisor <= 1:
        return (
            f"{src}scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur={max(1, round(radius))}:1[{out}]"
        )
    small_width = max(2, (width // divisor) // 2 * 2)
    small_height = max(2, (height // divisor) // 2 * 2)
    scaled_radius = max(1, round(radius / divisor))
    return (
        f"{src}scale={small_width}:{small_height}:force_original_aspect_ratio=increase,"
        f"crop={small_width}:{small_height},boxblur={scaled_radius}:1,"
        f"scale={width}:{height}[{out}]"
    )


def foreground(width: int, height: int, *, src: str, out: str, zoom: float) -> str:
    """The sharp layer sitting on top of the blurred backdrop.

    At zoom 1.0 the entire source frame is fitted inside the canvas: a 16:9
    clip becomes 1080x608 and the remaining two thirds of a 1920px-tall phone
    screen are blur. Zooming in scales the picture past the canvas width and
    crops the overflow off the left and right edges, so the visible video grows
    vertically by the zoom factor and loses `(zoom-1)/zoom` of its width.

    The crop is clamped with min() because the enlarged frame is not always
    wider than the canvas — a source that is already portrait hits the height
    limit first, and cropping it to a width it does not have would fail.
    """
    if zoom <= 1.0:
        return f"{src}scale={width}:{height}:force_original_aspect_ratio=decrease[{out}]"
    box_width = max(2, int(round(width * zoom)) // 2 * 2)
    return (
        f"{src}scale={box_width}:{height}"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"crop=w=min(iw\\,{width}):h=min(ih\\,{height})[{out}]"
    )


def fill(width: int, height: int, *, src: str, out: str) -> str:
    """Crop a source to fill the canvas outright, with no backdrop.

    For sources already shot vertical, where a blurred border would only add
    letterboxing to a picture that does not need it.
    """
    return (
        f"{src}scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}[{out}]"
    )
