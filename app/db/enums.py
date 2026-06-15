"""State machines, written down.

The previous version had statuses invented ad hoc in several modules —
'sliced' in one place, 'completed' in another, 'ready' in the UI — and the job
status was recomputed by scanning sibling rows. Naming every state once, next
to the transitions that are legal, is what makes the flow followable again.

Stored as plain strings (StrEnum), so adding a state never needs a database
type migration.
"""
from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    """A source video being turned into clips."""

    CREATED = "created"          # registered, nothing done yet
    DOWNLOADING = "downloading"  # fetching the source
    TRANSCRIBING = "transcribing"
    PLANNING = "planning"        # deciding where to cut
    RENDERING = "rendering"      # clips are being rendered
    READY = "ready"              # every planned clip reached a terminal state
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_JOB


class ClipStatus(StrEnum):
    """One clip cut out of a job's source video."""

    PLANNED = "planned"      # boundaries decided, not rendered
    RENDERING = "rendering"
    READY = "ready"          # file on disk, ready to publish
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_CLIP


class PublicationStatus(StrEnum):
    """Intent to publish one video to one account at one time."""

    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_PUBLICATION

    @property
    def blocks_rescheduling(self) -> bool:
        """States in which a clip must not be queued for publishing again.

        A failed publication counts: it may have gone live before failing, and
        re-queueing it automatically could post a duplicate.
        """
        return self is not PublicationStatus.CANCELLED


class TaskKind(StrEnum):
    """What a queued unit of work does. One handler per kind."""

    DOWNLOAD = "download"  # fetch + transcribe + plan slices
    RENDER = "render"      # render one clip
    PUBLISH = "publish"    # upload one publication to TikTok
    CLEANUP = "cleanup"    # delete expired media


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_active(self) -> bool:
        return self in _ACTIVE_TASK

    @property
    def is_terminal(self) -> bool:
        return not self.is_active


class LoginStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class AssetKind(StrEnum):
    """What kind of file a library asset is.

    The distinction is not cosmetic. A still has no timeline of its own, so it
    has to be held on screen for its whole appearance rather than played; audio
    has no picture at all, so it must never be picked as b-roll.
    """

    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"


class SourceKind(StrEnum):
    """Where a publication's video comes from."""

    CLIP = "clip"        # a Clip row produced by this app
    LOCAL = "local"      # an arbitrary file already on disk
    YOUTUBE = "youtube"  # a URL resolved at publish time


_TERMINAL_JOB = frozenset({JobStatus.READY, JobStatus.FAILED, JobStatus.CANCELLED})
_TERMINAL_CLIP = frozenset({ClipStatus.READY, ClipStatus.FAILED, ClipStatus.CANCELLED})
_TERMINAL_PUBLICATION = frozenset(
    {PublicationStatus.PUBLISHED, PublicationStatus.FAILED, PublicationStatus.CANCELLED}
)
_ACTIVE_TASK = frozenset({TaskStatus.QUEUED, TaskStatus.RUNNING})
