"""The b-roll library: fragments that get laid over clips.

Assets are the one kind of media in this app that a person puts in by hand, and
the only one that outlives the job it was used on — so they live outside the
retention sweeps and are addressed by content, not by job.

Deduplication is by checksum rather than by filename. Uploading the same file
again is a tagging operation, not a second copy: two rows pointing at identical
footage would compete for the same keyword and halve the chance of either being
picked.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import tempfile
from datetime import datetime
from typing import BinaryIO

from sqlmodel import Session, col, select

from app.adapters.media import ffmpeg as media
from montage import client as montage
from app.core.clock import utc_now
from app.core.errors import NotFoundError, ValidationError
from app.core.config import get_settings
from app.db.enums import AssetKind
from app.db.models import MediaAsset
from montage import style
from montage.rules import inserts

log = logging.getLogger(__name__)

# Extensions treated as a still. GIF is deliberately absent: it has a timeline
# and ffmpeg plays it, so it behaves like a very short video.
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".avif"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm", ".m4v", ".gif"})
# Music beds and transition sounds. They carry no picture, so nothing here can
# ever end up on screen as b-roll.
AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"})

# What the browser is told a fragment is when the library previews it. Not
# cosmetic: Firefox will not play a .wav announced as audio/mpeg and Chrome
# will not play a .webm announced as video/mp4, so a single wrong default here
# turns a working preview into a silent black card.
CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".bmp": "image/bmp", ".avif": "image/avif",
    ".gif": "image/gif",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".flac": "audio/flac",
}
# Read in chunks this size while hashing an upload. A megabyte is small enough
# that a dozen concurrent uploads cost nothing worth measuring, and large
# enough that a 200 MB clip is 200 reads rather than 200 000.
UPLOAD_CHUNK_BYTES = 1024 * 1024


def library_dir() -> str:
    directory = os.path.join(media.media_root(), "library")
    os.makedirs(directory, exist_ok=True)
    return directory


def kind_for(filename: str) -> str:
    suffix = os.path.splitext(filename)[1].lower()
    if suffix in IMAGE_SUFFIXES:
        return AssetKind.IMAGE
    if suffix in VIDEO_SUFFIXES:
        return AssetKind.VIDEO
    if suffix in AUDIO_SUFFIXES:
        return AssetKind.AUDIO
    raise ValidationError(
        f"unsupported file type '{suffix or filename}' — expected one of: "
        + ", ".join(sorted(VIDEO_SUFFIXES | IMAGE_SUFFIXES | AUDIO_SUFFIXES))
    )


def content_type_for(filename: str, kind: str | None = None) -> str:
    """The MIME type to serve this file as, taken from its extension.

    The kind is only a fallback: `.gif` is stored as a video because ffmpeg
    treats it as one, but a browser has to be told it is an image or the
    preview shows nothing at all.
    """
    suffix = os.path.splitext(filename or "")[1].lower()
    if suffix in CONTENT_TYPES:
        return CONTENT_TYPES[suffix]
    return {
        AssetKind.IMAGE: "image/jpeg",
        AssetKind.AUDIO: "audio/mpeg",
        AssetKind.VIDEO: "video/mp4",
    }.get(kind, "application/octet-stream")


def normalize_tags(raw: str | None) -> str:
    """Tags as stored: lowercase, comma separated, deduplicated, order kept."""
    seen: list[str] = []
    for part in (raw or "").replace(";", ",").replace("#", " ").split(","):
        for token in part.split():
            tag = token.strip().lower()
            if tag and tag not in seen:
                seen.append(tag)
    return ",".join(seen)


def save_upload(
    session: Session, *, filename: str, data: bytes, tags: str | None = None
) -> MediaAsset:
    """Store an uploaded fragment held in memory."""
    return save_stream(session, filename=filename, stream=io.BytesIO(data), tags=tags)


def save_stream(
    session: Session, *, filename: str, stream: BinaryIO, tags: str | None = None
) -> MediaAsset:
    """Store an uploaded fragment read in chunks, and register it.

    Never holds the whole file in memory: a 200 MB b-roll clip pulled in with
    one `read()` is 200 MB of the API process, and a handful of simultaneous
    uploads is how that container gets killed on a small server. The bytes go
    straight to a temporary file beside the library while being hashed, and are
    either renamed into place or thrown away — re-uploading a file that is
    already in the library still never leaves a second copy behind.
    """
    limit = get_settings().inserts.max_upload_mb * 1024 * 1024
    kind = kind_for(filename)  # before a byte is written

    library = library_dir()
    digest = hashlib.sha256()
    size = 0
    handle_fd, temp_path = tempfile.mkstemp(dir=library, prefix=".upload-")
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            while chunk := stream.read(UPLOAD_CHUNK_BYTES):
                size += len(chunk)
                if size > limit:
                    raise ValidationError(
                        f"the file is over the {limit / 1e6:.0f} MB limit"
                    )
                digest.update(chunk)
                handle.write(chunk)
        if not size:
            raise ValidationError("the uploaded file is empty")

        checksum = digest.hexdigest()
        existing = by_checksum(session, checksum)
        if existing is not None:
            log.info("asset %s is already in the library; updating its tags", existing.id)
            return set_tags(session, existing.id, _merge_tags(existing.tags, tags))

        safe = media.safe_filename(os.path.basename(filename), "asset")
        path = os.path.abspath(os.path.join(library, f"{checksum[:16]}-{safe}"))
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None and os.path.exists(temp_path):
            os.remove(temp_path)

    return register(
        session, path=path, original_name=os.path.basename(filename),
        tags=tags, kind=kind, checksum=checksum,
    )


def register(
    session: Session,
    *,
    path: str,
    original_name: str = "",
    tags: str | None = None,
    kind: str | None = None,
    checksum: str | None = None,
) -> MediaAsset:
    """Add a file already on disk to the library."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise ValidationError(f"no such file: {path}")

    checksum = checksum or _checksum_of(path)
    existing = by_checksum(session, checksum)
    if existing is not None:
        return set_tags(session, existing.id, _merge_tags(existing.tags, tags))

    probe = montage.analyse(path)
    asset = MediaAsset(
        kind=kind or kind_for(path),
        path=path,
        original_name=(original_name or os.path.basename(path))[:255],
        tags=normalize_tags(tags),
        duration_sec=probe.get("duration"),
        width=probe.get("width"),
        height=probe.get("height"),
        size_bytes=int(probe.get("size_bytes") or 0),
        checksum=checksum,
    )
    session.add(asset)
    session.flush()
    log.info("registered asset %s (%s, %s)", asset.id, asset.kind, asset.original_name)
    return asset


def get(session: Session, asset_id: int) -> MediaAsset:
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise NotFoundError(f"asset {asset_id} not found")
    return asset


def by_checksum(session: Session, checksum: str) -> MediaAsset | None:
    return session.exec(select(MediaAsset).where(MediaAsset.checksum == checksum)).first()


def list_all(session: Session, *, limit: int = 200, offset: int = 0) -> list[MediaAsset]:
    return list(
        session.exec(
            select(MediaAsset)
            .order_by(col(MediaAsset.created_at).desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )


def set_tags(session: Session, asset_id: int, tags: str | None) -> MediaAsset:
    asset = get(session, asset_id)
    asset.tags = normalize_tags(tags)
    asset.updated_at = utc_now()
    session.add(asset)
    session.flush()
    return asset


def delete(session: Session, asset_id: int) -> None:
    asset = get(session, asset_id)
    path = asset.path
    session.delete(asset)
    session.flush()
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as exc:  # pragma: no cover - filesystem edge case
            log.warning("could not delete %s: %s", path, exc)


def options_for_planner(session: Session) -> list[inserts.AssetOption]:
    """The library as the insert planner sees it, least recently used first.

    Assets whose file has gone missing are skipped rather than raising: a
    library with one deleted file should still produce clips.
    """
    rows = session.exec(
        select(MediaAsset).order_by(
            col(MediaAsset.last_used_at).asc().nulls_first(), col(MediaAsset.id).asc()
        )
    ).all()

    options: list[inserts.AssetOption] = []
    for rank, asset in enumerate(rows):
        if not asset.path or not os.path.exists(asset.path):
            log.warning("asset %s points at a missing file: %s", asset.id, asset.path)
            continue
        options.append(
            inserts.AssetOption(
                asset_id=int(asset.id),
                path=asset.path,
                tags=tuple(asset.tag_list),
                duration_sec=float(asset.duration_sec or 0.0),
                still=asset.kind == AssetKind.IMAGE,
                audio=asset.kind == AssetKind.AUDIO,
                last_used_rank=rank,
            )
        )
    return options


def companion_for(session: Session, *, tag: str, seed: int = 0) -> str | None:
    """A video from the library to fill the bottom half of a split screen.

    Rotated by clip rather than taken strictly least-recently-used: renders of
    one job run in parallel and only record their use at the end, so every clip
    of that job would otherwise pick the same background.
    """
    wanted = set(style.tag_aliases(normalize_tags(tag)))
    options = [
        option
        for option in options_for_planner(session)
        if not option.still and not option.audio and wanted.intersection(option.tags)
    ]
    if not options:
        return None
    return options[seed % len(options)].path


def mark_used(session: Session, paths: list[str], *, when: datetime | None = None) -> int:
    """Record that these fragments have just been put on screen.

    Keyed by path because that is what a rendered composition carries — the
    planner's output is deliberately free of database identifiers.
    """
    if not paths:
        return 0
    moment = when or utc_now()
    rows = session.exec(select(MediaAsset).where(col(MediaAsset.path).in_(set(paths)))).all()
    for asset in rows:
        asset.last_used_at = moment
        asset.use_count += 1
        session.add(asset)
    session.flush()
    return len(rows)


def _merge_tags(existing: str, incoming: str | None) -> str:
    return normalize_tags(",".join(part for part in (existing, incoming or "") if part))


def _checksum_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
