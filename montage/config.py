"""Typed configuration for the montage service — its own, not AUT's.

The same rule as the application it came out of: everything the renderer can be
tuned with lives here, nothing else calls `os.getenv`, and `.env` is read once
at a process entrypoint rather than during import, so a test run can never pick
up a developer's local settings.

The groups are lifted from `app.core.config` unchanged, env var names included.
That is deliberate: a setting that meant one thing before the split and another
after it would be the worst possible outcome of a move that is supposed to
change no behaviour at all. What is *not* here is the other half of that file —
the database, the queue, TikTok, YouTube, ASR, retention. A service that
renders video has no business holding credentials for a service that publishes
it, and the fact that this file is a third of the size of the one it came from
is the seam being real.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from typing import ClassVar, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RenderTranscriptMode = Literal[
    "auto", "cache_or_transcribe", "always_transcribe", "job_transcript"
]
RenderStrategy = Literal["auto", "one_pass", "two_stage"]
InsertKind = Literal["broll_full", "broll_pip"]

# Nested groups read the live environment and never touch a .env file: see the
# module docstring for why.
_BASE = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)


class PathSettings(BaseSettings):
    """Where the media lives. Shared with AUT through a mounted volume.

    Only two of AUT's paths matter here — the media root the renderer reads and
    writes under, and the fonts libass loads. The rest (cookies, models, the
    database) belong to the side that owns them.
    """

    model_config = _BASE | SettingsConfigDict(env_prefix="APP_")

    # Subdirectories of media_dir. Structure rather than configuration, so a
    # ClassVar — and the same list AUT has, because the two write into the same
    # mounted volume and a disagreement about which directories exist is a
    # render that fails on a path nobody created.
    MEDIA_SUBDIRS: ClassVar[tuple[str, ...]] = (
        "originals", "slices", "covers", "transcripts", "fragments", "library", "tmp",
    )

    media_dir: Path = Field(default=Path("./AutoClips"), validation_alias="AUTOCLIPS_DIR")
    fonts_dir: Path = Field(default=Path("./assets/fonts"), validation_alias="AUTOCLIPS_FONTS_DIR")

    @field_validator("media_dir", "fonts_dir", mode="before")
    @classmethod
    def _expand(cls, value):
        return Path(str(value)).expanduser() if value else value


class SubtitleSettings(BaseSettings):
    """Karaoke subtitles burned into rendered clips.

    One word per cue, always — words are never glued into pairs.
    """

    model_config = _BASE | SettingsConfigDict(env_prefix="AUTOCLIPS_SUBTITLE_")

    strip_punct: bool = True
    uppercase: bool = False
    # Each cue pops in (scale + fade) instead of appearing flat. Cosmetic;
    # turn it off for a hard-cut look.
    animate: bool = True
    # Whisper word timestamps can run late on fast speech; negative shows cues
    # earlier.
    time_offset_seconds: float = 0.0
    font_size: int = 82
    position_percent: int = 76
    title_font: str = Field(default="Oswald", validation_alias="AUTOCLIPS_TITLE_FONT")


class RenderSettings(BaseSettings):
    model_config = _BASE | SettingsConfigDict(env_prefix="AUTOCLIPS_")

    width: int = 1080
    height: int = 1920
    crf: int = 23
    # libx264 by default, and not "auto", on measured evidence: on an
    # integrated AMD GPU (Radeon 780M) h264_amf rendered a 40s clip in 11.9s
    # against x264's 10.9s, and produced 22.5 MB against 8.5 MB. Hardware
    # encoding is worth switching on where there is a discrete GPU — set
    # "auto", or name one: h264_nvenc, h264_qsv, h264_amf, h264_videotoolbox.
    video_encoder: str = Field(default="libx264", validation_alias="AUTOCLIPS_VIDEO_ENCODER")
    # The blurred backdrop is computed at this fraction of the output size and
    # scaled back up. It is a heavy blur, so the detail thrown away is detail
    # the blur would have destroyed anyway — but the filter runs on ~16x fewer
    # pixels. Measured: 21.4s -> 10.9s for a 40s clip, byte-for-byte the same
    # output size. 1 disables the shortcut.
    blur_scale_divisor: int = Field(
        default=4, ge=1, le=8, validation_alias="AUTOCLIPS_BLUR_SCALE_DIVISOR"
    )
    # How far the sharp layer is zoomed into the blurred backdrop. 1.0 fits the
    # whole source frame, which leaves a 16:9 clip 608px tall in a 1920px
    # canvas — a lot of blur. 1.2 makes the picture 20% taller and takes the
    # extra width off the left and right edges instead. Past ~1.35 the crop
    # starts cutting faces that sit near the frame edge.
    foreground_zoom: float = Field(
        default=1.2, ge=1.0, le=2.0, validation_alias="AUTOCLIPS_RENDER_FOREGROUND_ZOOM"
    )
    # Sharpening pass over the finished frame. Left on because it is a
    # deliberate look, but it is expensive: turning it off took the same clip
    # from 10.9s to 6.9s. The quickest trade of crispness for throughput.
    sharpen: bool = Field(default=True, validation_alias="AUTOCLIPS_RENDER_SHARPEN")
    fps: int = Field(default=30, ge=15, le=60, validation_alias="AUTOCLIPS_RENDER_FPS")

    # How a composition is turned into ffmpeg runs. "one_pass" builds one graph
    # and encodes once; "two_stage" renders each segment to a cached
    # intermediate, joins them without re-encoding, then spends one delivery
    # encode. Measured on a 40s four-segment montage: one pass took 18.4s at
    # VMAF 96.4, two stages 19.7-24.0s at 95.5. So one pass is the default, and
    # "auto" only reaches for two stages when the graph gets large or the
    # fragment cache is already warm. Set "two_stage" explicitly while tuning
    # look or inserts, where every clip is rendered several times and reusing
    # segments is worth more than the encode.
    strategy: RenderStrategy = Field(default="auto", validation_alias="AUTOCLIPS_RENDER_STRATEGY")
    # Above these, "auto" stops building one graph. Not a performance limit —
    # a filter graph nobody can read is a filter graph nobody can debug.
    one_pass_max_segments: int = Field(
        default=8, ge=1, validation_alias="AUTOCLIPS_RENDER_ONE_PASS_MAX_SEGMENTS"
    )
    one_pass_max_inserts: int = Field(
        default=4, ge=0, validation_alias="AUTOCLIPS_RENDER_ONE_PASS_MAX_INSERTS"
    )

    # Reuse of rendered segments between two-stage renders. Disable to measure
    # a cold render, or when the disk is more precious than the CPU.
    fragment_cache: bool = Field(
        default=True, validation_alias="AUTOCLIPS_RENDER_FRAGMENT_CACHE"
    )
    fragment_encoder: str = Field(
        default="libx264", validation_alias="AUTOCLIPS_RENDER_FRAGMENT_ENCODER"
    )
    # Quality of the intermediates, which is entirely a disk-versus-fidelity
    # trade. Measured against a lossless render of the same montage: crf 4
    # scored 95.55 at 38 MB/min, crf 8 -> 95.51 at 24.6, crf 14 -> 95.31 at
    # 13.2, crf 18 -> 94.86 at 9.4. Truly lossless scored 95.54 at 99.9 MB/min,
    # so it buys nothing at all. 8 sits where the curve flattens.
    fragment_crf: int = Field(
        default=8, ge=0, le=30, validation_alias="AUTOCLIPS_RENDER_FRAGMENT_CRF"
    )
    transcript_mode: RenderTranscriptMode = Field(
        default="auto", validation_alias="AUTOCLIPS_RENDER_TRANSCRIPT_MODE"
    )
    ffprobe_timeout_seconds: float = Field(
        default=20.0, validation_alias="AUTOCLIPS_FFPROBE_TIMEOUT_SECONDS"
    )
    silence_scan_timeout_seconds: float = Field(
        default=30.0, validation_alias="AUTOCLIPS_SILENCE_SCAN_TIMEOUT_SECONDS"
    )
    render_timeout_seconds: float = Field(
        default=600.0, validation_alias="AUTOCLIPS_RENDER_TIMEOUT_SECONDS"
    )


class AudioSettings(BaseSettings):
    """The music bed and the transition sounds.

    Levels here are relative. The finished mix is normalised to -14 LUFS at the
    end, so what these numbers control is the balance between the bed and
    everything above it, not the loudness of the clip.
    """

    model_config = _BASE | SettingsConfigDict(env_prefix="AUTOCLIPS_AUDIO_")

    enabled: bool = True
    # How far below the rest of the mix the bed sits before ducking. Quiet on
    # purpose: a bed that is noticeable while somebody is talking is too loud.
    music_gain_db: float = Field(default=-20.0, ge=-60.0, le=0.0)
    music_fade_in_seconds: float = Field(default=0.6, ge=0.0, le=10.0)
    music_fade_out_seconds: float = Field(default=1.2, ge=0.0, le=10.0)
    # Sidechain compression: speech triggers, music gets out of the way. The
    # threshold is a linear level, so 0.03 is roughly -30 dB — low enough that
    # ordinary speech opens it and room tone does not.
    duck_threshold: float = Field(default=0.03, gt=0.0, le=1.0)
    duck_ratio: float = Field(default=8.0, ge=1.0, le=20.0)
    duck_attack_ms: float = Field(default=20.0, ge=0.01, le=2000.0)
    # Slow enough not to pump between words, quick enough to come back up in a
    # real pause.
    duck_release_ms: float = Field(default=300.0, ge=0.01, le=9000.0)

    effect_gain_db: float = Field(default=-8.0, ge=-60.0, le=12.0)
    effect_seconds: float = Field(default=1.0, ge=0.1, le=10.0)
    max_effects_per_clip: int = Field(default=6, ge=0, le=40)
    effect_min_gap_seconds: float = Field(default=4.0, ge=0.0, le=60.0)
    # Where transition sounds are allowed. Both are places the picture already
    # changes; a whoosh over a continuous shot has nothing to explain it.
    effects_on_cuts: bool = True
    effects_on_inserts: bool = True


class SceneSettings(BaseSettings):
    """Marking up material that has no usable speech.

    One ffmpeg pass finds both signals the scene cutter needs. It is a decode,
    so it costs roughly a thirty-seventh of the source's running time — three
    minutes for a two-hour film — and only profiles that ask for it pay.
    """

    model_config = _BASE | SettingsConfigDict(env_prefix="AUTOCLIPS_SCENES_")

    # How different two consecutive frames must be to count as a cut. 0.4 is
    # ffmpeg's usual working value: a hard cut in edited footage scores well
    # above it, a camera pan well below. Lower it for material shot in long
    # takes, at the cost of cutting on movement rather than on edits.
    threshold: float = Field(default=0.4, ge=0.05, le=1.0)
    # Generous on purpose: this runs once per source, and a film that takes ten
    # minutes to mark up is still cheaper than transcribing it.
    timeout_seconds: float = Field(default=1800.0, ge=30.0)


class InsertSettings(BaseSettings):
    """Automatic b-roll: how much of a clip may be covered, and where.

    Every number here is a guardrail rather than a target. The pipeline runs
    unattended, so the failure mode to design against is not "too little
    b-roll" — it is a clip whose hook is buried under a stock video three
    seconds in.
    """

    model_config = _BASE | SettingsConfigDict(env_prefix="AUTOCLIPS_INSERTS_")

    enabled: bool = True
    # broll_full covers the frame, broll_pip sits in a corner over it.
    kind: InsertKind = "broll_full"
    max_per_clip: int = Field(default=4, ge=0, le=12)
    min_seconds: float = Field(default=1.5, ge=0.3, le=15.0)
    max_seconds: float = Field(default=3.5, ge=0.5, le=30.0)
    # Space between inserts. Without it a passage dense in keywords becomes a
    # slideshow with a voice over it.
    min_gap_seconds: float = Field(default=6.0, ge=0.0, le=60.0)
    # The opening seconds belong to the speaker: they decide whether anybody
    # watches the rest.
    hook_guard_seconds: float = Field(default=2.5, ge=0.0, le=30.0)
    tail_guard_seconds: float = Field(default=1.5, ge=0.0, le=30.0)
    # Ceiling on the share of a clip covered by b-roll.
    max_share: float = Field(default=0.35, ge=0.0, le=0.9)
    # What to do with a clip whose words match no tag in the library. On, it
    # falls back to a fixed beat — which sounds like "the library still gets
    # used" and behaves like "tags do not matter": every asset lands on every
    # clip, whatever it is about. Off is the default for that reason; turn it
    # on only for a library of neutral filler.
    cadence_when_no_match: bool = False
    cadence_seconds: float = Field(default=12.0, ge=2.0, le=120.0)

    max_upload_mb: int = Field(default=200, ge=1, le=4096)

class Settings(BaseSettings):
    """Everything the montage service is tuned with."""

    model_config = _BASE

    paths: PathSettings = Field(default_factory=PathSettings)
    subtitles: SubtitleSettings = Field(default_factory=SubtitleSettings)
    render: RenderSettings = Field(default_factory=RenderSettings)
    inserts: InsertSettings = Field(default_factory=InsertSettings)
    scenes: SceneSettings = Field(default_factory=SceneSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The settings for this process, built once.

    Cached like AUT's for the same reason: reading the environment on every
    filter string would be a measurable cost in a graph with a hundred of them.
    Tests clear it through the same `cache_clear`.
    """
    return Settings()
