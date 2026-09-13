"""The four built-in scenarios produce what the four profiles produce.

The main test of the whole conversion. A profile is four decisions baked into a
Python value and read by a renderer; a scenario is the same four written in a
model somebody can edit. If the two disagree on the same clip, every job that
exists renders differently the day the switch is thrown — and the acceptance
criterion for every stage up to the seventh is that they do not.

The "today" side is assembled from the functions the present pipeline calls —
`single_source`, `make_subtitle_cues`, `client.dress` — rather than from a
restatement of what they do. `plan_vertical_clip` itself cannot be called here
because it probes the file for its shape and its silences, and those two probes
are exactly what a `ClipFacts` supplies. They are passed in; everything after
them is the code that runs in production.
"""
from __future__ import annotations

import dataclasses

import pytest

from app.domain import profiles
from montage import client as montage
from montage import composition as comp
from montage import scenario as sc
from montage import style as style_module
from montage import subtitles as subtitle_builder
from montage.rules.inserts import AssetOption
from montage.scenario import builtin

KEEP = ((0.0, 30.0), (32.0, 60.0), (62.0, 90.0))
SPEECH = [
    {"start": 12.0, "end": 15.0, "text": "смотрите на машину",
     "words": [{"start": 12.0, "end": 13.0, "word": "смотрите"},
               {"start": 13.0, "end": 13.5, "word": "на"},
               {"start": 13.5, "end": 15.0, "word": "машину"}]},
    {"start": 50.0, "end": 53.0, "text": "и на дорогу",
     "words": [{"start": 50.0, "end": 50.5, "word": "и"},
               {"start": 50.5, "end": 51.0, "word": "на"},
               {"start": 51.0, "end": 53.0, "word": "дорогу"}]},
]
LIBRARY = (
    AssetOption(asset_id=1, path="/media/library/machine.mp4", tags=("машину", "broll"),
                duration_sec=8.0, last_used_rank=0),
    AssetOption(asset_id=2, path="/media/library/road.mp4", tags=("дорогу", "broll"),
                duration_sec=6.0, last_used_rank=1),
    AssetOption(asset_id=3, path="/media/library/bed.mp3", tags=("music",),
                duration_sec=300.0, audio=True, last_used_rank=2),
    AssetOption(asset_id=4, path="/media/library/whoosh.wav", tags=("sfx",),
                duration_sec=1.0, audio=True, last_used_rank=3),
    AssetOption(asset_id=5, path="/media/library/gameplay.mp4", tags=("background",),
                duration_sec=600.0, last_used_rank=4),
)
SEED = 7


def style_for(profile_name: str) -> style_module.StyleSpec:
    """The style a job on this profile starts with, as `styles.resolved_for`
    builds it with no preset and no per-job overrides."""
    return style_module.resolve(
        profile=profiles.style_overrides(profiles.get(profile_name))
    )


def facts(style: style_module.StyleSpec, **overrides) -> sc.ClipFacts:
    """One clip, with the two things `plan_vertical_clip` would have probed."""
    base = dict(
        source_path="/media/source.mp4", start_sec=10.0, end_sec=100.0,
        width=1920, height=1080, has_audio=True,
        keep=KEEP if style.pacing.remove_silence else (),
        speech=tuple(SPEECH), assets=LIBRARY,
        title="Заголовок клипа", index=1, seed=SEED,
    )
    return sc.ClipFacts(**{**base, **overrides})


def today(profile_name: str, clip: sc.ClipFacts) -> comp.Composition:
    """What the present pipeline makes of this clip on this profile."""
    style = style_for(profile_name)
    layout = style.framing.layout
    companion = None
    if layout == comp.LAYOUT_SPLIT:
        companion = next(
            (asset.path for asset in clip.assets
             if style.framing.companion_tag in asset.tags),
            None,
        )
        if companion is None:
            # A split screen with nothing for the bottom half falls back to a
            # backdrop rather than failing, which is what the clip is worth.
            layout = comp.LAYOUT_BLUR
    elif layout == comp.LAYOUT_AUTO:
        # What `choose_layout` decided, written out: a source already about as
        # tall and narrow as the canvas is cropped to fill it, anything wider
        # keeps the backdrop. Spelled here rather than called so the two sides
        # do not agree merely by sharing an implementation.
        canvas = comp.canvas_for(style)
        source_aspect = clip.width / clip.height
        wide = source_aspect > (canvas.width / canvas.height) * (
            1.0 + style.framing.fill_tolerance
        )
        layout = comp.LAYOUT_BLUR if wide else comp.LAYOUT_FILL

    draft = comp.single_source(
        clip.source_path, start_sec=clip.start_sec, end_sec=clip.end_sec,
        keep_segments=clip.keep, canvas=comp.canvas_for(style), style=style,
        frame=comp.frame_for_layout(layout), backdrop=layout == comp.LAYOUT_BLUR,
    )
    if companion:
        draft = dataclasses.replace(draft, layers=draft.layers + (comp.Layer(
            source_path=companion, at_sec=0.0, duration_sec=draft.duration_sec,
            frame=comp.BOTTOM_HALF, z=-1,
        ),))

    cues = subtitle_builder.make_subtitle_cues(
        list(clip.speech), timeline_segments=draft.timeline(),
        fallback_text=clip.title, style=style.subtitles,
    )
    draft = dataclasses.replace(
        draft, subtitles=comp.SubtitleSpec(cues=tuple(cues), title_text=clip.title),
    )
    return montage.dress(
        draft, list(clip.assets), seed=clip.seed,
        broll=style.inserts.enabled,
        music=style.audio.music and style.audio.enabled,
        sfx=style.audio.sfx and style.audio.enabled,
    )


def tomorrow(
    profile_name: str, clip: sc.ClipFacts, *, expect: set[str] | None = None
) -> comp.Composition:
    """What the scenario that profile became makes of the same clip.

    Warnings are checked as well as the EDL. A scenario that produced the right
    composition while complaining about something would be telling an operator
    that a clip has a problem it does not have.
    """
    scenario = builtin.for_profile(profile_name, style_for(profile_name))
    composition, notes = sc.compile(scenario, clip)
    assert {note.code for note in notes} == (expect or set()), f"{profile_name}: {notes}"
    return composition


@pytest.mark.parametrize("profile_name", sorted(builtin.BUILTIN))
class TestEveryProfileSurvivesAsAScenario:
    def test_the_spine_is_cut_and_framed_the_same(self, profile_name):
        clip = facts(style_for(profile_name))

        assert tomorrow(profile_name, clip).spine == today(profile_name, clip).spine

    def test_the_same_words_land_at_the_same_times(self, profile_name):
        clip = facts(style_for(profile_name))

        assert tomorrow(profile_name, clip).subtitles == today(profile_name, clip).subtitles

    def test_the_same_pictures_go_over_it(self, profile_name):
        clip = facts(style_for(profile_name))

        assert tomorrow(profile_name, clip).stack == today(profile_name, clip).stack

    def test_the_same_sounds_play_under_it(self, profile_name):
        clip = facts(style_for(profile_name))

        assert tomorrow(profile_name, clip).audio == today(profile_name, clip).audio

    def test_the_whole_edl_is_the_same(self, profile_name):
        """The four above are here to say *where* a difference is when there is
        one; this is the claim."""
        clip = facts(style_for(profile_name))

        assert tomorrow(profile_name, clip) == today(profile_name, clip)

    def test_and_on_a_source_that_is_already_vertical(self, profile_name):
        clip = facts(style_for(profile_name), width=1080, height=1920)

        assert tomorrow(profile_name, clip) == today(profile_name, clip)

    def test_and_on_a_clip_too_short_for_everything_in_it(self, profile_name):
        clip = facts(
            style_for(profile_name), end_sec=22.0,
            keep=((0.0, 5.0), (7.0, 12.0)) if style_for(profile_name).pacing.remove_silence else (),
        )

        assert tomorrow(profile_name, clip) == today(profile_name, clip)

    def test_and_with_an_empty_library(self, profile_name):
        """The profiles fall silent; the scenario says which tag it wanted.

        That difference is the point of the conversion rather than a breach of
        it: "the library has nothing tagged music" is something an operator can
        act on, and a clip that quietly came out without its soundtrack is not.
        The composition is still identical.
        """
        style = style_for(profile_name)
        wants_assets = style.inserts.enabled or (
            style.audio.enabled and (style.audio.music or style.audio.sfx)
        ) or style.framing.layout == comp.LAYOUT_SPLIT
        clip = facts(style, assets=())

        expected = {"no_asset"} if wants_assets else set()
        if style.framing.layout == comp.LAYOUT_SPLIT:
            expected.add("no_companion")

        assert tomorrow(profile_name, clip, expect=expected) == today(profile_name, clip)


class TestWhatEachScenarioCostsToRender:
    """§6.1's promise, once the pipeline actually asks.

    Trap 18 recorded the bill the scene cutter left behind when it stopped
    buying a transcript it never read: the subtitles paid it instead, one
    Whisper pass per clip at render time, which the README calls the slowest
    thing in the pipeline. This is where that is settled — not by fixing it,
    but by making it unreproducible. A montage states what it needs and nobody
    computes the rest.
    """

    def wants(self, profile_name: str) -> set[str]:
        scenario = builtin.for_profile(profile_name, style_for(profile_name))
        return {kind.value for kind in sc.required_facts(scenario)}

    def test_a_film_still_asks_because_it_still_burns_subtitles(self):
        """Worth stating plainly, because it is the tempting wrong claim.

        Laziness does not make the `film` profile cheap: `film` burns subtitles
        like the rest, so it wants word timings like the rest, and since it
        never transcribed the whole source it pays per clip. What changed is
        that the cost is now *visible* and attached to the element that causes
        it, rather than being an emergent property of two settings nobody
        connected.
        """
        assert "cues" in self.wants("film")

    def test_a_talking_clip_does_ask(self):
        assert "cues" in self.wants("talking")

    def test_b_roll_asks_even_with_the_subtitles_off(self):
        """It is placed on the words that name it, so the words are the input
        whether or not anybody sees them."""
        style = dataclasses.replace(
            style_for("talking"),
            subtitles=dataclasses.replace(style_for("talking").subtitles, enabled=False),
        )
        scenario = builtin.talking(style)

        assert sc.FactKind.CUES in sc.required_facts(scenario)

    def test_turning_everything_off_leaves_one_probe(self):
        """The lever §9.1 promised: take the subtitles out of a montage and
        the transcription goes with them, with no second setting to remember."""
        bare = style_for("talking")
        for group, fields in (
            ("subtitles", {"enabled": False}), ("pacing", {"remove_silence": False}),
            ("inserts", {"enabled": False}), ("audio", {"enabled": False}),
        ):
            bare = dataclasses.replace(
                bare, **{group: dataclasses.replace(getattr(bare, group), **fields)}
            )

        wants = {k.value for k in sc.required_facts(builtin.talking(bare))}

        assert wants == {"duration", "dimensions"}

    def test_the_pipeline_asks_before_it_buys(self):
        """The question crosses the seam ahead of the words, not after them.

        A montage that wants a transcript says so and gets one; a montage that
        does not is never sent looking. That ordering is the whole saving —
        `select_subtitle_transcript` is where the Whisper pass lives.
        """
        bare = style_for("film")
        bare = dataclasses.replace(
            bare, subtitles=dataclasses.replace(bare.subtitles, enabled=False)
        )

        assert montage.wants_transcript(builtin.film(bare)) is False
        assert montage.wants_transcript(
            builtin.for_profile("talking", style_for("talking"))
        ) is True


class TestTheSavingIsRealAndNotJustDeclared:
    """`required_facts` is only worth something if the pipeline honours it."""

    def compose(self, monkeypatch, *, subtitles: bool):
        """Compose one clip, with every route to ASR booby-trapped."""
        from app.adapters.asr import selection
        from app.services import rendering

        def unreachable(*a, **k):
            raise AssertionError(
                "a montage with no use for words went looking for them anyway"
            )

        monkeypatch.setattr(selection, "select_subtitle_transcript", unreachable)
        monkeypatch.setattr(rendering, "cached_segments", unreachable)

        style = style_for("film")
        style = dataclasses.replace(
            style, subtitles=dataclasses.replace(style.subtitles, enabled=subtitles)
        )
        clip = facts(style)

        class Spec:
            id = clip.seed
            index = clip.index
            start_sec = clip.start_sec
            end_sec = clip.end_sec
            title = clip.title
            text = ""
            original_path = clip.source_path
            display_title = clip.title

        return rendering.compose_clip(
            clip=Spec(), job=Spec(), options={}, style=style,
            scenario_name="film",
            library=rendering.Library(options=list(LIBRARY)),
            seed=clip.seed,
        )

    def test_nothing_reaches_asr_for_a_montage_without_subtitles(self, monkeypatch):
        plan = self.compose(monkeypatch, subtitles=False)

        assert plan.composition.subtitles is None
        assert plan.subtitle_source == "not_needed"

    def test_and_a_montage_with_them_does_go_and_get_them(self, monkeypatch):
        """The other half of the claim: the trap fires when it should."""
        with pytest.raises(AssertionError, match="went looking"):
            self.compose(monkeypatch, subtitles=True)
