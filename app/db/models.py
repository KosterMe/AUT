"""Database tables.

The shape of this schema is the main correction over the previous version,
where a clip existed only as a JSON blob inside a task's `result_json`, was
duplicated into the job's `metadata_json`, and was linked to its scheduled
upload by an integer buried in that JSON. Finding "which schedule belongs to
this clip" meant loading every render task and parsing each one.

Here a clip is a row, and a publication points at it with a foreign key.
The rules that follow from that:

* Tables hold **state**; JSON columns hold only genuinely open-ended data
  (yt-dlp source metadata, task payloads, upload option bags).
* A status is written in exactly one place — the owner of that entity.
* Times are naive UTC (see app.core.clock).
* `owner_id` is present but unused: it is the seam for multi-user support,
  so adding it later is a backfill rather than a reshape of every query.
"""

# NOTE: no `from __future__ import annotations` here on purpose. SQLModel
# resolves Relationship() targets from the real annotation objects; with
# postponed evaluation it hands SQLAlchemy the literal string `List['Clip']`
# and mapper configuration fails. Every annotation below is therefore written
# in the explicit typing.Optional / typing.List form.

from datetime import datetime
from typing import List, Optional

from sqlalchemy import Index
from sqlmodel import Field, Relationship, SQLModel

from app.core.clock import utc_now
from app.db.enums import (
    AssetKind,
    ClipStatus,
    JobStatus,
    LoginStatus,
    PublicationStatus,
    SourceKind,
    TaskKind,
    TaskStatus,
)


class Account(SQLModel, table=True):
    """A TikTok account, identified by its saved cookie jar on disk."""

    __tablename__ = "accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: Optional[int] = Field(default=None, index=True)

    username: str = Field(index=True, unique=True, max_length=128)
    display_name: Optional[str] = Field(default=None, max_length=128)
    cookie_path: str = Field(max_length=1024)
    # Flipped to False the moment TikTok rejects the session, so the UI can
    # ask for a re-login instead of letting uploads fail one by one.
    has_valid_session: bool = Field(default=True, index=True)

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_used_at: Optional[datetime] = Field(default=None)

    publications: List["Publication"] = Relationship(back_populates="account")


class ClipJob(SQLModel, table=True):
    """A source video and the intent to cut clips out of it."""

    __tablename__ = "clip_jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: Optional[int] = Field(default=None, index=True)

    source_platform: str = Field(max_length=16, index=True)  # youtube | local
    source_ref: str = Field(max_length=2048)

    # What kind of video this is: talking | plain | split | film. It picks the
    # cutter and the frame, so it is a property of the job rather than of one
    # render — re-rendering a film with the podcast cutter would recut it.
    profile: str = Field(default="talking", max_length=16, index=True)

    # Title auto-detected from the source; custom_title overrides it for
    # on-screen text and captions.
    title: Optional[str] = Field(default=None, max_length=512)
    custom_title: Optional[str] = Field(default=None, max_length=200)
    # Trailing #tags/@mentions applied to every clip of this job. When set,
    # they replace auto-detected topic tags rather than adding to them.
    caption_tags: Optional[str] = Field(default=None, max_length=500)

    duration_seconds: Optional[float] = Field(default=None)
    original_path: Optional[str] = Field(default=None, max_length=1024)
    thumbnail_path: Optional[str] = Field(default=None, max_length=1024)

    status: str = Field(default=JobStatus.CREATED, max_length=16, index=True)
    error: Optional[str] = Field(default=None, max_length=2048)
    # Progress of the currently running stage, mirrored from the active task
    # so listing jobs never has to join the queue.
    stage: Optional[str] = Field(default=None, max_length=64)
    progress: float = Field(default=0.0)

    # Raw extractor output (yt-dlp). Read-only reference data, never state.
    source_metadata_json: str = Field(default="{}")

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    clips: List["Clip"] = Relationship(
        back_populates="job",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

    @property
    def display_title(self) -> str:
        return (self.custom_title or self.title or f"job-{self.id}").strip()


class Clip(SQLModel, table=True):
    """One cut from a job's source video, rendered vertically for TikTok.

    Was a JSON blob in `AutoClipTask.result_json` plus a duplicate copy in
    `ClipJob.metadata_json.slice_last_result`.
    """

    __tablename__ = "clips"
    # Listing a job's clips in order is the single most common read.
    __table_args__ = (Index("ix_clips_job_order", "job_id", "index"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="clip_jobs.id", index=True)

    # 1-based position in the source, used for "часть N" captions and ordering.
    index: int = Field(index=True)
    start_sec: float
    end_sec: float
    duration_sec: float

    # Headline burned on the clip, and the transcript text it was cut around.
    title: Optional[str] = Field(default=None, max_length=512)
    text: Optional[str] = Field(default=None, max_length=4096)

    video_path: Optional[str] = Field(default=None, max_length=1024)
    cover_path: Optional[str] = Field(default=None, max_length=1024)

    status: str = Field(default=ClipStatus.PLANNED, max_length=16, index=True)
    error: Optional[str] = Field(default=None, max_length=2048)

    # The render task currently or last responsible for this clip.
    task_id: Optional[int] = Field(default=None, foreign_key="tasks.id", index=True)

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    job: Optional[ClipJob] = Relationship(back_populates="clips")
    publications: List["Publication"] = Relationship(back_populates="clip")


class Publication(SQLModel, table=True):
    """Intent to publish one video to one account at one time.

    Separate from the Task that executes it: the user owns the intent (they
    reschedule, retitle, cancel it) while the queue owns execution. Merging
    the two is what made the old `scheduled_uploads` table double as a job
    queue and a user-facing list, with retry bookkeeping leaking into the UI.
    """

    __tablename__ = "publications"
    # "which clips are already spoken for" — the check that replaces the old
    # full scan over every render task's JSON.
    __table_args__ = (Index("ix_publications_clip_status", "clip_id", "status"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: Optional[int] = Field(default=None, index=True)

    account_id: int = Field(foreign_key="accounts.id", index=True)
    # Set when publishing a clip this app produced. The foreign key is what
    # replaces the old JSON round-trip between clips and schedules.
    clip_id: Optional[int] = Field(default=None, foreign_key="clips.id", index=True)

    source_kind: str = Field(default=SourceKind.CLIP, max_length=16)
    # Absolute path (clip/local) or URL (youtube), resolved when the task runs.
    source_ref: str = Field(max_length=2048)

    caption: str = Field(max_length=2200)
    options_json: str = Field(default="{}")

    scheduled_at: datetime = Field(index=True)
    status: str = Field(default=PublicationStatus.SCHEDULED, max_length=16, index=True)

    result_text: Optional[str] = Field(default=None, max_length=2048)
    published_at: Optional[datetime] = Field(default=None)

    task_id: Optional[int] = Field(default=None, foreign_key="tasks.id", index=True)

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    account: Optional[Account] = Relationship(back_populates="publications")
    clip: Optional[Clip] = Relationship(back_populates="publications")


class MediaAsset(SQLModel, table=True):
    """A fragment in the b-roll library: something to lay over a clip.

    Tags are what makes the library usable without a person: the insert planner
    matches them against the words in a clip's subtitles, so an asset tagged
    `машина` goes on screen at the moment the word is spoken. They are stored
    normalised and comma-separated because they are only ever read as a whole
    set, never queried individually.

    `checksum` is unique, so uploading the same file twice updates the tags of
    the asset that is already there instead of filling the library with
    duplicates that then compete for the same keyword.
    """

    __tablename__ = "media_assets"

    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: Optional[int] = Field(default=None, index=True)

    kind: str = Field(default=AssetKind.VIDEO, max_length=16, index=True)
    path: str = Field(max_length=1024)
    original_name: str = Field(default="", max_length=255)
    tags: str = Field(default="", max_length=500)

    duration_sec: Optional[float] = Field(default=None)
    width: Optional[int] = Field(default=None)
    height: Optional[int] = Field(default=None)
    size_bytes: int = Field(default=0)
    checksum: str = Field(max_length=64, unique=True, index=True)

    # Rotation, not statistics: the planner prefers whatever has been on screen
    # least recently, so one fragment does not end up in every clip of a job.
    last_used_at: Optional[datetime] = Field(default=None, index=True)
    use_count: int = Field(default=0)

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def tag_list(self) -> list[str]:
        return [tag for tag in (part.strip() for part in self.tags.split(",")) if tag]


class Task(SQLModel, table=True):
    """A unit of background work.

    One table for every kind of work, replacing two near-identical queues
    (`scheduled_uploads` and `autoclip_tasks`) that each reimplemented atomic
    claiming, heartbeats, stale reclaim and retry — with subtly different bugs.

    Leasing model: a worker claims a task by compare-and-swapping
    queued -> running and setting `lease_expires_at`. It refreshes the lease
    while working. A lease that expires means the worker died, and the task is
    requeued or failed depending on attempts left.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        # The claim query: due tasks of the kinds this worker serves, best
        # priority first. Without this every poll scans the whole table.
        Index("ix_tasks_claim", "status", "kind", "run_at", "priority"),
        # The reclaim sweep: running tasks whose lease has expired.
        Index("ix_tasks_lease", "status", "lease_expires_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    kind: str = Field(max_length=16, index=True)
    payload_json: str = Field(default="{}")

    status: str = Field(default=TaskStatus.QUEUED, max_length=16, index=True)
    # Human-readable step inside the handler ("downloading", "transcribing").
    stage: str = Field(default="queued", max_length=64)
    progress: float = Field(default=0.0)

    # Not before this time. Also how retry backoff is expressed.
    run_at: datetime = Field(default_factory=utc_now, index=True)
    # Higher runs first among tasks that are due.
    priority: int = Field(default=0, index=True)

    # Optional idempotency key. A unique index makes "publish this clip twice"
    # impossible at the database level instead of via a scan-and-check.
    dedupe_key: Optional[str] = Field(default=None, max_length=128, unique=True)

    attempts: int = Field(default=0)
    max_attempts: int = Field(default=3)

    heartbeat_at: Optional[datetime] = Field(default=None)
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
    # Cooperative cancellation: handlers check this between steps.
    cancel_requested: bool = Field(default=False, index=True)
    # Set by a handler once it performs a side effect that cannot be undone or
    # safely repeated — publishing to TikTok, for instance. If the worker then
    # dies, the task is failed for a human to look at rather than retried,
    # because a retry could post the same video twice.
    irreversible: bool = Field(default=False)

    error: Optional[str] = Field(default=None, max_length=4096)
    result_json: str = Field(default="{}")

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def attempts_left(self) -> int:
        return max(0, self.max_attempts - self.attempts)


class LoginSession(SQLModel, table=True):
    """An in-progress attempt to capture a TikTok session for an account."""

    __tablename__ = "login_sessions"

    id: str = Field(primary_key=True, max_length=64)  # uuid hex
    username: str = Field(index=True, max_length=128)
    # local_browser = drives Chrome on this machine (desktop only)
    # import = the user supplied a cookie file or JSON payload
    method: str = Field(default="import", max_length=16)
    status: str = Field(default=LoginStatus.PENDING, max_length=16, index=True)
    error: Optional[str] = Field(default=None, max_length=2048)

    started_at: datetime = Field(default_factory=utc_now)
    completed_at: Optional[datetime] = Field(default=None)


__all__ = [
    "Account",
    "AssetKind",
    "Clip",
    "ClipJob",
    "ClipStatus",
    "JobStatus",
    "LoginSession",
    "LoginStatus",
    "MediaAsset",
    "Publication",
    "PublicationStatus",
    "SourceKind",
    "Task",
    "TaskKind",
    "TaskStatus",
]
