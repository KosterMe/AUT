"""Choosing a video encoder, preferring hardware when it actually works.

Two things make this less trivial than reading `ffmpeg -encoders`:

* **Listed is not available.** A Windows ffmpeg build lists `h264_nvenc` on a
  machine with no NVIDIA card at all. The only reliable check is to encode a
  few frames and see whether it succeeds, so that is what happens here — once
  per process, cached.
* **Quality is expressed differently everywhere.** x264 has CRF; AMF wants
  CQP, NVENC wants CQ, QSV wants global_quality. `encoder_args` maps one
  quality number onto whichever encoder was chosen, so callers keep thinking
  in CRF.

Hardware encoding only removes the encode step — the filter chain (blur,
sharpen, subtitle burn) still runs on the CPU.
"""
from __future__ import annotations

import logging
import subprocess
from functools import lru_cache

log = logging.getLogger(__name__)

# Tried in this order when the encoder is set to "auto". Ordered by how good
# the output tends to look at a comparable bitrate.
HARDWARE_CANDIDATES = (
    "h264_nvenc",       # NVIDIA
    "h264_qsv",         # Intel Quick Sync
    "h264_amf",         # AMD
    "h264_videotoolbox",  # Apple
)
SOFTWARE_ENCODER = "libx264"


def resolve(preference: str) -> str:
    """The encoder to use. Falls back to libx264 when nothing else works."""
    choice = (preference or "auto").strip().lower()

    if choice in ("", "auto"):
        for candidate in HARDWARE_CANDIDATES:
            if is_usable(candidate):
                log.info("using hardware encoder %s", candidate)
                return candidate
        log.info("no working hardware encoder found; using %s", SOFTWARE_ENCODER)
        return SOFTWARE_ENCODER

    if choice == SOFTWARE_ENCODER:
        return SOFTWARE_ENCODER

    if is_usable(choice):
        return choice

    # An explicit choice that does not work is worth complaining about — the
    # user asked for it, and silently halving their throughput would be rude.
    log.warning("encoder '%s' was requested but does not work here; using %s",
                choice, SOFTWARE_ENCODER)
    return SOFTWARE_ENCODER


def encoder_args(encoder: str, *, crf: int, fast: bool = True) -> list[str]:
    """ffmpeg output arguments for `encoder` at roughly x264's CRF `crf`.

    The mappings are approximate by nature: hardware encoders at a nominally
    equal quality produce somewhat larger files than x264. For short vertical
    clips that trade is worth several times the speed.
    """
    quality = max(0, min(51, crf))

    if encoder == "h264_nvenc":
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p4" if fast else "p6",
            "-rc", "vbr",
            "-cq", str(quality),
            "-b:v", "0",          # 0 with vbr+cq means "quality-driven"
        ]

    if encoder == "h264_qsv":
        return [
            "-c:v", "h264_qsv",
            "-preset", "faster" if fast else "medium",
            "-global_quality", str(quality),
        ]

    if encoder == "h264_amf":
        return [
            "-c:v", "h264_amf",
            "-quality", "speed" if fast else "quality",
            "-rc", "cqp",
            "-qp_i", str(quality),
            "-qp_p", str(quality),
        ]

    if encoder == "h264_videotoolbox":
        # This one takes a 1-100 scale where higher is better, so CRF has to
        # be inverted rather than passed through.
        return [
            "-c:v", "h264_videotoolbox",
            "-q:v", str(max(1, min(100, int(100 - quality * 1.6)))),
        ]

    return [
        "-c:v", SOFTWARE_ENCODER,
        "-preset", "veryfast" if fast else "medium",
        "-crf", str(quality),
    ]


@lru_cache(maxsize=16)
def is_usable(encoder: str) -> bool:
    """Encode a few frames to find out whether the encoder really works."""
    from app.adapters.media.ffmpeg import ffmpeg_exe

    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        return False
    if encoder != SOFTWARE_ENCODER and encoder not in _listed_encoders(ffmpeg):
        return False

    try:
        proc = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
                "-frames:v", "3",
                "-c:v", encoder,
                "-f", "null", "-",
            ],
            capture_output=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.debug("probe for %s failed: %s", encoder, exc)
        return False
    return proc.returncode == 0


@lru_cache(maxsize=1)
def _listed_encoders(ffmpeg: str) -> frozenset[str]:
    """Names `ffmpeg -encoders` reports. A cheap filter before the real probe."""
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=60
        )
    except (subprocess.SubprocessError, OSError):
        return frozenset()

    names = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        # Lines look like: " V....D h264_amf  AMD AMF H.264 Encoder"
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return frozenset(names)


def describe() -> dict[str, bool]:
    """Which encoders work here. For diagnostics and the health endpoint."""
    return {name: is_usable(name) for name in (*HARDWARE_CANDIDATES, SOFTWARE_ENCODER)}
