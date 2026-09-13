"""The ffmpeg capabilities probe.

The stderr samples below are real output from ffmpeg 6.1.1, not invented. The
probe exists because the documentation and the installed build disagree, so a
test that invents what ffmpeg says would be making exactly the mistake the probe
was written to catch.

Nothing here runs ffmpeg: what is worth testing is the judging, and the judging
is a pure function of bytes that came back.
"""
from __future__ import annotations

import pytest

from app.adapters.media import capabilities as caps

# Real complaints, addresses and all.
GBLUR_STDERR = (
    b"[Parsed_gblur_0 @ 0x561e4f559540] [Eval @ 0x7ffd73e93cf0] "
    b"Undefined constant or missing '(' in 't'\n"
)
CROP_STDERR = b"Error applying option 'eval' to filter 'crop': Option not found\n"
ATEMPO_STDERR = (
    b"[Parsed_atempo_0 @ 0x560356f3d500] Value 0.400000 for parameter "
    b"'tempo' out of range [0.5 - 100]\n"
)
XFADE_STDERR = (
    b"[Parsed_xfade_0 @ 0x55cc16cf5200] First input link main parameters "
    b"(size 64x64) do not match the corresponding second input link xfade "
    b"parameters (size 32x32)\n"
)


def frames(*pixels: int) -> list[bytes]:
    """One 1x1 RGB frame per argument, so runs and repeats are easy to write."""
    return [bytes([value, value, value]) for value in pixels]


class TestJudging:
    """Accepted is not animated, and only the frames can tell them apart."""

    def test_one_picture_repeated_is_the_silent_failure(self):
        assert caps.judge(frames(7, 7, 7, 7)) == caps.FROZEN

    def test_a_different_picture_every_frame_is_animation(self):
        assert caps.judge(frames(1, 2, 3, 4)) == caps.SMOOTH

    def test_changing_in_runs_is_a_step_function(self):
        assert caps.judge(frames(1, 1, 1, 9, 9, 9)) == caps.STEPPED

    def test_a_gate_reaches_two_states_and_that_is_all_it_should(self):
        assert caps.judge(frames(1, 1, 9, 9, 1, 1)) == caps.STEPPED

    def test_a_single_frame_cannot_be_shown_to_animate(self):
        assert caps.judge(frames(3)) == caps.FROZEN


class TestFrames:
    def test_a_stream_is_cut_into_whole_frames(self):
        blob = bytes(4 * 3 * 2)  # two 2x2 RGB frames
        assert len(caps.split_frames(blob, 2, 2)) == 2

    def test_a_truncated_tail_is_dropped_rather_than_compared(self):
        assert len(caps.split_frames(bytes(4 * 3 * 2 + 5), 2, 2)) == 2

    def test_nothing_decoded_is_no_frames(self):
        assert caps.split_frames(b"", 2, 2) == []


class TestReason:
    """ffmpeg's own words, minus the parts that change between runs."""

    def test_instance_addresses_are_dropped(self):
        assert "0x" not in caps.reason(GBLUR_STDERR)

    def test_the_complaint_itself_survives_verbatim(self):
        assert caps.reason(GBLUR_STDERR) == "Undefined constant or missing '(' in 't'"

    def test_filter_tags_are_dropped_because_the_row_already_names_the_filter(self):
        assert caps.reason(ATEMPO_STDERR) == (
            "Value 0.400000 for parameter 'tempo' out of range [0.5 - 100]"
        )

    def test_a_message_with_no_tags_is_left_alone(self):
        assert caps.reason(CROP_STDERR) == CROP_STDERR.decode().strip()

    def test_brackets_inside_the_message_are_not_mistaken_for_tags(self):
        assert caps.reason(XFADE_STDERR).startswith("First input link")

    def test_the_first_line_is_the_one_reported(self):
        assert caps.reason(CROP_STDERR + GBLUR_STDERR) == CROP_STDERR.decode().strip()

    def test_silence_still_produces_a_reason(self):
        assert caps.reason(b"") == "no output"


class TestCommand:
    """Each kind of check asks ffmpeg for a different thing."""

    def build(self, **kwargs) -> list[str]:
        check = caps.Check(key="k", label="l", construction="c",
                           graph="[0:v]null", **kwargs)
        return caps.command("ffmpeg", check, check.graph)

    def test_every_source_is_passed_as_lavfi_in_order(self):
        argv = self.build(sources=("first", "second"))
        assert argv.count("-f") >= 2
        assert argv[argv.index("first") - 1] == "-i"
        assert argv.index("first") < argv.index("second")

    def test_a_picture_check_asks_for_raw_frames(self):
        assert self.build()[-5:] == ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]

    def test_a_picture_check_stops_at_the_frame_count(self):
        argv = self.build(frames=9)
        assert argv[argv.index("-frames:v") + 1] == "9"

    def test_a_sound_check_asks_for_pcm_at_the_probe_rate(self):
        argv = self.build(kind=caps.SOUND)
        assert argv[argv.index("-ar") + 1] == str(caps.SAMPLE_RATE)
        assert "rawvideo" not in argv

    def test_a_run_check_decodes_to_nothing(self):
        assert self.build(kind=caps.RUN)[-3:] == ["-f", "null", "-"]

    def test_a_timing_check_decodes_to_nothing_too(self):
        # It is compared against a passthrough, and piping 74MB of raw frames
        # would cost more than the filter under test.
        assert self.build(kind=caps.TIMING)[-3:] == ["-f", "null", "-"]


def picture_check(**kwargs) -> caps.Check:
    return caps.Check(key="k", label="свойство", construction="filter=…",
                      graph="[0:v]null", width=1, height=1, frames=4, **kwargs)


def examine(monkeypatch, check: caps.Check, replies: dict) -> caps.Finding:
    """Run one check against canned ffmpeg output, keyed by check name.

    The baseline passthrough is a check of its own, so a timing test can make
    it slower or faster than the construction it is compared against.
    """
    def fake(ffmpeg, target, graph, timeout):
        return replies[target.key]

    monkeypatch.setattr(caps, "_measure", fake)
    return caps._examine("ffmpeg", check, check.graph, 1.0, {})


class TestExamine:
    def test_a_refusal_is_recorded_with_what_ffmpeg_said(self, monkeypatch):
        finding = examine(monkeypatch, picture_check(),
                          {"k": (1, b"", GBLUR_STDERR, 0.1)})

        assert finding.verdict == caps.REJECTED
        assert finding.detail == "Undefined constant or missing '(' in 't'"
        assert not finding.offerable

    def test_frames_that_never_change_are_reported_as_frozen(self, monkeypatch):
        # The case a exit code cannot see: the render succeeded and the
        # animation is simply absent.
        finding = examine(monkeypatch, picture_check(),
                          {"k": (0, bytes(4 * 3), b"", 0.1)})

        assert finding.verdict == caps.FROZEN
        assert (finding.distinct, finding.frames) == (1, 4)
        assert not finding.offerable

    def test_frames_that_all_differ_are_offerable_as_a_keyframe_track(self, monkeypatch):
        blob = b"".join(bytes([n, n, n]) for n in range(4))
        finding = examine(monkeypatch, picture_check(), {"k": (0, blob, b"", 0.1)})

        assert finding.verdict == caps.SMOOTH
        assert finding.offerable

    def test_a_clean_exit_with_no_frames_is_still_a_refusal(self, monkeypatch):
        assert examine(monkeypatch, picture_check(),
                       {"k": (0, b"", b"", 0.1)}).verdict == caps.REJECTED

    def test_a_sound_check_reports_the_length_that_came_out(self, monkeypatch):
        one_second = bytes(caps.SAMPLE_RATE * caps.BYTES_PER_SAMPLE)
        finding = examine(monkeypatch, picture_check(kind=caps.SOUND),
                          {"k": (0, one_second, b"", 0.1)})

        assert finding.verdict == caps.OK
        assert finding.seconds == pytest.approx(1.0)

    def test_a_sound_check_that_produced_silence_is_a_refusal(self, monkeypatch):
        finding = examine(monkeypatch, picture_check(kind=caps.SOUND),
                          {"k": (0, b"", ATEMPO_STDERR, 0.1)})

        assert finding.verdict == caps.REJECTED
        assert "out of range" in finding.detail

    def test_cost_is_a_multiple_of_the_same_run_without_the_filter(self, monkeypatch):
        finding = examine(monkeypatch, picture_check(kind=caps.TIMING), {
            "k": (0, b"", b"", 0.8),
            "baseline": (0, b"", b"", 0.2),
        })

        assert finding.verdict == caps.OK
        assert finding.cost == pytest.approx(4.0)

    def test_a_timing_check_that_will_not_run_reports_no_cost(self, monkeypatch):
        finding = examine(monkeypatch, picture_check(kind=caps.TIMING),
                          {"k": (1, b"", CROP_STDERR, 0.1)})

        assert finding.verdict == caps.REJECTED
        assert finding.cost == 0.0


class TestDeclaredChecks:
    """The check list is data, and data can be wrong in ways ffmpeg would only
    find at run time."""

    def test_every_key_is_unique_because_the_api_is_keyed_by_it(self):
        keys = [c.key for c in caps.CHECKS]
        assert len(keys) == len(set(keys))

    def test_every_graph_input_has_a_source_behind_it(self):
        import re

        for check in caps.CHECKS:
            inputs = [int(n) for n in re.findall(r"\[(\d+):[va]\]", check.graph)]
            highest = max(inputs, default=0)
            assert highest < len(check.sources), check.key

    def test_the_only_substitution_is_the_subtitle_file(self):
        import re

        for check in caps.CHECKS:
            assert set(re.findall(r"\{(\w+)\}", check.graph)) <= {"ass"}, check.key

    def test_the_probe_writes_a_real_subtitle_file_for_the_libass_check(self, monkeypatch):
        seen: list[str] = []

        def record(ffmpeg, check, graph, timeout, cache):
            seen.append(graph)
            return caps.Finding(key=check.key, label=check.label,
                                construction=check.construction, verdict=caps.OK)

        monkeypatch.setattr(caps, "ffmpeg_exe", lambda: "ffmpeg")
        monkeypatch.setattr(caps, "_build", lambda *_: "test")
        monkeypatch.setattr(caps, "_examine", record)

        subtitles = next(c for c in caps.CHECKS if "{ass}" in c.graph)
        caps.probe((subtitles,))

        assert "{ass}" not in seen[0]
        assert ".ass" in seen[0]


def finding(**kwargs) -> caps.Finding:
    base = dict(key="k", label="свойство", construction="filter=…", verdict=caps.SMOOTH)
    return caps.Finding(**{**base, **kwargs})


class TestWhatTheEditorIsTold:
    """`GET /api/capabilities` exists so the editor stops offering what this
    build cannot deliver (Р-40), which makes `offerable` the load-bearing bit."""

    @pytest.mark.parametrize("verdict", [caps.SMOOTH, caps.STEPPED, caps.OK])
    def test_something_that_worked_can_be_offered(self, verdict):
        assert finding(verdict=verdict).offerable

    @pytest.mark.parametrize("verdict", [caps.FROZEN, caps.REJECTED])
    def test_something_that_did_not_work_is_never_offered(self, verdict):
        assert not finding(verdict=verdict).offerable

    def test_a_frozen_construction_is_withheld_like_a_rejected_one(self):
        # It is the more dangerous of the two: the render would succeed.
        assert not finding(verdict=caps.FROZEN).offerable

    def test_the_payload_is_keyed_by_check_so_the_editor_can_look_one_up(self):
        report = caps.Report(build="6.1.1", findings=(finding(key="position"),))
        payload = report.as_dict()

        assert payload["capabilities"]["position"]["offerable"] is True
        assert payload["capabilities"]["position"]["verdict"] == caps.SMOOTH
        assert payload["build"] == "6.1.1"

    def test_the_payload_survives_a_build_that_is_not_there(self):
        report = caps.Report(ok=False, detail="no ffmpeg on this machine")

        assert report.as_dict() == {
            "ok": False, "build": "", "ffmpeg": "",
            "detail": "no ffmpeg on this machine", "capabilities": {},
        }

    def test_a_missing_build_is_reported_rather_than_raised(self, monkeypatch):
        monkeypatch.setattr(caps, "ffmpeg_exe", lambda: None)
        report = caps.probe()

        assert not report.ok
        assert report.findings == ()

    def test_a_finding_can_be_looked_up_by_key(self):
        report = caps.Report(findings=(finding(key="a"), finding(key="b")))

        assert report.by_key("b").key == "b"
        assert report.by_key("absent") is None


class TestRendering:
    def test_the_measurement_is_shown_beside_the_verdict(self):
        assert _cell(frames=24, distinct=24) == "анимируется, 24/24 кадров"

    def test_a_frozen_row_shows_how_few_frames_differed(self):
        assert _cell(verdict=caps.FROZEN, frames=24, distinct=1) == (
            "принимается и не анимирует, 1/24 кадров"
        )

    def test_a_refusal_carries_ffmpeg_s_own_words(self):
        assert _cell(verdict=caps.REJECTED, detail="Option not found") == (
            "отвергается — Option not found"
        )

    def test_a_sound_row_shows_the_length_that_came_out(self):
        assert _cell(verdict=caps.OK, seconds=0.66) == "работает, 0.66 с"

    def test_a_cost_row_shows_the_multiple(self):
        assert _cell(verdict=caps.OK, cost=26.4) == "×26.4 к пустому проходу"

    def test_a_caveat_is_kept_next_to_the_fact_it_qualifies(self):
        assert _cell(note="опции eval у фильтра нет").endswith(
            "; опции eval у фильтра нет"
        )

    def test_markdown_carries_a_table_for_each_group(self):
        rules = [line for line in caps.render_markdown(_report()).splitlines()
                 if line.startswith("| ---")]

        assert rules == ["| --- | --- | --- |", "| --- | --- |", "| --- | --- | --- |"]

    def test_markdown_names_the_build_it_measured(self):
        assert "Замерено на `6.1.1`" in caps.render_markdown(_report())

    def test_markdown_puts_each_finding_in_its_own_table(self):
        rendered = caps.render_markdown(_report()).splitlines()
        chain = rendered.index("| Конструкция цепочки | Факт |")

        assert rendered.index("| позиция x, y | `overlay` | анимируется, 2/2 кадров |") < chain
        assert rendered.index("| xfade на совпадающих входах | работает |") > chain

    def test_text_marks_what_can_be_offered_and_what_cannot(self):
        rendered = caps.render_text(_report())

        assert "+ позиция x, y" in rendered
        assert "- масштаб через накопитель zoom" in rendered
        assert "3 of 4 constructions usable here" in rendered

    def test_text_says_so_when_there_is_no_build_to_ask(self):
        report = caps.Report(ok=False, detail="no ffmpeg on this machine")

        assert caps.render_text(report) == (
            "ffmpeg capabilities: no ffmpeg on this machine"
        )


def _cell(**kwargs) -> str:
    return caps._fact(finding(**kwargs))


def _report() -> caps.Report:
    """One finding from each group, so the renderers can be checked whole."""
    return caps.Report(build="6.1.1", ffmpeg="/usr/bin/ffmpeg", findings=(
        finding(key="position", label="позиция x, y", construction="overlay",
                frames=2, distinct=2),
        finding(key="scale_accumulator", label="масштаб через накопитель zoom",
                verdict=caps.FROZEN, frames=2, distinct=1),
        finding(key="xfade", label="xfade на совпадающих входах", verdict=caps.OK),
        finding(key="cost_rotation", label="поворот", verdict=caps.OK, cost=4.4),
    ))
