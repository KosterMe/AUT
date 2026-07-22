from __future__ import annotations

import re
from typing import Any

from app.core.config import get_settings


MAX_TIKTOK_CAPTION = 2200
DEFAULT_HASHTAGS = ("#нарезка", "#моменты", "#nowkie", "#новки", "@nowkie", "#fyp")
FALLBACK_TITLE = "Видео"
MAX_HASHTAGS = 12
MAX_KEYWORD_HASHTAGS = 3
STOPWORDS = {
    "а",
    "без",
    "был",
    "была",
    "были",
    "было",
    "в",
    "во",
    "вот",
    "все",
    "для",
    "его",
    "если",
    "или",
    "как",
    "когда",
    "кто",
    "на",
    "не",
    "но",
    "она",
    "они",
    "оно",
    "при",
    "так",
    "там",
    "что",
    "это",
    "этот",
    "часть",
    "видео",
    "нарезка",
    "момент",
    "моменты",
    "the",
    "and",
    "for",
    "from",
    "full",
    "generated",
    "rendered",
    "clip",
    "fragment",
    "фрагмент",
    "long",
    "name",
    "part",
    "replace",
    "short",
    "slice",
    "text",
    "title",
    "transcript",
    "video",
    "with",
}
TOPIC_RULES = (
    (("#атакаТитанов", "#аниме"), ("атака титанов", "attack on titan", "эрен", "армин", "микаса", "леви", "титан")),
    (("#аниме",), ("аниме", "anime", "манга", "мангака", "сезон", "серия")),
    (("#кино", "#сериал"), ("фильм", "кино", "сериал", "режиссер", "сцена", "эпизод", "movie", "film", "series")),
    (("#fortnite", "#игры", "#gaming"), ("fortnite", "фортнайт")),
    (("#игры", "#gaming"), ("игра", "игры", "game", "gaming", "minecraft", "майнкрафт", "roblox", "роблокс")),
    (("#юмор", "#мемы"), ("юмор", "смешно", "мем", "мемы", "прикол", "funny", "meme")),
    (("#факты",), ("факт", "факты", "интересно", "объяснение", "разбор", "теория")),
    (("#спорт",), ("спорт", "матч", "футбол", "баскетбол", "хоккей", "football", "soccer", "basketball")),
    (("#музыка",), ("музыка", "песня", "трек", "концерт", "music", "song", "track")),
)


def build_tiktok_caption(
    *,
    title: str | None,
    text: str | None,
    source_title: str | None = None,
    index: int | None = None,
    custom_tags: tuple[str, ...] = (),
) -> str:
    caption_title = _caption_title(source_title=source_title, title=title, index=index)
    hashtags = build_hashtags(
        source_title=source_title,
        title=title,
        text=text,
        custom_tags=custom_tags,
    )
    caption = f"{caption_title}\n\n{' '.join(hashtags)}"
    return caption[:MAX_TIKTOK_CAPTION].strip()


def caption_for_clip(
    *,
    source_title: str | None,
    clip_title: str | None,
    clip_text: str | None,
    index: int | None,
    caption_tags: str | None = None,
) -> str:
    """The caption a clip is published with.

    First line names the video and the part number; the rest are hashtags. Per
    job `caption_tags`, when set, are used *exclusively* — the user asked for
    those tags, so auto-detected topic tags would only dilute them.
    """
    return build_tiktok_caption(
        title=clip_title,
        text=clip_text,
        source_title=source_title,
        index=index,
        custom_tags=parse_tags(caption_tags or ""),
    )


def parse_tags(raw: str) -> tuple[str, ...]:
    tags: list[str] = []
    for chunk in re.split(r"[\s,]+", raw.strip()):
        tag = _normalize_hashtag(chunk)
        if tag and tag not in tags:
            tags.append(tag)
    return tuple(tags)


def clip_headline(
    *,
    source_title: str | None,
    title: str | None = None,
    index: int | None = None,
) -> str:
    """On-screen headline for a clip: the original video name plus the part.

    Mirrors the first line of the TikTok caption (``build_tiktok_caption``) so the
    text burned onto the video and cover matches what viewers read in the
    description.
    """
    return _caption_title(source_title=source_title, title=title, index=index)


def _caption_title(*, source_title: str | None, title: str | None, index: int | None) -> str:
    base = _display_title(source_title) or _display_title(title) or FALLBACK_TITLE
    part = f"часть {index}" if index else "часть"
    return f"{base} - {part}"


def display_title(source_title: str | None, title: str | None = None) -> str:
    """Clean, human-readable video name (no extension/path), for on-screen use."""
    return _display_title(source_title) or _display_title(title) or FALLBACK_TITLE


def _display_title(value: str | None) -> str:
    text = _clean_text(value or "")
    if not text:
        return ""
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = re.sub(r"\.(?:mp4|mov|mkv|webm|avi|m4v)$", "", text, flags=re.IGNORECASE)
    return text[:160].strip(" -_")


def common_hashtags() -> tuple[str, ...]:
    return parse_tags(get_settings().tiktok.default_hashtags) or DEFAULT_HASHTAGS


def build_hashtags(
    *,
    source_title: str | None,
    title: str | None,
    text: str | None,
    custom_tags: tuple[str, ...] = (),
) -> tuple[str, ...]:
    # Manual per-video tags are exclusive: when set, they are the whole tag
    # list for every clip of that video (no auto topic/keyword detection mixed in).
    if custom_tags:
        return tuple(custom_tags[:MAX_HASHTAGS])

    tags: list[str] = []
    source_text = _clean_text(source_title or "").lower()
    _append_tags(tags, _topic_tags(source_text))
    if not tags:
        title_text = _clean_text(title or "").lower()
        _append_tags(tags, _topic_tags(title_text))
    if not tags:
        combined = _clean_text(" ".join(value for value in (source_title, title, text) if value)).lower()
        _append_tags(tags, _topic_tags(combined))

    if not tags:
        primary_text = source_title or title or text or ""
        _append_tags(tags, _keyword_hashtags(primary_text))

    _append_tags(tags, common_hashtags())
    return tuple(tags[:MAX_HASHTAGS])


def _topic_tags(text: str) -> tuple[str, ...]:
    tags: list[str] = []
    for topic_tags, needles in TOPIC_RULES:
        if any(needle in text for needle in needles):
            _append_tags(tags, topic_tags)
    return tuple(tags)


def _normalize_hashtag(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if not value.startswith(("#", "@")):
        value = f"#{value}"
    return re.sub(r"[^\w#@]", "", value, flags=re.UNICODE)[:80]


def _keyword_hashtags(text: str) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for position, word in enumerate(re.findall(r"[A-Za-zА-Яа-яЁё0-9]{4,}", _clean_text(text).lower())):
        normalized = word.replace("ё", "е")
        if normalized in STOPWORDS or normalized.isdigit():
            continue
        counts[normalized] = counts.get(normalized, 0) + 1
        first_seen.setdefault(normalized, position)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], first_seen[item[0]]))
    return tuple(f"#{word}" for word, _count in ordered[:MAX_KEYWORD_HASHTAGS])


def _append_tags(target: list[str], source: tuple[str, ...]) -> None:
    for value in source:
        tag = _normalize_hashtag(value)
        if tag and tag not in target:
            target.append(tag)


def _clean_text(text: str) -> str:
    text = re.sub(r"\[[^\]]+\]", " ", text)
    text = text.replace(">>", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()

