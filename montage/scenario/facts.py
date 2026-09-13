"""What a scenario needs to know about a clip, and nothing more.

Facts cost three orders of magnitude apart from each other:

| fact             | how it is got                  | cost             |
| ---------------- | ------------------------------ | ---------------- |
| length, size     | `ffprobe`                      | milliseconds     |
| has audio        | `ffprobe`                      | milliseconds     |
| library assets   | one query                      | milliseconds     |
| cuts on silence  | `silencedetect` over the clip  | seconds          |
| loudness curve   | a pass over the clip           | seconds          |
| word timings     | Whisper over the clip          | **tens of seconds on a CPU** |

Computing all of them to throw half away is what the pipeline does today, and
the bottom row is why it matters: the `film` profile skips the whole-source
transcript and then sends every clip through Whisper individually, which the
README calls the slowest thing in the pipeline.

So a scenario states what it needs and only that is computed. Crucially the
statement is *derived* from the scenario rather than configured beside it:
adding subtitles turns transcription on by itself, and taking them out turns it
off. Today those are two independent switches — `burn_subtitles` and
`transcript_mode` — that can be set inconsistently, and are.

`required_facts` is also what the cutting stage can be asked *before* it runs.
The speech cutter needs a transcript for its own work; if the scenario wants
word timings too, the transcript is computed once and shared. If the cutter
reads the picture and the scenario does not ask for words, ASR never runs at
all for that job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator, Protocol

from montage.rules.inserts import AssetOption
from montage.scenario import model
from montage.subtitles import SubtitleCue


class FactKind(str, Enum):
    DURATION = "duration"        # always needed: the spine is laid out in it
    DIMENSIONS = "dimensions"    # width and height of the source
    HAS_AUDIO = "has_audio"
    CUTS = "cuts"                # silence or scene boundaries, in output time
    CUES = "cues"                # lines with word timings
    LOUDNESS = "loudness"
    ASSETS = "assets"            # what the library offers this clip


@dataclass(frozen=True)
class ClipFacts:
    """Everything known about one clip when it is about to be rendered."""

    source_path: str = ""
    start_sec: float = 0.0
    end_sec: float = 0.0
    width: int = 0
    height: int = 0
    has_audio: bool = False
    # The stretches worth keeping, silence taken out, relative to `start_sec`.
    # The design sketch (§6) has this as a flat tuple of cut times in output
    # time, which cannot express a removed pause — the gaps between the kept
    # windows are exactly what silence removal produces, and a list of instants
    # has nowhere to put them. Cut times are derived from these instead, so one
    # `silencedetect` pass still answers both questions. See trap 20.
    keep: tuple[tuple[float, float], ...] = ()
    # Word-timed transcript lines, in the shape `subtitles.make_subtitle_cues`
    # consumes. Not finished `SubtitleCue`s: a cue is timed against the output
    # timeline, and the compiler has not laid the spine out yet when it reads
    # this. Same trap.
    speech: tuple[Any, ...] = ()
    loudness: tuple[tuple[float, float], ...] = ()
    assets: tuple[AssetOption, ...] = ()
    title: str = ""
    index: int = 1
    seed: int = 0

    @property
    def duration_sec(self) -> float:
        return round(max(0.0, self.end_sec - self.start_sec), 3)

    @property
    def cuts(self) -> tuple[float, ...]:
        """Where the joins land in output time, for `event(cut=N)` anchors.

        Cumulative: dropping a pause moves everything after it earlier, so the
        nth join is at the sum of the lengths of the windows before it. The
        last window's end is not a cut — it is where the clip stops.
        """
        at: list[float] = []
        cursor = 0.0
        for start, end in self.keep[:-1]:
            cursor += max(0.0, end - start)
            at.append(round(cursor, 3))
        return tuple(at)


class FactProvider(Protocol):
    """Computes one fact on demand. The impure half, injected.

    A protocol rather than a class because the two callers are not alike: the
    render worker may spend a minute on Whisper, and the preview endpoint must
    not, so what a fact costs to produce is the caller's business.
    """

    def get(self, kind: FactKind) -> Any: ...


# --- what needs what (§6.1) --------------------------------------------------


def _anchors(scenario: model.Scenario) -> Iterator[model.Anchor]:
    """Every anchor in the scenario, wherever it is hiding.

    Keyframes carry anchors too, and a keyframe timed to a spoken word needs
    the same word timings a subtitle does. Missing one of these means the fact
    is not computed and the anchor silently resolves to zero.
    """
    for element in scenario.elements:
        templates = [element]
        if isinstance(element, model.RuleElement) and element.template is not None:
            templates.append(element.template)
        for item in templates:
            if isinstance(item, model.RuleElement):
                continue
            yield item.start
            if item.duration.until is not None:
                yield item.duration.until
            for value in (item.frame.x, item.frame.y, item.frame.width,
                          item.frame.height, item.frame.rotate, item.frame.opacity):
                for key in value.keys:
                    yield key.at


def _events(scenario: model.Scenario) -> Iterator[model.EventRef]:
    for anchor in _anchors(scenario):
        if anchor.event is not None:
            yield anchor.event
    for element in scenario.elements:
        if isinstance(element, model.Element) and element.slot.event is not None:
            yield element.slot.event


def _slots(scenario: model.Scenario) -> Iterator[model.Slot]:
    for element in scenario.elements:
        if isinstance(element, model.Element):
            yield element.slot
        elif element.template is not None:
            yield element.template.slot


def _frames(scenario: model.Scenario) -> Iterator[model.Frame]:
    for element in scenario.elements:
        if isinstance(element, model.Element):
            yield element.frame
        elif element.template is not None:
            yield element.template.frame


def _rules(scenario: model.Scenario) -> Iterator[model.RuleElement]:
    for element in scenario.elements:
        if isinstance(element, model.RuleElement):
            yield element


def required_facts(scenario: model.Scenario) -> frozenset[FactKind]:
    """What this scenario needs to know about a clip. A pure function.

    Derived, never declared: a scenario cannot ask for Whisper and then not
    use it, or use word timings without asking. That equivalence is the whole
    point — it is what makes removing the subtitles from a scenario switch
    transcription off with no second setting to remember.
    """
    needed = {FactKind.DURATION}
    style = scenario.style
    events = [event.kind for event in _events(scenario)]
    rules = [rule.rule for rule in _rules(scenario)]
    slots = list(_slots(scenario))

    if style.subtitles.enabled:
        needed.add(FactKind.CUES)
    if model.RULE_KEYWORD_BROLL in rules:
        # b-roll is placed against the words that name it, so it needs them.
        needed.add(FactKind.CUES)
    if {"word", "sentence"} & set(events):
        needed.add(FactKind.CUES)

    if style.pacing.remove_silence:
        needed.add(FactKind.CUTS)
    if model.RULE_ON_EVERY_CUT in rules:
        needed.add(FactKind.CUTS)
    if {"cut", "silence"} & set(events):
        needed.add(FactKind.CUTS)

    if model.RULE_ON_LOUDEST in rules or "loudest" in events:
        needed.add(FactKind.LOUDNESS)

    if any(frame.fit in (model.FIT_AUTO, model.FIT_COVER, model.FIT_CONTAIN)
           for frame in _frames(scenario)):
        needed.add(FactKind.DIMENSIONS)
    if any(slot.kind == model.SLOT_BLUR_OF for slot in slots):
        needed.add(FactKind.DIMENSIONS)

    if any(slot.kind == model.SLOT_LIBRARY for slot in slots):
        needed.add(FactKind.ASSETS)
    if style.audio.enabled and (style.audio.music or style.audio.sfx):
        needed.add(FactKind.ASSETS)

    if _mixes_sound(scenario):
        # Laying music under a source that turns out to be silent is a
        # different graph from laying it under one that is not.
        needed.add(FactKind.HAS_AUDIO)

    return frozenset(needed)


def _mixes_sound(scenario: model.Scenario) -> bool:
    if scenario.style.audio.enabled and (
        scenario.style.audio.music or scenario.style.audio.sfx
    ):
        return True
    return any(
        element.audio.enabled
        for element in scenario.elements
        if isinstance(element, model.Element)
    )


def describes(needed: frozenset[FactKind]) -> tuple[str, ...]:
    """What this scenario costs, in words, for the editor to show beside it.

    The price of an element has to be visible before it is applied to a
    hundred clips — and watching Whisper disappear from this line when one
    element is removed is the most convincing explanation of the trade there
    is.
    """
    labels = {
        FactKind.CUES: "тайминги слов (Whisper)",
        FactKind.CUTS: "склейки",
        FactKind.LOUDNESS: "кривая громкости",
        FactKind.DIMENSIONS: "размеры источника",
        FactKind.ASSETS: "библиотека ассетов",
        FactKind.HAS_AUDIO: "наличие звука",
    }
    return tuple(labels[kind] for kind in FactKind if kind in needed and kind in labels)


# --- asking for only what is needed ------------------------------------------

# Field on ClipFacts each fact kind fills in.
_FIELDS = {
    FactKind.DIMENSIONS: ("width", "height"),
    FactKind.HAS_AUDIO: ("has_audio",),
    FactKind.CUTS: ("keep",),
    FactKind.CUES: ("speech",),
    FactKind.LOUDNESS: ("loudness",),
    FactKind.ASSETS: ("assets",),
}


@dataclass
class CachingProvider:
    """Wraps a callable per fact and computes each at most once.

    The cache is the point: `required_facts` is asked once, but a fact can be
    read several times while a scenario compiles, and re-running Whisper
    because two elements wanted the same words would undo the whole exercise.
    """

    sources: dict[FactKind, Callable[[], Any]] = field(default_factory=dict)
    _cache: dict[FactKind, Any] = field(default_factory=dict, init=False)

    def get(self, kind: FactKind) -> Any:
        if kind not in self._cache:
            source = self.sources.get(kind)
            self._cache[kind] = source() if source is not None else None
        return self._cache[kind]

    @property
    def computed(self) -> frozenset[FactKind]:
        """What was actually asked for — the measurable half of laziness."""
        return frozenset(self._cache)


def resolve(
    needed: frozenset[FactKind],
    provider: FactProvider,
    *,
    base: ClipFacts,
) -> ClipFacts:
    """Fill in `base` with the facts `needed` names, and only those.

    `base` carries what is already known without asking anybody — the path,
    the window, the title, the seed. Everything else is bought from the
    provider one fact at a time.
    """
    import dataclasses

    changes: dict[str, Any] = {}
    for kind, fields in _FIELDS.items():
        if kind not in needed:
            continue
        value = provider.get(kind)
        if value is None:
            continue
        if len(fields) == 1:
            changes[fields[0]] = value
        else:
            changes.update(dict(zip(fields, value)))
    return dataclasses.replace(base, **changes) if changes else base
