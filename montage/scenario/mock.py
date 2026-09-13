"""A clip that does not exist, described well enough to lay a scenario out on.

The layout switcher is the editor's most important control (§8.2): a scenario
that has only ever been seen at one length has not been checked. Switching to
30 or 180 seconds means compiling it against a clip nobody has — so the facts
for that clip are made up here, and made up in one place, because two
different mocks would give two different answers to "what does this do on a
short one".

What is invented and what is not: the shape of the material is invented (its
length, its size, where it joins, what is said), the library is **not** — the
real assets are passed in. A scenario whose b-roll rule can never fire because
the library is empty should show exactly that, and inventing fragments to make
the picture look busy would hide the one thing the operator needs to see.
"""
from __future__ import annotations

from montage.rules.inserts import AssetOption
from montage.scenario import facts as fact_module, model

# Not a real path, and deliberately not one: nothing reads the mock's source,
# and a mock that named a file on disk would eventually be handed to ffmpeg by
# somebody who did not know it was a mock.
SOURCE = "mock://source"

# The lengths the editor offers (§8.2). Not a limit — any duration compiles —
# but these are the ones worth a button, because they are the lengths clips
# actually come out at.
DURATIONS = (30.0, 60.0, 90.0, 120.0, 180.0)

# How often the invented material says something, and how long a line lasts.
_LINE_EVERY_SEC = 5.0
_LINE_LENGTH_SEC = 3.2
# What it says when the library has no tags to borrow. Neutral on purpose: a
# mock that says real words invites reading meaning into where a rule fired.
_FILLER = ("раз", "два", "три", "четыре")


def facts(
    scenario: model.Scenario,
    duration_sec: float | None = None,
    *,
    assets: tuple[AssetOption, ...] = (),
    seed: int = 0,
) -> fact_module.ClipFacts:
    """Facts for a clip of this length that does not exist.

    `duration_sec` overrides the scenario's own mock length, which is what the
    layout switcher changes. Everything else comes from the scenario's
    `MockClip`, because those are properties of the material the scenario was
    written for and they do not change when the length does.
    """
    mock = scenario.mock
    length = round(float(duration_sec if duration_sec is not None else mock.duration_sec), 3)
    length = max(1.0, length)
    return fact_module.ClipFacts(
        source_path=SOURCE,
        start_sec=0.0,
        end_sec=length,
        width=mock.width,
        height=mock.height,
        has_audio=True,
        keep=_windows(mock.cuts, length),
        speech=_speech(length, assets),
        loudness=tuple((float(second), -20.0) for second in range(int(length) + 1)),
        assets=assets,
        title="Пример",
        index=1,
        seed=seed,
    )


def _windows(cuts: tuple[float, ...], length: float) -> tuple[tuple[float, float], ...]:
    """The kept stretches, from the joins the mock says the material has.

    A mock with no cuts is one continuous window, which is what a clip with no
    silence removed looks like — and it has to be spelled out rather than left
    empty, because an empty `keep` means "nothing was measured" and the spine
    would be laid out over the raw length instead (trap 22).
    """
    inside = sorted(at for at in cuts if 0.0 < at < length)
    edges = [0.0, *inside, length]
    return tuple(
        (round(start, 3), round(end, 3))
        for start, end in zip(edges, edges[1:])
        if end - start > 0.0
    )


def _speech(length: float, assets: tuple[AssetOption, ...]) -> tuple[dict, ...]:
    """Invented lines with word timings, so cues and keyword rules have work.

    The words are the library's own tags when there are any. That looks like a
    trick and is the opposite of one: a keyword rule shown against material
    that never says a keyword draws nothing, and "nothing" is not what the
    scenario does — it is what this particular invented transcript did. The
    mock's job is to show the scenario on material it was written for.
    """
    vocabulary = _vocabulary(assets)
    lines: list[dict] = []
    at = 0.0
    cursor = 0
    while at + _LINE_LENGTH_SEC <= length:
        words = [vocabulary[(cursor + i) % len(vocabulary)] for i in range(4)]
        cursor += len(words)
        step = _LINE_LENGTH_SEC / len(words)
        lines.append({
            "start_sec": round(at, 3),
            "end_sec": round(at + _LINE_LENGTH_SEC, 3),
            "text": " ".join(words),
            "words": [
                {
                    "text": word,
                    "start_sec": round(at + index * step, 3),
                    "end_sec": round(at + (index + 1) * step, 3),
                }
                for index, word in enumerate(words)
            ],
        })
        at += _LINE_EVERY_SEC
    return tuple(lines)


def _vocabulary(assets: tuple[AssetOption, ...]) -> tuple[str, ...]:
    """Every tag the picture library carries, in a stable order."""
    tags = sorted({
        tag.strip().lower()
        for asset in assets if not asset.audio
        for tag in asset.tags if tag.strip()
    })
    return tuple(tags) or _FILLER
