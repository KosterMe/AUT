"""Reading YouTube's own caption tracks instead of transcribing."""
from __future__ import annotations

import json

from app.adapters.youtube import transcripts as captions



def test_youtube_captions_parse_json3():
    payload = json.dumps(
        {
            "events": [
                {
                    "tStartMs": 1000,
                    "dDurationMs": 1500,
                    "segs": [{"utf8": "hello"}, {"utf8": " world"}],
                },
                {"tStartMs": 2600, "dDurationMs": 900, "segs": [{"utf8": "\n"}]},
                {
                    "tStartMs": 4000,
                    "dDurationMs": 1000,
                    "segs": [{"utf8": "second line"}],
                },
            ]
        }
    )

    segments = captions.parse_json3(payload)

    assert [(item.start_sec, item.end_sec, item.text) for item in segments] == [
        (1.0, 2.5, "hello world"),
        (4.0, 5.0, "second line"),
    ]


def test_youtube_captions_select_manual_before_auto(monkeypatch):
    monkeypatch.setenv("AUTOCLIPS_YOUTUBE_CAPTION_LANGS", "ru,en,*")
    metadata = {
        "automatic_captions": {
            "ru": [{"ext": "json3", "url": "auto-ru"}],
        },
        "subtitles": {
            "en": [{"ext": "vtt", "url": "manual-en"}],
            "ru": [{"ext": "json3", "url": "manual-ru"}],
        },
    }

    selected = captions.select_caption_track(metadata)

    assert selected["kind"] == "manual"
    assert selected["language"] == "ru"
    assert selected["url"] == "manual-ru"
