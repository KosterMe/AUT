"""Word timings recovered from YouTube captions.

This is a performance fix expressed as a correctness test. When captions carry
no word timings, the render falls back to running Whisper on every clip — so
"are the words there?" decides whether a job takes minutes or an hour.
"""
from __future__ import annotations

import json

from app.adapters.youtube import transcripts as captions
from app.domain import transcript as transcript_model


def json3(events: list[dict]) -> str:
    return json.dumps({"events": events})


def auto_caption_event(start_ms: int, duration_ms: int, words: list[tuple[int, str]]) -> dict:
    """A cue as YouTube's ASR emits it: one seg per word, with an offset."""
    segs = []
    for index, (offset, text) in enumerate(words):
        seg: dict = {"utf8": text if index == 0 else f" {text}"}
        if offset is not None:
            seg["tOffsetMs"] = offset
        segs.append(seg)
    return {"tStartMs": start_ms, "dDurationMs": duration_ms, "segs": segs}


class TestAutomaticCaptions:
    def test_word_offsets_become_words(self):
        payload = json3(
            [auto_caption_event(10_000, 2_000, [(None, "привет"), (500, "как"), (1200, "дела")])]
        )
        segments = captions.parse_json3(payload)

        assert len(segments) == 1
        words = segments[0].words
        assert [w.text for w in words] == ["привет", "как", "дела"]
        # The first piece carries no offset: it starts with the cue.
        assert words[0].start_sec == 10.0
        assert words[1].start_sec == 10.5
        assert words[2].start_sec == 11.2

    def test_a_word_lasts_until_the_next_one_starts(self):
        payload = json3(
            [auto_caption_event(0, 3_000, [(None, "one"), (800, "two"), (1600, "three")])]
        )
        words = captions.parse_json3(payload)[0].words

        assert words[0].end_sec == 0.8
        assert words[1].end_sec == 1.6
        # The last word runs to the end of the cue.
        assert words[2].end_sec == 3.0

    def test_words_never_overlap_or_run_backwards(self):
        payload = json3(
            [auto_caption_event(5_000, 4_000, [(None, "a"), (100, "b"), (100, "c"), (2000, "d")])]
        )
        words = captions.parse_json3(payload)[0].words

        assert all(w.end_sec >= w.start_sec for w in words)
        assert all(
            current.end_sec <= following.start_sec + 1e-6
            for current, following in zip(words, words[1:])
        )

    def test_whitespace_pieces_are_not_words(self):
        payload = json3(
            [
                {
                    "tStartMs": 0,
                    "dDurationMs": 2000,
                    "segs": [
                        {"utf8": "hello"},
                        {"utf8": "\n"},
                        {"utf8": " world", "tOffsetMs": 900},
                    ],
                }
            ]
        )
        segments = captions.parse_json3(payload)

        assert [w.text for w in segments[0].words] == ["hello", "world"]
        # The separator still belongs in the readable text.
        assert "hello" in segments[0].text and "world" in segments[0].text


class TestManualCaptions:
    def test_captions_without_offsets_yield_no_words(self):
        """A hand-written caption file has nothing word-level to recover, and
        must say so rather than inventing timings."""
        payload = json3(
            [{"tStartMs": 0, "dDurationMs": 3000, "segs": [{"utf8": "A whole written line."}]}]
        )
        segments = captions.parse_json3(payload)

        assert segments[0].text == "A whole written line."
        assert segments[0].words is None
        assert segments[0].has_words is False


class TestRoundTrip:
    def test_words_survive_the_transcript_cache(self):
        """Words are useless if they are lost on the way to the render task."""
        payload = json3([auto_caption_event(0, 2_000, [(None, "раз"), (700, "два")])])
        original = captions.parse_json3(payload)

        restored = transcript_model.deserialize(transcript_model.serialize(original))

        assert [w.text for w in restored[0].words] == ["раз", "два"]
        assert restored[0].words[1].start_sec == 0.7

    def test_words_survive_deduplication(self):
        """Rolling captions repeat a cue; merging them must not drop timings."""
        event = auto_caption_event(1_000, 1_000, [(None, "тот"), (400, "же")])
        segments = captions.parse_json3(json3([event, dict(event, tStartMs=1_500)]))

        assert len(segments) == 1
        assert segments[0].words is not None
        assert [w.text for w in segments[0].words] == ["тот", "же"]


class TestRenderPathConsequence:
    def test_captions_with_words_skip_re_transcription(self, monkeypatch):
        """The whole point: a clip whose window already has words is rendered
        from them instead of triggering a Whisper pass."""
        from app.adapters.asr import selection

        payload = json3(
            [auto_caption_event(0, 6_000, [(None, "a"), (1000, "b"), (2000, "c")])]
        )
        serialized = transcript_model.serialize(captions.parse_json3(payload))

        def explode(*args, **kwargs):
            raise AssertionError("re-transcribed a clip that already had word timings")

        monkeypatch.setattr(selection, "_cache_or_transcribe", explode)

        _, meta = selection.select_subtitle_transcript(
            "/tmp/source.mp4",
            fallback_segments=serialized,
            start_sec=0.0,
            end_sec=6.0,
            enabled=True,
        )
        assert meta["source"] == "job_transcript_words"

    def test_captions_without_words_still_fall_back(self, monkeypatch):
        from app.adapters.asr import selection

        payload = json3(
            [{"tStartMs": 0, "dDurationMs": 6000, "segs": [{"utf8": "no word timings here"}]}]
        )
        serialized = transcript_model.serialize(captions.parse_json3(payload))
        called: list[bool] = []

        monkeypatch.setattr(
            selection,
            "_cache_or_transcribe",
            lambda *a, **k: (called.append(True), ([], {"source": "transcribed"}))[1],
        )

        selection.select_subtitle_transcript(
            "/tmp/source.mp4",
            fallback_segments=serialized,
            start_sec=0.0,
            end_sec=6.0,
            enabled=True,
        )
        assert called == [True]
