"""The compiler: a composition in, ffmpeg arguments out.

A filter graph is the compiler's real output, so these tests read it directly
rather than rendering frames — which is also the only way to check the things
that cost the most when they are wrong (where the decoder seeks, what a cached
fragment is allowed to depend on) without a video file and a stopwatch.
"""
from __future__ import annotations

import dataclasses
import os

import pytest

from montage.render import compiler
from montage import composition as comp
from montage import style as style_module

SOURCE = "/media/source.mp4"
BROLL = "/media/broll.mp4"


def build(**overrides) -> comp.Composition:
    kwargs = dict(
        spine=(
            comp.Segment(SOURCE, 60.0, 70.0),
            comp.Segment(SOURCE, 300.0, 310.0),
        ),
        canvas=comp.Canvas(1080, 1920, 30),
    )
    kwargs.update(overrides)
    return comp.Composition(**kwargs)


def one_pass(composition: comp.Composition, *, has_audio: bool = True, subtitles=None) -> str:
    args = compiler.one_pass_args(
        composition,
        "/out/clip.mp4",
        subtitle_path=subtitles,
        audio_by_source={path: has_audio for path in composition.source_paths},
        has_audio=has_audio,
        encoder="libx264",
    )
    return " ".join(str(part) for part in args)


def graph_of(args: list[str]) -> str:
    return args[args.index("-filter_complex") + 1]


# --- where the decoder goes -------------------------------------------------


def test_each_segment_is_seeked_at_its_own_input():
    """The measured reason this compiler exists.

    The previous renderer opened one input spanning the first segment to the
    last and trimmed inside the graph, so the decoder walked every frame in
    between — 30.2 s against 18.4 s on a montage drawn from four points in a
    14-minute source.
    """
    args = compiler.one_pass_args(
        build(), "/out/clip.mp4", subtitle_path=None,
        audio_by_source={SOURCE: True}, has_audio=True, encoder="libx264",
    )

    assert args.count("-i") == 2
    assert ["-ss", "60.000", "-t", "10.000", "-i", SOURCE] == args[2:8]
    assert ["-ss", "300.000", "-t", "10.000", "-i", SOURCE] == args[8:14]
    # Nothing is cut inside the graph any more; the inputs already arrive cut.
    assert "trim=" not in graph_of(args)


def test_a_single_segment_needs_no_concat_at_all():
    graph = one_pass(build(spine=(comp.Segment(SOURCE, 0.0, 30.0),)))

    assert "concat=" not in graph
    # The look pass reads the composed segment straight off, with no join in
    # between — the common case should not pay for machinery it does not use.
    assert "[v0]eq=contrast" in graph
    assert "[a0]loudnorm" in graph


def test_segments_are_concatenated_with_their_audio():
    graph = one_pass(build())

    assert "[v0][a0][v1][a1]concat=n=2:v=1:a=1[vcat][acat]" in graph


# --- framing ----------------------------------------------------------------


def test_foreground_is_zoomed_and_cropped_to_the_canvas_width():
    """The sharp layer is scaled past the canvas width so it grows taller, and
    the overflow comes off the left and right edges."""
    graph = one_pass(build())

    # 1080 * 1.2 -> a 1296-wide box, so a 16:9 source lands 20% taller.
    assert "scale=1296:1920:force_original_aspect_ratio=decrease" in graph
    # min() keeps an already-portrait source, which never reaches 1296 wide,
    # from being cropped to a width it does not have.
    assert "crop=w=min(iw\\,1080):h=min(ih\\,1920)[s0fg]" in graph


def test_foreground_zoom_of_one_fits_the_whole_frame(configure):
    configure(AUTOCLIPS_RENDER_FOREGROUND_ZOOM=1)

    graph = one_pass(build())

    assert "scale=1080:1920:force_original_aspect_ratio=decrease[s0fg]" in graph
    assert "crop=w=min" not in graph


def test_background_is_blurred_at_reduced_resolution():
    """The blur is the most expensive filter in the chain. Doing it small and
    scaling up costs ~16x less and looks the same, because a heavy blur throws
    away exactly the detail the downscale removed."""
    graph = one_pass(build())

    assert "scale=270:480:force_original_aspect_ratio=increase" in graph
    # Radius scales with the resolution, keeping the same visual softness.
    assert "boxblur=6:1,scale=1080:1920[s0bg]" in graph


def test_blur_divisor_of_one_keeps_the_full_resolution_blur(configure):
    configure(AUTOCLIPS_BLUR_SCALE_DIVISOR=1)

    graph = one_pass(build())

    assert "boxblur=24:1[s0bg]" in graph
    assert "scale=270:480" not in graph


def test_sharpening_is_on_by_default_and_can_be_turned_off(configure):
    assert "unsharp=5:5:0.55:3:3:0.25" in one_pass(build())

    configure(AUTOCLIPS_RENDER_SHARPEN=0)
    assert "unsharp" not in one_pass(build())


def test_a_split_screen_is_a_half_frame_spine_and_a_layer_under_it():
    """Two elements, each in its half of the canvas — which is what a split
    screen always was. The bottom half is one layer across the clip rather
    than a second source attached to every segment."""
    composition = build(
        spine=(comp.Segment(SOURCE, 0.0, 10.0, frame=comp.TOP_HALF,
                            backdrop=False),),
        layers=(comp.Layer(BROLL, at_sec=0.0, duration_sec=10.0,
                           source_start_sec=4.0, frame=comp.BOTTOM_HALF, z=-1),),
    )
    graph = one_pass(composition, has_audio=True)

    # The spine fills the top half and is padded into a full canvas, leaving
    # the bottom black for the layer to sit in.
    assert "crop=1080:960,pad=1080:1920:0:0:black[v0]" in graph
    # And the companion fills its own half rather than being stretched to it.
    assert "scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960" in graph
    assert "overlay=0:960" in graph


def test_a_spine_in_a_band_of_its_own_is_padded_to_where_it_sits():
    """Not one of the three layouts — a rectangle somebody dragged. The
    renderer has taken arbitrary segment frames since layouts dissolved; what
    changed with trap 32 is that the compiler stopped collapsing them."""
    graph = one_pass(build(spine=(
        comp.Segment(
            SOURCE, 0.0, 10.0, backdrop=False,
            frame=comp.Frame(y=40.0, width=100.0, height=60.0, fit="cover"),
        ),
    )))

    assert "scale=1080:1152:force_original_aspect_ratio=increase,crop=1080:1152" in graph
    # 40% of 1920 is 768, less half of the 1152-pixel band: 192 from the top.
    assert "pad=1080:1920:0:192:black" in graph


def test_a_frame_covering_the_canvas_crops_instead_of_blurring():
    graph = one_pass(build(spine=(
        comp.Segment(SOURCE, 0.0, 10.0, frame=comp.FULL_FRAME, backdrop=False),
    )))

    assert "boxblur" not in graph
    assert "crop=1080:1920[v0]" in graph


# --- audio ------------------------------------------------------------------


def test_a_source_without_audio_contributes_silence():
    """Skipping it instead would desynchronise everything after it."""
    composition = build(
        spine=(comp.Segment(SOURCE, 0.0, 10.0), comp.Segment(BROLL, 0.0, 4.0))
    )
    graph = one_pass_graph_with_audio_map(composition, {SOURCE: True, BROLL: False})

    assert "[0:a]asetpts=PTS-STARTPTS,aformat=sample_rates=48000" in graph
    assert "anullsrc=r=48000:cl=stereo,atrim=duration=4.000" in graph


def one_pass_graph_with_audio_map(composition, audio_by_source) -> str:
    args = compiler.one_pass_args(
        composition, "/out/clip.mp4", subtitle_path=None,
        audio_by_source=audio_by_source, has_audio=any(audio_by_source.values()),
        encoder="libx264",
    )
    return graph_of(args)


def test_a_silent_composition_is_encoded_without_an_audio_stream():
    args = compiler.one_pass_args(
        build(), "/out/clip.mp4", subtitle_path=None,
        audio_by_source={SOURCE: False}, has_audio=False, encoder="libx264",
    )

    assert "-an" in args
    assert "-map" in args and "[a]" not in args
    assert "loudnorm" not in graph_of(args)


# --- inserts ----------------------------------------------------------------


def test_inserts_are_laid_over_the_joined_video_in_timeline_order():
    composition = build(
        layers=(
            corner(at_sec=14.0, duration_sec=3.0),
            full_frame(at_sec=4.0, duration_sec=2.0),
        )
    )
    graph = one_pass(composition)

    # Ordered by when they appear, not by how they were listed.
    assert graph.index("enable='between(t,4.000,6.000)'") < graph.index(
        "enable='between(t,14.000,17.000)'"
    )
    # Padded at the front so overlay always has a frame when it switches on.
    assert "tpad=start_duration=4.000:start_mode=add:color=black[ins0]" in graph
    # Each insert consumes the previous one's output, and the look comes last.
    assert "[vcat][ins0]overlay=0:0" in graph
    assert "[vins0][ins1]overlay=" in graph
    assert graph.index("[vins1]eq=contrast") > graph.index("[vins0][ins1]")


def test_a_full_frame_insert_covers_the_canvas_and_a_pip_does_not():
    full = one_pass(build(layers=(full_frame(at_sec=1.0, duration_sec=2.0),)))
    pip = one_pass(build(layers=(corner(at_sec=1.0, duration_sec=2.0),)))

    assert "crop=1080:1920,setpts=PTS-STARTPTS" in full
    assert "overlay=0:0" in full
    assert "scale=496:-2" in pip
    assert "overlay=536:180" in pip


# --- animation ---------------------------------------------------------------


def sliding(**kwargs) -> comp.Layer:
    """A quarter-canvas layer crossing the frame from off the left edge."""
    return comp.Layer(
        source_path=BROLL,
        frame=comp.Frame(
            x=50.0, y=20.0, width=40.0,
            motion=comp.Motion(x=((0.0, -20.0), (2.0, 120.0))),
        ),
        **kwargs,
    )


def test_a_still_layer_still_compiles_to_the_position_it_always_did():
    """The floor under all of this (§4.3): a composition with no animation has
    to produce the string it produced before animation existed, or every
    scenario starts paying for a feature it does not use."""
    graph = one_pass(build(layers=(corner(at_sec=1.0, duration_sec=2.0),)))

    assert "overlay=536:180" in graph
    assert "x='" not in graph


def test_a_moving_layer_becomes_an_expression_in_t():
    """The one construction §7.2 measured as actually animating: `overlay`
    with expressions, evaluated on every frame."""
    graph = one_pass(build(layers=(sliding(at_sec=0.0, duration_sec=3.0),)))

    assert "overlay=x='if(lt(t," in graph
    # Per cent of the canvas converted to pixels of the top-left corner, which
    # is the only thing overlay takes: −20% of 1080 is −216, minus half of the
    # 432-pixel box.
    assert "-432.000" in graph
    assert "y='384'" in graph, "y does not move, so it stays a number"


def test_the_expression_is_held_at_both_ends_rather_than_extrapolated():
    """A curve that kept going past its last key would put the layer
    somewhere nobody asked for, seconds after the movement was over."""
    graph = one_pass(build(layers=(sliding(at_sec=0.0, duration_sec=6.0),)))

    # Before the first point and after the last one, the value is a constant.
    assert "if(lt(t,0.000),-432.000," in graph
    assert graph.rstrip().count("1080.000)") >= 1


def test_a_layer_that_grows_is_scaled_on_every_frame():
    """`eval` defaults to `init`, which is the whole difference between a
    layer that grows and a layer that is simply the wrong size."""
    graph = one_pass(build(layers=(
        comp.Layer(
            BROLL, at_sec=0.0, duration_sec=4.0,
            frame=comp.Frame(x=50.0, y=50.0, width=40.0,
                             motion=comp.Motion(width=((0.0, 20.0), (4.0, 80.0)))),
        ),
    )))

    assert "scale=w='if(lt(t," in graph
    assert ":eval=frame" in graph
    # 20% of 1080 to 80% of it, in pixels.
    assert "216.000" in graph and "864.000" in graph
    # And it is placed by what it is *now*, not by a size worked out in
    # advance. Only horizontally: this layer left its height to the aspect
    # ratio, and `box` positions such a layer by its top edge — an asymmetry
    # kept rather than quietly fixed while something else was being added.
    assert "-w/2" in graph
    assert "-h/2" not in graph


def test_a_layer_that_turns_gets_a_box_cut_for_its_widest_angle():
    """`ow`/`oh` are evaluated once, where `t` does not exist yet — measured,
    not assumed: the per-frame form is rejected outright on this build."""
    graph = one_pass(build(layers=(
        comp.Layer(
            BROLL, at_sec=0.0, duration_sec=4.0,
            frame=comp.Frame(x=50.0, y=50.0, width=40.0, height=25.0,
                             motion=comp.Motion(rotate=((0.0, -20.0), (4.0, 20.0)))),
        ),
    )))

    # Degrees in the scenario, radians in the filter: 20° is 0.349 rad.
    assert "rotate=a='if(lt(t," in graph
    assert "ow=rotw(0.349):oh=roth(0.349)" in graph
    # Transparent corners, or the layer arrives as a black diamond.
    assert "format=rgba,rotate=" in graph
    assert ":c=none" in graph
    # This one has a height, so it is centred on both axes.
    assert "-w/2" in graph and "-h/2" in graph


def test_nothing_of_this_appears_when_the_frame_stands_still():
    graph = one_pass(build(layers=(corner(at_sec=1.0, duration_sec=2.0),)))

    for construction in ("eval=frame", "rotate=a=", "-w/2"):
        assert construction not in graph


def test_a_moving_layer_is_not_a_layer_that_fills_the_canvas():
    """`fills_canvas` unlocks a scale-and-crop with no positioning at all, and
    a frame passing through the middle of the canvas must not take it."""
    still = comp.Frame(width=100.0, height=100.0)
    moving = dataclasses.replace(
        still, motion=comp.Motion(x=((0.0, 50.0), (1.0, 60.0))),
    )

    assert still.fills_canvas and not moving.fills_canvas


# --- the door ----------------------------------------------------------------


def test_the_client_hands_a_preview_straight_to_the_renderer(monkeypatch):
    """A signature that nothing calls is a signature nothing checks.

    This one was wrong from the day the package was split out — the client
    took `(output_path, spec, style)` and the renderer takes
    `(composition, output_path, spec)` — and every test that touched a preview
    replaced `client.preview` itself, so the mismatch was invisible until
    something ran it for real. The lesson is in where this test patches: one
    level *below* the function under test, not at it.
    """
    from montage import client
    from montage.render import compiler as render_compiler

    seen = {}

    def fake(composition, output_path, *, spec=None):
        seen.update(composition=composition, output_path=output_path, spec=spec)
        return "result"

    monkeypatch.setattr(render_compiler, "render_preview", fake)
    composition = build()
    window = client.PreviewSpec(at_sec=1.0, duration_sec=2.0, scale=0.5)

    assert client.preview(composition, "/out/preview.mp4", spec=window) == "result"
    assert seen == {
        "composition": composition, "output_path": "/out/preview.mp4", "spec": window,
    }


# --- fragments and the cache ------------------------------------------------


def test_a_fragment_carries_no_style_at_all():
    """What makes a cached fragment reusable: it depends on its source and the
    canvas, and on nothing that a restyle would change."""
    args = compiler.fragment_args(
        build(), comp.Segment(SOURCE, 60.0, 70.0), 0, "/cache/seg.mkv",
        has_audio=True, keep_audio=True, encoder="libx264",
    )
    graph = graph_of(args)

    assert "subtitles=" not in graph
    assert "unsharp" not in graph
    assert "eq=contrast" not in graph
    assert "overlay=0:0" not in graph
    assert "loudnorm" not in graph


def test_fragments_are_written_as_matroska_not_mp4():
    """Measured: with MP4 intermediates the same montage scored VMAF 92.3
    against Matroska's 95.5, because MP4 timestamp edits survive
    `concat -c copy` and shift the frame grid the delivery encode works from."""
    assert compiler.FRAGMENT_SUFFIX == ".mkv"

    path = compiler.fragment_path(comp.Canvas(), comp.Segment(SOURCE, 0.0, 10.0))

    assert path.endswith(".mkv")


def test_fragment_identity_follows_the_source_and_the_framing(tmp_path, configure):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"first version")
    segment = comp.Segment(str(source), 0.0, 10.0)

    original = compiler.fragment_path(comp.Canvas(), segment)
    assert compiler.fragment_path(comp.Canvas(), segment) == original

    # Reframing invalidates it: the fragment is already composed into the canvas.
    configure(AUTOCLIPS_RENDER_FOREGROUND_ZOOM=1.35)
    assert compiler.fragment_path(comp.Canvas(), segment) != original

    # So does replacing the file in place, which a path alone would miss.
    configure(AUTOCLIPS_RENDER_FOREGROUND_ZOOM=1.2)
    source.write_bytes(b"a different video entirely")
    os.utime(source, (1_600_000_000, 1_600_000_000))
    assert compiler.fragment_path(comp.Canvas(), segment) != original


def test_the_final_pass_burns_the_look_over_the_joined_fragments():
    composition = build(layers=(full_frame(at_sec=4.0, duration_sec=2.0),))
    args = compiler.final_pass_args(
        composition, "/tmp/joined.mkv", "/out/clip.mp4",
        subtitle_path="/tmp/clip.ass", has_audio=True, encoder="libx264",
    )
    graph = graph_of(args)

    assert args[args.index("-i") + 1] == "/tmp/joined.mkv"
    assert "[0:v][ins0]overlay=0:0" in graph
    assert "subtitles=filename=" in graph
    assert "loudnorm" in graph
    # The joined file is already cut; the delivery encode must not re-cut it.
    assert "-shortest" not in args


# --- strategy ---------------------------------------------------------------


def test_one_pass_is_the_default():
    """It measured both faster and cleaner: 18.4 s at VMAF 96.4 against
    19.7-24.0 s at 95.5."""
    assert compiler.choose_strategy(build()) == compiler.STRATEGY_ONE_PASS


def test_a_large_graph_falls_back_to_two_stages(configure):
    configure(AUTOCLIPS_RENDER_ONE_PASS_MAX_SEGMENTS=2)

    small = build()
    large = build(spine=tuple(
        comp.Segment(SOURCE, float(i * 20), float(i * 20 + 10)) for i in range(3)
    ))

    assert compiler.choose_strategy(small) == compiler.STRATEGY_ONE_PASS
    assert compiler.choose_strategy(large) == compiler.STRATEGY_TWO_STAGE


def test_the_layer_ceiling_counts_everything_laid_over_the_clip(configure):
    """Trap 10, reviewed rather than discovered. The ceiling counts layers,
    and a layer is no longer only b-roll: a split screen's bottom half is one
    too. At the old value of 4 it collided with the b-roll planner's own
    maximum of 4, so one background was enough to send an ordinary clip down
    the slower path without saying anything."""
    configure(AUTOCLIPS_RENDER_ONE_PASS_MAX_INSERTS=2)

    two = build(layers=tuple(
        full_frame(at_sec=float(i), duration_sec=1.0) for i in range(2)
    ))
    three = build(layers=tuple(
        full_frame(at_sec=float(i), duration_sec=1.0) for i in range(3)
    ))

    assert compiler.choose_strategy(two) == compiler.STRATEGY_ONE_PASS
    assert compiler.choose_strategy(three) == compiler.STRATEGY_TWO_STAGE


def test_a_clip_at_the_brolls_own_maximum_still_renders_in_one_pass():
    """The collision itself, pinned: four inserts is what the planner is
    allowed to produce, and a split screen underneath them makes five layers.
    Both have to stay on the fast path."""
    four_inserts = build(layers=tuple(
        corner(at_sec=float(i * 3), duration_sec=2.0) for i in range(4)
    ))
    with_background = build(layers=(
        comp.Layer(BROLL, at_sec=0.0, duration_sec=10.0, frame=comp.BOTTOM_HALF, z=-1),
    ) + four_inserts.layers)

    assert compiler.choose_strategy(four_inserts) == compiler.STRATEGY_ONE_PASS
    assert compiler.choose_strategy(with_background) == compiler.STRATEGY_ONE_PASS


def test_mixed_layouts_go_through_two_stages():
    mixed = build(spine=(
        comp.Segment(SOURCE, 0.0, 10.0),
        comp.Segment(SOURCE, 20.0, 30.0, frame=comp.FULL_FRAME, backdrop=False),
    ))

    assert compiler.choose_strategy(mixed) == compiler.STRATEGY_TWO_STAGE


def test_a_warm_cache_is_worth_the_extra_encode(tmp_path):
    composition = build()
    for segment in composition.spine:
        path = compiler.fragment_path(composition.canvas, segment)
        with open(path, "wb") as handle:
            handle.write(b"0" * 4096)

    assert compiler.choose_strategy(composition) == compiler.STRATEGY_TWO_STAGE


def test_the_configured_strategy_overrides_every_rule(configure):
    configure(AUTOCLIPS_RENDER_STRATEGY="two_stage")
    assert compiler.choose_strategy(build()) == compiler.STRATEGY_TWO_STAGE

    configure(AUTOCLIPS_RENDER_STRATEGY="one_pass")
    mixed = build(spine=(
        comp.Segment(SOURCE, 0.0, 10.0),
        comp.Segment(SOURCE, 20.0, 30.0, frame=comp.FULL_FRAME, backdrop=False),
    ))
    assert compiler.choose_strategy(mixed) == compiler.STRATEGY_ONE_PASS


@pytest.mark.parametrize("value", ["", "auto"])
def test_an_unset_strategy_means_auto(configure, value):
    configure(AUTOCLIPS_RENDER_STRATEGY=value or "auto")
    assert compiler.choose_strategy(build()) == compiler.STRATEGY_ONE_PASS


# --- choosing a layout ------------------------------------------------------


def probing(monkeypatch, **info):
    monkeypatch.setattr(compiler, "probe_media", lambda path: info)


class TestFramingASource:
    """What `auto` decides, now that the answer is a rectangle.

    The rule did not change — a source already about as tall and narrow as the
    canvas is cropped to fill it, anything wider keeps a blurred backdrop — but
    it comes back as the frame it always meant rather than as a word the
    renderer would branch on.
    """

    def test_a_vertical_source_is_cropped_rather_than_blurred(self, monkeypatch):
        """Giving a 9:16 video a blurred backdrop made of itself is a frame of
        wasted screen."""
        probing(monkeypatch, width=1080, height=1920)

        frame, backdrop = compiler.frame_for_source("/media/phone.mp4", comp.Canvas())

        assert (frame, backdrop) == (comp.FULL_FRAME, False)

    def test_a_taller_than_vertical_source_is_cropped_too(self, monkeypatch):
        probing(monkeypatch, width=1080, height=2400)

        assert compiler.frame_for_source("/media/tall.mp4", comp.Canvas())[1] is False

    @pytest.mark.parametrize("width,height", [(1920, 1080), (1080, 1350), (1080, 1080)])
    def test_anything_wider_keeps_the_backdrop(self, monkeypatch, width, height):
        """Cropping 4:5 to 9:16 would throw away a third of its width, which is
        exactly what the backdrop exists to avoid."""
        probing(monkeypatch, width=width, height=height)

        frame, backdrop = compiler.frame_for_source("/media/wide.mp4", comp.Canvas())

        assert (frame, backdrop) == (comp.CONTAINED, True)

    def test_an_explicit_framing_is_taken_as_given(self, monkeypatch):
        probing(monkeypatch, width=1080, height=1920)

        frame, backdrop = compiler.frame_for_source(
            "/media/phone.mp4", comp.Canvas(), requested=comp.LAYOUT_BLUR
        )

        assert (frame, backdrop) == (comp.CONTAINED, True)

    def test_a_split_screen_frames_the_source_into_the_top_half(self, monkeypatch):
        probing(monkeypatch, width=1920, height=1080)

        frame, backdrop = compiler.frame_for_source(
            "/media/wide.mp4", comp.Canvas(), requested=comp.LAYOUT_SPLIT
        )

        assert (frame, backdrop) == (comp.TOP_HALF, False)

    def test_an_unprobeable_source_falls_back_to_the_backdrop(self, monkeypatch):
        probing(monkeypatch, exists=False)

        assert compiler.frame_for_source("/media/gone.mp4", comp.Canvas())[1] is True


# --- the soundtrack ---------------------------------------------------------


MUSIC = "/media/track.mp3"
WHOOSH = "/media/whoosh.wav"


def full_frame(path=None, **kwargs) -> comp.Layer:
    """A layer covering the canvas — what `broll_full` used to name."""
    return comp.Layer(source_path=path or BROLL, frame=comp.FULL_FRAME, **kwargs)


def corner(path=None, *, policy=None, **kwargs) -> comp.Layer:
    """A layer in the corner — what `broll_pip` used to name.

    The frame comes from the insert policy, which is where the size and the
    margin have always lived; the difference is that it is now a rectangle
    somebody could edit rather than arithmetic inside the renderer.
    """
    rules = policy or style_module.InsertPolicy.from_settings()
    return comp.Layer(
        source_path=path or BROLL,
        frame=comp.frame_for_kind(comp.INSERT_PIP, rules, comp.Canvas()),
        **kwargs,
    )


def bed(path=None, **kwargs) -> comp.AudioTrack:
    """A music bed: a track that loops under the clip and ducks under speech.

    Ducking is explicit now. `MusicBed` defaulted to it, which meant a track
    that should keep its level had to be talked out of it; a track says what it
    does instead.
    """
    defaults = dict(loop=True, duck_threshold=0.03, duck_ratio=8.0,
                    fade_in_sec=0.6, fade_out_sec=1.2)
    return comp.AudioTrack(source_path=path or MUSIC, **{**defaults, **kwargs})


def scored(**extra):
    return build(
        spine=(comp.Segment(SOURCE, 0.0, 20.0), comp.Segment(SOURCE, 30.0, 40.0)),
        **extra,
    )


class TestSoundtrack:
    def test_without_music_the_audio_chain_is_unchanged(self):
        graph = one_pass(scored())

        assert "sidechaincompress" not in graph
        assert "amix" not in graph
        assert "loudnorm=I=-14:TP=-1.5:LRA=11" in graph

    def test_the_bed_is_compressed_against_the_voice(self):
        """Ducking, not a fixed low level: a level quiet enough under speech is
        inaudible in a pause."""
        graph = one_pass(scored(audio=(bed(gain_db=-18.0, duck_ratio=6.0),)))

        assert "asplit=2[voicemix][voicekey]" in graph
        assert "volume=-18.00dB" in graph
        assert "[bed][voicekey]sidechaincompress=" in graph
        assert "ratio=6.00" in graph

    def test_the_bed_loops_to_cover_the_whole_clip(self):
        args = compiler.one_pass_args(
            scored(audio=(bed(),)), "/out/clip.mp4", subtitle_path=None,
            audio_by_source={SOURCE: True}, has_audio=True, encoder="libx264",
        )

        # 30s of output over a track of unknown length: it has to repeat.
        inputs = args[2:args.index("-filter_complex")]
        at = inputs.index(MUSIC) - 1
        assert inputs[at - 6:at + 1] == [
            "-stream_loop", "-1", "-ss", "0.000", "-t", "30.000", "-i"
        ]

    def test_the_mix_does_not_divide_the_voice_by_the_number_of_inputs(self):
        """amix normalises by default, which would drop the speech 6 dB for the
        crime of having music under it."""
        graph = one_pass(scored(audio=(bed(),)))

        assert "amix=inputs=2:normalize=0" in graph

    def test_effects_are_delayed_into_place_rather_than_spliced(self):
        graph = one_pass(scored(audio=(comp.AudioTrack(WHOOSH, at_sec=19.88),)))

        assert "adelay=19880|19880[sfx0]" in graph
        assert "amix=inputs=2:normalize=0" in graph

    def test_a_truncated_effect_is_faded_so_it_cannot_click(self):
        graph = one_pass(scored(audio=(comp.AudioTrack(WHOOSH, at_sec=5.0, duration_sec=1.0),)))

        assert "afade=t=out:st=0.950:d=0.05" in graph

    def test_music_and_effects_share_one_mix(self):
        graph = one_pass(scored(
            audio=(
                bed(),
                comp.AudioTrack(WHOOSH, at_sec=5.0, duration_sec=1.0),
                comp.AudioTrack(WHOOSH, at_sec=15.0, duration_sec=1.0),
            ),
        ))

        assert "[voicemix][ducked][sfx0][sfx1]amix=inputs=4:normalize=0" in graph
        # Normalisation still happens once, on the finished mix.
        assert graph.count("loudnorm") == 1

    def test_the_soundtrack_belongs_to_the_final_pass(self):
        """A cached fragment has to stay valid when the music changes."""
        composition = scored(audio=(
            bed(), comp.AudioTrack(WHOOSH, at_sec=5.0, duration_sec=1.0),
        ))
        fragment = " ".join(compiler.fragment_args(
            composition, composition.spine[0], 0, "/tmp/seg.mkv",
            has_audio=True, keep_audio=True,
        ))
        final = " ".join(compiler.final_pass_args(
            composition, "/tmp/joined.mkv", "/out/clip.mp4",
            subtitle_path=None, has_audio=True, encoder="libx264",
        ))

        assert MUSIC not in fragment and "sidechaincompress" not in fragment
        assert MUSIC in final and "sidechaincompress" in final


# --- the style travels with the clip ----------------------------------------


def styled(**overrides) -> comp.Composition:
    from montage.style import StyleSpec

    return build(style=StyleSpec.from_settings().merged(overrides))


def test_the_graph_follows_the_composition_and_not_the_environment(configure):
    """The regression this refactor exists to prevent.

    The framing used to be read from `get_settings()` inside the filter
    builders, so a clip rendered twice with different configuration came out
    two different videos — and two jobs could not look different at all.
    """
    composition = styled(framing={"zoom": 1.5, "blur_divisor": 2})
    configure(AUTOCLIPS_RENDER_FOREGROUND_ZOOM=1.0, AUTOCLIPS_BLUR_SCALE_DIVISOR=8)

    graph = one_pass(composition)

    assert "scale=1620:1920:force_original_aspect_ratio=decrease" in graph
    assert "scale=540:960:force_original_aspect_ratio=increase" in graph


def test_the_grade_comes_off_the_style():
    graph = one_pass(styled(grade={"contrast": 1.2, "saturation": 0.9, "sharpen": False}))

    assert "eq=contrast=1.20:saturation=0.90" in graph
    assert "unsharp" not in graph


def test_two_compositions_can_want_different_looks_at_once():
    """One process, two jobs, two looks — which the environment could never do."""
    warm = one_pass(styled(grade={"saturation": 1.4}))
    flat = one_pass(styled(grade={"saturation": 1.0}))

    assert "saturation=1.40" in warm
    assert "saturation=1.00" in flat


def test_a_pip_insert_takes_its_geometry_from_the_policy():
    composition = styled(inserts={"pip_width_share": 0.6, "pip_margin_px": 20})
    composition = dataclasses.replace(
        composition,
        layers=(corner(policy=composition.style.inserts, at_sec=1.0, duration_sec=2.0),),
    )

    graph = one_pass(composition)

    assert "scale=648:-2" in graph
    assert "overlay=412:" in graph


def test_the_fragment_key_follows_the_style_it_was_composed_with():
    """A fragment is already composed into the canvas, so reframing has to
    invalidate it — and now the framing is on the clip, not in the process."""
    from montage.style import FramingStyle

    segment = comp.Segment(SOURCE, 0.0, 10.0)
    default = compiler.fragment_path(comp.Canvas(), segment)
    zoomed = compiler.fragment_path(
        comp.Canvas(), segment, framing=FramingStyle(zoom=1.35)
    )

    assert default != zoomed
    # The look, which the final pass applies, must not touch it.
    assert compiler.fragment_path(comp.Canvas(), segment) == default


# --- previews ---------------------------------------------------------------


class TestPreview:
    @pytest.fixture(autouse=True)
    def _fake_ffmpeg(self, monkeypatch, tmp_path):
        self.runs: list[list[str]] = []
        monkeypatch.setattr(compiler, "ffmpeg_exe", lambda: "ffmpeg")
        monkeypatch.setattr(compiler, "ffprobe_has_audio", lambda path: True)
        monkeypatch.setattr(compiler, "probe_render_output", lambda *a, **k: {})
        monkeypatch.setattr(compiler, "_run_ffmpeg", lambda args: self.runs.append(args))
        self.output = str(tmp_path / "preview.mp4")

    def graph(self) -> str:
        args = self.runs[-1]
        return args[args.index("-filter_complex") + 1]

    def test_a_preview_renders_only_the_window_asked_for(self):
        compiler.render_preview(
            build(), self.output, spec=compiler.PreviewSpec(at_sec=12.0, duration_sec=3.0)
        )

        args = self.runs[-1]
        # Seeked straight into the second segment: the first ends at output
        # second 10, so output second 12 is source second 302.
        assert ["-ss", "302.000", "-t", "3.000"] == args[2:6]
        assert len(self.runs) == 1

    def test_a_preview_is_rendered_small_and_cheap(self):
        compiler.render_preview(
            build(), self.output, spec=compiler.PreviewSpec(scale=0.5, crf=30)
        )

        args = self.runs[-1]
        assert "scale=540:960" in self.graph() or "crop=540:960" in self.graph()
        assert args[args.index("-crf") + 1] == "30"

    def test_a_preview_never_goes_through_the_fragment_cache(self, configure):
        """It is a throwaway at a size nothing else uses; caching it would only
        make the real render miss."""
        configure(AUTOCLIPS_RENDER_STRATEGY="two_stage")

        compiler.render_preview(build(), self.output)

        # Two stages would be one run per fragment plus a join plus a final
        # pass. One run means it took the one-pass path regardless.
        assert len(self.runs) == 1
