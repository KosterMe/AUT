"""Deciding, without a person, where b-roll goes.

The pipeline renders unattended, so "which fragment goes on screen at which
second" has to fall out of rules rather than out of somebody's judgement. The
strongest signal available is the one the clip already carries: word-level
subtitle cues, which say exactly when each word is spoken. An asset tagged
`ferrari` can therefore be put on screen at the instant the word is said,
rather than somewhere in the vicinity.

Everything here is pure. It takes a composition, a library and a policy, and
returns inserts — no database, no filesystem, no ffmpeg — because the part most
likely to need tuning is the part that should be cheapest to test.

Two rules that are easy to get wrong and expensive to discover in a finished
clip:

* **Nothing covers the hook.** The first seconds decide whether anybody watches
  the rest, and they are the speaker's, not the b-roll's.
* **The same clip renders the same way twice.** Choices are seeded by the clip,
  never by the clock or by iteration order over a dict, so a re-render produces
  the same edit and the fragment cache stays meaningful.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.domain.composition import INSERT_FULL, INSERT_KINDS, Composition, Insert
from app.domain.subtitles import SubtitleCue

# Shortest tag that is matched by stem rather than exactly. Below this a tag is
# usually an acronym or a brand ("bmw", "ai"), where a prefix match would fire
# on half the dictionary.
MIN_STEM_CHARS = 4
# How much two words may differ in length and still count as the same word.
# Russian inflection mostly changes the tail by a letter or two — маши́на,
# маши́ну, маши́не — while "стол" and "столица" differ by three and are not
# related. A cheap approximation of a lemmatiser, and deliberately so: the
# alternative is a morphology dependency in the domain layer.
MAX_STEM_LENGTH_DIFFERENCE = 2


@dataclass(frozen=True)
class AssetOption:
    """One item of the library, as the planner sees it.

    `last_used_rank` orders the library by how recently each item was put on
    screen, 0 being the least recently used. Rotating through it is what stops
    one asset appearing in every clip of a job.
    """

    asset_id: int
    path: str
    tags: tuple[str, ...] = ()
    duration_sec: float = 0.0
    still: bool = False
    # A music bed or a sound effect: it has no picture, so it can never be an
    # insert. Kept on the same value object because the library is one library.
    audio: bool = False
    last_used_rank: int = 0


@dataclass(frozen=True)
class InsertPolicy:
    """How much b-roll a clip may carry, and where it may go."""

    kind: str = INSERT_FULL
    max_inserts: int = 4
    min_seconds: float = 1.5
    max_seconds: float = 3.5
    # Space between inserts. Without it a paragraph dense in keywords turns
    # into a slideshow.
    min_gap_seconds: float = 6.0
    hook_guard_seconds: float = 2.5
    tail_guard_seconds: float = 1.5
    # Ceiling on how much of the clip may be covered, as a fraction.
    max_share: float = 0.35
    # Used only when nothing matched by keyword: place inserts on a fixed beat.
    cadence_seconds: float = 12.0
    cadence_when_no_match: bool = True

    @classmethod
    def from_settings(cls) -> "InsertPolicy":
        configured = get_settings().inserts
        return cls(
            kind=configured.kind,
            max_inserts=configured.max_per_clip,
            min_seconds=configured.min_seconds,
            max_seconds=configured.max_seconds,
            min_gap_seconds=configured.min_gap_seconds,
            hook_guard_seconds=configured.hook_guard_seconds,
            tail_guard_seconds=configured.tail_guard_seconds,
            max_share=configured.max_share,
            cadence_seconds=configured.cadence_seconds,
            cadence_when_no_match=configured.cadence_when_no_match,
        )

    def __post_init__(self) -> None:
        if self.kind not in INSERT_KINDS:
            raise ValueError(f"unknown insert kind {self.kind!r}")
        if self.min_seconds > self.max_seconds:
            raise ValueError("min_seconds must not exceed max_seconds")


@dataclass(frozen=True)
class _Slot:
    """A moment that could carry an insert, and what could fill it.

    Candidates rather than a single asset: which one actually appears cannot be
    settled until the walk reaches this moment and knows what the clip has
    already shown.
    """

    at_sec: float
    candidates: tuple[AssetOption, ...]
    order: int = field(default=0, compare=False)


def choose_inserts(
    composition: Composition,
    *,
    assets: list[AssetOption],
    policy: InsertPolicy | None = None,
    seed: int = 0,
) -> tuple[Insert, ...]:
    """Pick the b-roll for one clip.

    Keyword hits come first; if the clip's words match nothing in the library —
    or it has no words at all, which is what happens with subtitles disabled —
    the cadence fallback places inserts on a fixed beat instead, so a library
    still gets used on material it has no vocabulary for.
    """
    rules = policy or InsertPolicy()
    if not assets or composition.duration_sec <= 0:
        return ()

    visible = [asset for asset in assets if not asset.audio]
    if not visible:
        return ()

    cues = composition.subtitles.cues if composition.subtitles else ()
    slots = _keyword_slots(cues, visible, seed=seed)
    if not slots and rules.cadence_when_no_match:
        slots = _cadence_slots(composition.duration_sec, visible, rules, seed=seed)

    return _accept(slots, composition.duration_sec, rules)


def normalize(word: str) -> str:
    """A word reduced to the letters that carry its identity."""
    lowered = word.strip().lower().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я]+", "", lowered)


def matches(word: str, tag: str) -> bool:
    """Whether a spoken word is the tag, allowing for inflection.

    Prefix comparison rather than lemmatisation: tags are written by the person
    who uploaded the asset, so exactness matters less than not needing a
    morphological analyser inside the domain layer.
    """
    left, right = normalize(word), normalize(tag)
    if not left or not right:
        return False
    if left == right:
        return True
    if len(right) < MIN_STEM_CHARS or len(left) < MIN_STEM_CHARS:
        return False
    if abs(len(left) - len(right)) > MAX_STEM_LENGTH_DIFFERENCE:
        return False
    shared = 0
    for a, b in zip(left, right):
        if a != b:
            break
        shared += 1
    return shared >= MIN_STEM_CHARS


def _keyword_slots(
    cues: tuple[SubtitleCue, ...], assets: list[AssetOption], *, seed: int
) -> list[_Slot]:
    slots: list[_Slot] = []
    for order, cue in enumerate(cues):
        # A cue is usually a single word — that is the karaoke style — but the
        # fallback path used when a transcript has no word timings puts a whole
        # phrase in one cue. Matching every word in it keeps the library
        # working on both.
        spoken = cue.text.split()
        matched = [
            (asset, tag)
            for asset in assets
            for tag in asset.tags
            if any(matches(word, tag) for word in spoken)
        ]
        if not matched:
            continue
        # A longer tag is a more specific claim on the moment; then the least
        # recently used; then a stable hash, never dict or set order.
        ranked = sorted(
            matched,
            key=lambda pair: (
                -len(normalize(pair[1])),
                pair[0].last_used_rank,
                _tiebreak(pair[0], seed),
            ),
        )
        slots.append(
            _Slot(
                at_sec=cue.start_sec,
                candidates=tuple(_unique(asset for asset, _ in ranked)),
                order=order,
            )
        )
    return slots


def _cadence_slots(
    duration_sec: float, assets: list[AssetOption], policy: InsertPolicy, *, seed: int
) -> list[_Slot]:
    """Evenly spaced inserts, for clips whose words match nothing.

    Every slot offers the whole library in rotation order; the walk takes the
    first item each one has not already shown, so the beats fill themselves
    with different fragments without any bookkeeping here.
    """
    rotation = tuple(
        sorted(assets, key=lambda a: (a.last_used_rank, _tiebreak(a, seed)))
    )
    slots: list[_Slot] = []
    at = policy.hook_guard_seconds
    index = 0
    while at < duration_sec - policy.tail_guard_seconds - policy.min_seconds:
        slots.append(_Slot(at_sec=round(at, 3), candidates=rotation, order=index))
        at += max(policy.cadence_seconds, policy.min_gap_seconds)
        index += 1
    return slots


def _accept(slots: list[_Slot], duration_sec: float, policy: InsertPolicy) -> tuple[Insert, ...]:
    """Walk the candidates in time order, keeping the ones the rules allow."""
    latest_start = duration_sec - policy.tail_guard_seconds - policy.min_seconds
    budget = duration_sec * policy.max_share

    accepted: list[Insert] = []
    used_assets: set[int] = set()
    covered = 0.0
    previous_end = 0.0

    for slot in sorted(slots, key=lambda s: (s.at_sec, s.order)):
        if len(accepted) >= policy.max_inserts:
            break
        if slot.at_sec < policy.hook_guard_seconds or slot.at_sec > latest_start:
            continue
        if accepted and slot.at_sec - previous_end < policy.min_gap_seconds:
            continue

        # One appearance per fragment per clip. When everything that fits this
        # moment has already been shown, the moment goes uncovered: the same
        # footage twice in one clip reads as a mistake, and a plain stretch of
        # the speaker does not.
        asset = next((option for option in slot.candidates
                      if option.asset_id not in used_assets), None)
        if asset is None:
            continue

        room = duration_sec - policy.tail_guard_seconds - slot.at_sec
        length = _length_for(asset, policy, room=room)
        if length < policy.min_seconds or covered + length > budget:
            continue

        accepted.append(
            Insert(
                kind=policy.kind,
                source_path=asset.path,
                at_sec=round(slot.at_sec, 3),
                duration_sec=round(length, 3),
                still=asset.still,
            )
        )
        used_assets.add(asset.asset_id)
        covered += length
        previous_end = slot.at_sec + length

    return tuple(accepted)


def _unique(options) -> list[AssetOption]:
    """Drop repeats while keeping order — an asset can match on several tags."""
    seen: set[int] = set()
    result: list[AssetOption] = []
    for option in options:
        if option.asset_id not in seen:
            seen.add(option.asset_id)
            result.append(option)
    return result


def _tiebreak(asset: AssetOption, seed: int) -> int:
    """A stable shuffle: same clip, same order, every time."""
    return (asset.asset_id * 2654435761 + seed) % 1_000_003


def _length_for(asset: AssetOption, policy: InsertPolicy, *, room: float) -> float:
    """How long this asset stays on screen.

    A still has no duration of its own, so it takes the policy's maximum; a
    video shorter than the maximum plays out rather than being cut mid-motion.
    """
    natural = policy.max_seconds if asset.still or asset.duration_sec <= 0 else asset.duration_sec
    return min(max(policy.min_seconds, min(natural, policy.max_seconds)), room)


__all__ = [
    "AssetOption",
    "InsertPolicy",
    "choose_inserts",
    "matches",
    "normalize",
]
