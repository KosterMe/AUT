"""Subtitle timing and ASS generation — pure domain logic.

Karaoke subtitles are the most visible thing about a clip, and the rules
(one word per cue, anchored to word onsets, never overlapping) are easy to
regress, so they are pinned here.
"""
from __future__ import annotations

from montage import subtitles



def test_subtitle_cues_follow_montage_timeline():
    timeline = subtitles.make_timeline_segments(10, [(0, 3), (8, 12)])
    cues = subtitles.make_subtitle_cues(
        [
            {"start_sec": 11, "end_sec": 12, "text": "first phrase"},
            {"start_sec": 18, "end_sec": 20, "text": "second phrase after a very long silence"},
        ],
        timeline_segments=timeline,
    )

    assert len(cues) == 2
    assert cues[0].start_sec == 1
    assert cues[0].end_sec == 2
    assert cues[1].start_sec == 3
    assert cues[-1].end_sec == 5
    assert all(current.end_sec <= next_cue.start_sec for current, next_cue in zip(cues, cues[1:]))


def test_subtitle_cues_use_word_timestamps():
    timeline = subtitles.make_timeline_segments(10, [(0, 3), (8, 12)])
    cues = subtitles.make_subtitle_cues(
        [
            {
                "start_sec": 10,
                "end_sec": 22,
                "text": "one two three four five six",
                "words": [
                    {"start_sec": 10.2, "end_sec": 10.5, "text": "one"},
                    {"start_sec": 10.6, "end_sec": 10.9, "text": "two"},
                    {"start_sec": 18.2, "end_sec": 18.5, "text": "three"},
                    {"start_sec": 18.6, "end_sec": 18.9, "text": "four"},
                    {"start_sec": 19.0, "end_sec": 19.3, "text": "five"},
                    {"start_sec": 19.4, "end_sec": 19.7, "text": "six"},
                ],
            }
        ],
        timeline_segments=timeline,
    )

    # Karaoke mode: one word per cue, each cue anchored to the word onset.
    assert [cue.text for cue in cues] == ["one", "two", "three", "four", "five", "six"]
    assert cues[0].start_sec == 0.2
    assert cues[2].start_sec == 3.2
    assert all(cue.end_sec - cue.start_sec >= 0.12 for cue in cues)
    assert all(
        current.end_sec <= next_cue.start_sec for current, next_cue in zip(cues, cues[1:])
    )


def test_subtitle_cues_do_not_overlap_for_rolling_captions():
    timeline = subtitles.make_timeline_segments(0, [(0, 30)])
    cues = subtitles.make_subtitle_cues(
        [
            {"start_sec": 20.76, "end_sec": 25.4, "text": "second opening first opening"},
            {"start_sec": 23.16, "end_sec": 27.72, "text": "started with a broken wall"},
            {"start_sec": 25.4, "end_sec": 29.92, "text": "and a sealed breach"},
        ],
        timeline_segments=timeline,
    )

    assert cues
    assert all(current.end_sec <= next_cue.start_sec for current, next_cue in zip(cues, cues[1:]))
    assert "first opening" in " ".join(cue.text.replace("\\N", " ") for cue in cues)
    assert all(cue.end_sec - cue.start_sec >= 0.45 for cue in cues)


def test_subtitle_cues_clean_stage_directions_and_speaker_marks():
    timeline = subtitles.make_timeline_segments(0, [(0, 10)])
    cues = subtitles.make_subtitle_cues(
        [
            {"start_sec": 0, "end_sec": 1, "text": ">> [музыка]"},
            {"start_sec": 1, "end_sec": 4, "text": ">> This line should look good"},
        ],
        timeline_segments=timeline,
    )

    assert len(cues) == 1
    assert cues[0].text == "This line should look\\Ngood"


def test_subtitle_time_offset_shifts_all_cues():
    timeline = subtitles.make_timeline_segments(0, [(0, 10)])
    words = [
        {"start_sec": 1.0, "end_sec": 1.4, "text": "раньше"},
        {"start_sec": 2.0, "end_sec": 2.4, "text": "показать"},
    ]
    segs = [{"start_sec": 1.0, "end_sec": 3.0, "text": "раньше показать", "words": words}]
    base = subtitles.make_subtitle_cues(segs, timeline_segments=timeline)
    opts = subtitles.CueOptions(time_offset_seconds=-0.2)
    shifted = subtitles.make_subtitle_cues(segs, timeline_segments=timeline, options=opts)

    assert base[0].start_sec == 1.0
    assert shifted[0].start_sec == 0.8  # -0.2s: shown earlier
    assert all(cue.start_sec >= 0.0 for cue in shifted)


def test_word_cues_anchor_to_word_onsets():
    timeline = subtitles.make_timeline_segments(0, [(0, 10)])
    words = [
        {"start_sec": 1.0, "end_sec": 1.4, "text": "каждое"},
        {"start_sec": 1.5, "end_sec": 1.9, "text": "слово"},
        {"start_sec": 2.0, "end_sec": 2.4, "text": "появляется"},
        {"start_sec": 2.5, "end_sec": 2.9, "text": "вовремя"},
    ]
    cues = subtitles.make_subtitle_cues(
        [{"start_sec": 1.0, "end_sec": 3.0, "text": "каждое слово появляется вовремя", "words": words}],
        timeline_segments=timeline,
    )

    # Every cue starts exactly at the onset of its word.
    onsets = {round(word["start_sec"], 3) for word in words}
    assert all(cue.start_sec in onsets for cue in cues)
    assert cues[0].start_sec == 1.0
    assert all(len(cue.text.replace("\\N", " ").split()) == 1 for cue in cues)
    assert all(
        current.end_sec <= next_cue.start_sec for current, next_cue in zip(cues, cues[1:])
    )


def test_word_cues_strip_punctuation():
    timeline = subtitles.make_timeline_segments(0, [(0, 10)])
    cues = subtitles.make_subtitle_cues(
        [
            {
                "start_sec": 0,
                "end_sec": 5,
                "text": "One sentence. Next one.",
                "words": [
                    {"start_sec": 0.0, "end_sec": 0.3, "text": "One"},
                    {"start_sec": 0.3, "end_sec": 0.8, "text": "sentence."},
                    {"start_sec": 0.9, "end_sec": 1.2, "text": "Next"},
                    {"start_sec": 1.2, "end_sec": 1.6, "text": "one."},
                ],
            }
        ],
        timeline_segments=timeline,
    )

    rendered = [cue.text.replace("\\N", " ") for cue in cues]
    # Trailing punctuation is stripped for a clean karaoke look.
    assert rendered == ["One", "sentence", "Next", "one"]
    assert not any(text.endswith((".", ",", "!", "?")) for text in rendered)


def test_short_words_are_never_glued_to_their_neighbour():
    """A one-word cue stays a one-word cue, however short or fast it is.

    Pairing used to kick in for short words and quick onsets; it made the
    karaoke drift out of sync with the voice, so it is gone.
    """
    timeline = subtitles.make_timeline_segments(0, [(0, 10)])
    words = [
        {"start_sec": 1.0, "end_sec": 1.08, "text": "и"},
        {"start_sec": 1.09, "end_sec": 1.20, "text": "вот"},
        {"start_sec": 1.22, "end_sec": 1.60, "text": "так"},
    ]
    cues = subtitles.make_subtitle_cues(
        [{"start_sec": 1.0, "end_sec": 1.6, "text": "и вот так", "words": words}],
        timeline_segments=timeline,
    )

    assert [cue.text for cue in cues] == ["и", "вот", "так"]
    assert [cue.start_sec for cue in cues] == [1.0, 1.09, 1.22]


def test_subtitle_cues_cap_card_duration():
    timeline = subtitles.make_timeline_segments(0, [(0, 10)])
    cues = subtitles.make_subtitle_cues(
        [{"start_sec": 0, "end_sec": 6, "text": "Clean short subtitle"}],
        timeline_segments=timeline,
    )

    assert cues[0].end_sec - cues[0].start_sec <= 2.8


def test_ass_writer_preserves_line_break_command(tmp_path):
    ass_path = tmp_path / "clip.ass"

    subtitles.write_ass_file(
        [subtitles.SubtitleCue(start_sec=0, end_sec=1, text="first\\Nsecond")],
        str(ass_path),
        width=1080,
        height=1920,
    )

    content = ass_path.read_text(encoding="utf-8")
    assert "first\\Nsecond" in content
    assert "first\\\\Nsecond" not in content


def test_ass_writer_pops_each_cue_in(tmp_path):
    ass_path = tmp_path / "clip.ass"

    subtitles.write_ass_file(
        [subtitles.SubtitleCue(start_sec=0, end_sec=1, text="word")],
        str(ass_path),
        width=1080,
        height=1920,
    )

    line = [
        row
        for row in ass_path.read_text(encoding="utf-8").splitlines()
        if row.startswith("Dialogue:")
    ][0]
    # Lands at 88%, overshoots to 104%, settles at 100% — with a quick fade in
    # and a hard cut out, so back-to-back words do not flicker.
    assert r"\fscx88\fscy88" in line
    assert r"\t(0,90,\fscx104\fscy104)" in line
    assert r"\t(90,170,\fscx100\fscy100)" in line
    assert r"\fad(70,0)" in line


def test_cue_animation_fits_inside_a_very_short_cue():
    """A karaoke cue can be a couple of frames long; the pop has to be over
    before the word disappears, or it only ever renders mid-grow."""
    tags = subtitles._cue_tags(0.12, animate=True)

    settle_end = int(tags.split(r"\t(")[-1].split(",")[1])
    assert settle_end <= 120


def test_cue_animation_can_be_turned_off():
    tags = subtitles._cue_tags(1.0, animate=False)

    assert tags == r"{\blur0.2}"


def test_ass_writer_adds_top_headline_banner(tmp_path):
    ass_path = tmp_path / "clip.ass"

    subtitles.write_ass_file(
        [subtitles.SubtitleCue(start_sec=0, end_sec=1, text="word")],
        str(ass_path),
        width=1080,
        height=1920,
        title_text="Атака Титанов - часть 3",
        title_end_sec=42.0,
    )

    content = ass_path.read_text(encoding="utf-8")
    assert "Style: Top," in content
    # Headline spans the whole clip (starts at 0) using the Top style.
    assert "Dialogue: 0,0:00:00.00," in content
    assert ",Top,," in content
    assert "Атака Титанов - часть 3" in content


def test_ass_writer_without_title_has_no_banner(tmp_path):
    ass_path = tmp_path / "clip.ass"

    subtitles.write_ass_file(
        [subtitles.SubtitleCue(start_sec=0, end_sec=1, text="word")],
        str(ass_path),
        width=1080,
        height=1920,
    )

    content = ass_path.read_text(encoding="utf-8")
    assert "Style: Top," not in content


def test_cover_ass_writer_has_title_and_part_badge(tmp_path):
    ass_path = tmp_path / "clip.cover.ass"

    subtitles.write_cover_ass_file(
        str(ass_path),
        width=1080,
        height=1920,
        title_text="Мой Влог",
        part_text="часть 12",
    )

    content = ass_path.read_text(encoding="utf-8")
    assert "Style: CoverTitle," in content
    assert "Style: CoverPart," in content
    assert "Мой Влог" in content
    # Part badge is upper-cased for emphasis.
    assert "ЧАСТЬ 12" in content


def test_the_casing_policy_applies_to_a_clip_with_no_word_timings():
    """A style asking for uppercase used to get it only on clips whose words
    were timed — so the same preset produced two different looks depending on
    where the transcript came from."""
    timeline = [subtitles.TimelineSegment(0.0, 10.0, 0.0, 10.0)]
    opts = subtitles.CueOptions(uppercase=True)

    cues = subtitles.make_subtitle_cues(
        [], timeline_segments=timeline, fallback_text="привет мир", options=opts
    )

    assert cues and cues[0].text == "ПРИВЕТ МИР"


def test_the_style_decides_the_casing_on_both_paths():
    from montage.style import SubtitleStyle

    timeline = [subtitles.TimelineSegment(0.0, 10.0, 0.0, 10.0)]
    style = SubtitleStyle(uppercase=True)

    timed = subtitles.make_subtitle_cues(
        [{"start_sec": 0.0, "end_sec": 1.0, "text": "привет",
          "words": [{"text": "привет", "start_sec": 0.0, "end_sec": 0.6}]}],
        timeline_segments=timeline, style=style,
    )
    untimed = subtitles.make_subtitle_cues(
        [], timeline_segments=timeline, fallback_text="привет", style=style
    )

    assert timed[0].text == untimed[0].text == "ПРИВЕТ"
