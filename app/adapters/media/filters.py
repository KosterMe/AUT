"""ffmpeg filter fragments, as strings.

Small, pure, and shared: the clip compiler and the cover renderer frame their
picture exactly the same way, and a cover that crops differently from the video
it belongs to looks like a different video. Keeping the two chains in one place
is what stops them drifting apart.

Nothing here runs a process or reads a file — only configuration.
"""
from __future__ import annotations

from pathlib import Path

from app.core.config import get_settings


def path(value: str) -> str:
    """A filesystem path as a filter argument.

    Windows drive letters are the reason this exists: an unescaped `C:` reads
    as an option separator inside a filter graph.
    """
    return value.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def flt(value: float) -> str:
    return f"{float(value):.3f}"


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
