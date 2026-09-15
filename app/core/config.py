"""Typed application configuration — the single source of truth.

Everything the app can be tuned with lives here. Nothing else in the codebase
calls `os.getenv`; modules ask for `get_settings()` instead. That is what makes
the settings discoverable, testable, and documentable in one place.

Config comes from the process environment only. `.env` is a developer
convenience loaded once at a process entrypoint (`app.api.main`,
`app.workers`), never during import of a library module — so importing the
package inside a test suite can never pick up a developer's local `.env`.

Env var names are grouped by prefix:

    APP_*        runtime, paths, database, api
    QUEUE_*      task queue behaviour
    TIKTOK_*     TikTok account/upload behaviour
    YTDLP_*      yt-dlp download auth and transport
    AUTOCLIPS_*  media pipeline: rendering, ASR, subtitles, captions

The AUTOCLIPS_/YTDLP_ names are kept verbatim from the previous version so an
existing, hard-won `.env` (PO tokens, Whisper tuning) keeps working untouched.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

AsrBackend = Literal["whisper", "nvidia"]
RenderTranscriptMode = Literal["auto", "cache_or_transcribe", "always_transcribe", "job_transcript"]
RenderStrategy = Literal["auto", "one_pass", "two_stage"]
InsertKind = Literal["broll_full", "broll_pip"]

# Nested groups read the live environment and never touch a .env file: see the
# module docstring for why.
_BASE = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

# Comma-separated env vars. Without NoDecode, pydantic-settings tries to JSON
# decode any non-scalar field before validators run, so `a,b` would explode.
CsvStr = Annotated[tuple[str, ...], NoDecode]
CsvFloat = Annotated[tuple[float, ...], NoDecode]


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


class PathSettings(BaseSettings):
    """Filesystem layout. Relative paths resolve against the process cwd."""

    model_config = _BASE | SettingsConfigDict(env_prefix="APP_")

    # Subdirectories of media_dir. Named once here so startup and the ffmpeg
    # adapter cannot disagree about which ones exist. ClassVar, not a field:
    # this is structure, not configuration.
    MEDIA_SUBDIRS: ClassVar[tuple[str, ...]] = (
        "originals",
        "slices",
        "covers",
        "transcripts",
        "fragments",
        "library",
        "tmp",
    )

    data_dir: Path = Path("./data")
    cookies_dir: Path = Path("./CookiesDir")
    videos_dir: Path = Path("./VideosDirPath")
    media_dir: Path = Field(default=Path("./AutoClips"), validation_alias="AUTOCLIPS_DIR")
    fonts_dir: Path = Field(default=Path("./assets/fonts"), validation_alias="AUTOCLIPS_FONTS_DIR")

    def ensure(self) -> None:
        """Create every directory the app writes to. Called once at startup."""
        for path in (self.data_dir, self.cookies_dir, self.videos_dir, self.media_dir):
            path.mkdir(parents=True, exist_ok=True)
        for name in self.MEDIA_SUBDIRS:
            (self.media_dir / name).mkdir(parents=True, exist_ok=True)


class DatabaseSettings(BaseSettings):
    model_config = _BASE | SettingsConfigDict(env_prefix="APP_")

    # Full SQLAlchemy URL. Set this to postgresql+psycopg://... to move off
    # SQLite; the queue's claim query is written to be correct on both.
    database_url: str = ""
    # Legacy single-file override kept so existing deployments keep booting.
    db_path: str = Field(default="", validation_alias="TIKTOK_API_DB_PATH")
    echo_sql: bool = False
    # SQLite only: how long a writer waits on a locked database.
    busy_timeout_ms: int = 5000

    def url(self, data_dir: Path) -> str:
        if self.database_url:
            return self.database_url
        if self.db_path:
            return f"sqlite:///{self.db_path}"
        # Deliberately not `tiktok.db`: that name belongs to the pre-2.0 schema,
        # and pointing at it would make Alembic try to migrate a database whose
        # tables mean something else entirely.
        return f"sqlite:///{(data_dir / 'app.db').as_posix()}"


class ApiSettings(BaseSettings):
    model_config = _BASE | SettingsConfigDict(env_prefix="APP_")

    host: str = "0.0.0.0"
    port: int = 8000
    # Browser origins allowed to call the API. "*" is fine while the API is
    # bound to localhost; set real origins before exposing it.
    cors_origins: CsvStr = ("*",)
    # Auth skeleton: when set, every /api route requires this bearer token.
    # Empty (the default) leaves the API open, which is correct for a
    # single-user localhost deployment and wrong for anything public.
    auth_token: str = ""
    # URL other services use to reach this API (callbacks, worker links).
    public_url: str = "http://api:8000"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        return _csv(value) if isinstance(value, str) else value


class QueueSettings(BaseSettings):
    """Task queue behaviour, shared by every worker."""

    model_config = _BASE | SettingsConfigDict(env_prefix="QUEUE_")

    # Idle sleep between polls when there is nothing to run.
    poll_seconds: float = 5.0
    # A running task must refresh its lease this often...
    heartbeat_seconds: float = 15.0
    # ...or it is considered dead after this long and reclaimed.
    lease_seconds: float = 300.0
    max_attempts: int = 3
    # Retry delay grows as backoff_seconds * attempt.
    retry_backoff_seconds: float = 300.0
    # How many tasks one worker process runs at once.
    concurrency: int = 1

    @field_validator("concurrency")
    @classmethod
    def _at_least_one_thread(cls, value: int) -> int:
        """Zero starts a worker that polls nothing and says nothing about it.

        The container comes up, reports healthy, logs "worker started" — and
        the queue simply never moves. Scheduled posts stop going out with
        nothing anywhere to explain why.
        """
        if value < 1:
            raise ValueError("must be at least 1; a worker with no threads runs nothing")
        if value > 64:
            raise ValueError("must be 64 or fewer; this is a thread count, not a queue size")
        return value

    @field_validator("poll_seconds", "heartbeat_seconds", "lease_seconds")
    @classmethod
    def _positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be positive")
        return value


class TikTokSettings(BaseSettings):
    model_config = _BASE | SettingsConfigDict(env_prefix="TIKTOK_")

    login_url: str = "https://www.tiktok.com/login"
    # Upload chunk size in bytes; the file is streamed, never read whole.
    upload_chunk_bytes: int = 5 * 1024 * 1024
    # Fallback User-Agent when a session has none stored from login.
    default_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    # Tags appended after topic tags inferred from the video title.
    default_hashtags: str = Field(
        default="#нарезка #моменты #рек #рекомендации #tiktok #shorts #fyp",
        validation_alias="AUTOCLIPS_TIKTOK_HASHTAGS",
    )


class YouTubeSettings(BaseSettings):
    """yt-dlp auth and transport.

    YouTube serves HTTP 403 mid-download to clients without a PO token, so the
    working setup is usually: cookies file + mweb player client + a bgutil
    PO-token provider. See .env.example for the exact recipe.
    """

    model_config = _BASE | SettingsConfigDict(env_prefix="YTDLP_")

    cookies_file: str = ""
    cookies_from_browser: str = ""
    node_path: str = ""
    remote_components: str = "ejs:github"
    ffmpeg_location: str = ""
    player_clients: CsvStr = ()
    po_token: CsvStr = ()
    visitor_data: str = ""
    data_sync_id: str = ""
    # Where the bgutil PO-token provider answers. Its plugin defaults to
    # http://127.0.0.1:4416, which is right on a desktop and wrong in a
    # container, where loopback is the container itself — compose runs the
    # provider as its own service and points this at it.
    pot_base_url: str = ""
    socket_timeout: int = 20
    retries: int = 10
    # Cap on the source's smallest dimension (yt-dlp's `res`, so it reads the
    # same for a landscape and a portrait video). Everything downloaded here
    # exists to be re-encoded into a 1080x1920 canvas, and yt-dlp's own idea
    # of "best" ignores that: on one 24-minute source it chose AV1 2160p at
    # 540 MB where the 1080p rendition is 158 MB and nothing above 1080 ever
    # reaches the screen. The extra pixels are paid for twice — once on a link
    # that drops large transfers, and again in every render, which decodes 4K
    # AV1 to produce a frame 1080 wide. 0 disables the cap.
    max_resolution: int = 1080
    # Which video codec to prefer at that resolution. A source is downloaded
    # once and decoded again for every clip cut out of it — fifteen to twenty
    # times for one video — so the decoder's cost is paid over and over while
    # the file crosses the wire once. Measured in CPU seconds over 60 s of
    # source, decode plus the real filter chain and an x264 encode: 140.4 for
    # the AV1 2160p yt-dlp picks by itself, 90.0 for AV1 1080p, 75.4 for H.264
    # 1080p. H.264 costs 55 MB more to download and gives back 16% of every
    # render. Empty leaves the choice to yt-dlp.
    prefer_codec: str = "h264"
    # yt-dlp inherits HTTP_PROXY/HTTPS_PROXY from the shell otherwise, which
    # trips YouTube bot checks. Empty string means "explicitly no proxy";
    # unset the variable entirely only if you want inheritance back.
    proxy: str = ""

    @field_validator("player_clients", "po_token", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        return _csv(value) if isinstance(value, str) else value


class WhisperSettings(BaseSettings):
    """Local faster-whisper transcription."""

    model_config = _BASE | SettingsConfigDict(env_prefix="AUTOCLIPS_WHISPER_")

    model: str = "large-v3"
    device: str = "cpu"
    compute_type: str = "int8"
    # Empty = auto-detect. Only force a language when auto-detection is wrong.
    language: str = ""
    initial_prompt: str = ""
    word_timestamps: bool = True
    # Greedy decoding by default: 2-3x faster on CPU, marginal accuracy loss.
    beam_size: int = 1
    best_of: int = 1
    # Whisper re-decodes a hard segment at a higher temperature instead of
    # dropping it. A single value disables the fallback ladder.
    temperature: CsvFloat = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
    condition_on_previous_text: bool = False

    # The VAD trims audio it judges as silence BEFORE transcription, which is
    # the usual cause of dropped quiet or fast words.
    vad_filter: bool = True
    vad_speech_pad_ms: int = 400
    vad_min_silence_ms: int = 500
    vad_threshold: float = 0.35

    # Segment-drop guards. None = faster-whisper defaults.
    no_speech_threshold: float | None = None
    log_prob_threshold: float | None = None
    compression_ratio_threshold: float | None = None

    # Performance only — these must not affect transcript text or cache keys.
    cpu_threads: int = 0
    # Also the ceiling on chunk parallelism below: a shared CTranslate2 model
    # runs `num_workers` inferences at once and queues the rest.
    num_workers: int = 1

    # Chunk-parallel transcription: a big win for slow models on long media,
    # but only together with `num_workers`. On its own `chunked` splits the
    # audio and then feeds the chunks through one at a time — the same work
    # plus one ffmpeg extraction per chunk, so slightly slower than not
    # chunking at all. Raise both or neither.
    chunked: bool = False
    chunk_seconds: float = 300.0
    chunk_overlap_seconds: float = 1.5

    # Model downloads ignore inherited desktop proxy settings by default.
    proxy: str = ""

    @field_validator("temperature", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(float(item) for item in _csv(value))
        if isinstance(value, (int, float)):
            return (float(value),)
        return value


class NvidiaAsrSettings(BaseSettings):
    """Hosted NVIDIA Riva/NVCF ASR — GPU in the cloud, no local compute.

    Use a model that returns WORD timestamps offline (parakeet RNNT does);
    canary-1b returns text with no word offsets, which cannot drive slicing or
    karaoke subtitles.
    """

    model_config = _BASE | SettingsConfigDict(env_prefix="AUTOCLIPS_NVIDIA_ASR_")

    function_id: str = "71203149-d3b7-4460-8231-1be2543a1fca"
    api_key: str = ""
    language: str = "multi"
    server: str = "grpc.nvcf.nvidia.com:443"
    punctuation: bool = True
    sample_rate: int = 16000

    def resolved_api_key(self) -> str:
        return self.api_key or os.getenv("OPENAI_API_KEY", "")


class AsrSettings(BaseSettings):
    model_config = _BASE | SettingsConfigDict(env_prefix="AUTOCLIPS_")

    backend: AsrBackend = Field(default="whisper", validation_alias="AUTOCLIPS_ASR_BACKEND")
    whisper: WhisperSettings = Field(default_factory=WhisperSettings)
    nvidia: NvidiaAsrSettings = Field(default_factory=NvidiaAsrSettings)

    # Prefer YouTube's own captions over ASR when the source has them.
    use_youtube_captions: bool = True
    youtube_caption_langs: CsvStr = ("ru", "en", "*")
    youtube_caption_timeout_seconds: float = 20.0

    @field_validator("youtube_caption_langs", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        return _csv(value) if isinstance(value, str) else value


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
    one_pass_max_layers: int = Field(
        default=8, ge=0, validation_alias="AUTOCLIPS_RENDER_ONE_PASS_MAX_INSERTS"
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


class RetentionSettings(BaseSettings):
    """Disk hygiene. Without this the media directory grows without bound —
    the previous version accumulated 11 GB of intermediate slices."""

    model_config = _BASE | SettingsConfigDict(env_prefix="APP_RETENTION_")

    enabled: bool = True
    # Delete downloaded source videos this many days after their job finished.
    originals_days: int = 3
    # Delete rendered clips this many days after they were published.
    published_clips_days: int = 7
    # Delete everything belonging to jobs older than this, published or not.
    jobs_days: int = 30
    # Ceiling for the rendered-segment cache. Unlike the rest of this class the
    # limit is size, not age: a fragment is worth keeping exactly as long as
    # there is room, and the oldest go first.
    fragment_cache_gb: float = 5.0
    # How often the sweep runs. Nothing else triggers it: the handler exists to
    # keep the media directory from growing without bound, and for a while
    # nothing ever enqueued it, so it never ran once.
    cleanup_interval_hours: float = 6.0
    # How long yt-dlp's leftovers from an unfinished download are kept. They
    # are worth something for exactly as long as a retry might resume from
    # them, and nothing afterwards — hours, not days, but generous enough to
    # cover a job restarted by hand the next morning.
    abandoned_download_hours: float = 24.0

    @field_validator(
        "originals_days",
        "published_clips_days",
        "jobs_days",
        "cleanup_interval_hours",
        "abandoned_download_hours",
    )
    @classmethod
    def _not_negative(cls, value: float) -> float:
        """A negative age is not "keep forever" — it deletes everything.

        Each sweep works from `now - timedelta(days=setting)`. Set it to -10
        and the cutoff lands ten days in the *future*, so every job ever
        finished is past it and every source download goes on the next sweep.
        A typed minus sign should not be able to empty the media directory.
        Use `APP_RETENTION_ENABLED=false` to keep everything.
        """
        if value < 0:
            raise ValueError("must not be negative; set APP_RETENTION_ENABLED=false to keep everything")
        return value


class Settings(BaseSettings):
    """Root settings object. Build once per process via `get_settings()`."""

    model_config = _BASE | SettingsConfigDict(env_prefix="APP_")

    environment: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = False

    paths: PathSettings = Field(default_factory=PathSettings)
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    tiktok: TikTokSettings = Field(default_factory=TikTokSettings)
    youtube: YouTubeSettings = Field(default_factory=YouTubeSettings)
    asr: AsrSettings = Field(default_factory=AsrSettings)
    subtitles: SubtitleSettings = Field(default_factory=SubtitleSettings)
    render: RenderSettings = Field(default_factory=RenderSettings)
    inserts: InsertSettings = Field(default_factory=InsertSettings)
    scenes: SceneSettings = Field(default_factory=SceneSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)

    @property
    def database_url(self) -> str:
        return self.db.url(self.paths.data_dir)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so env parsing happens exactly once.

    Tests call `get_settings.cache_clear()` after changing the environment.
    """
    return Settings()


def load_dotenv_for_entrypoint() -> None:
    """Read `.env` into the environment. Call this from process entrypoints
    only — never at import time of a library module, or a test run would
    silently inherit the developer's local configuration."""
    from dotenv import load_dotenv

    load_dotenv()
    get_settings.cache_clear()
