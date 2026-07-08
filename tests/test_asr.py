"""Speech-to-text: chunk stitching, backend routing, cache invalidation."""
from __future__ import annotations

import os

import pytest

from app.adapters.asr import cache as transcript_cache
from app.adapters.asr import nvidia as nvidia_asr
from app.adapters.asr import whisper as transcription
from app.adapters.asr.whisper import _transcript_network_env
from app.domain.transcript import TranscriptSegment, TranscriptWord



def test_transcribe_media_chunked_stitches_without_seam_duplicates(monkeypatch, configure):

    configure(
        AUTOCLIPS_WHISPER_CHUNKED=1,
        AUTOCLIPS_WHISPER_CHUNK_SECONDS=10,
        AUTOCLIPS_WHISPER_CHUNK_OVERLAP_SECONDS=1,
        AUTOCLIPS_WHISPER_NUM_WORKERS=2,
    )
    monkeypatch.setattr(transcription, "_get_model", lambda *a, **k: object())

    # Fake per-chunk transcription: returns evenly spaced 1s "words", including
    # some inside the pre/post-roll that fall outside the chunk's owned window.
    def fake_segment(media_path, *, start_sec, end_sec, model_name=None, language=None):
        segs = []
        t = float(int(start_sec))
        while t < end_sec:
            segs.append(
                transcription.TranscriptSegment(
                    start_sec=round(t, 3),
                    end_sec=round(t + 0.9, 3),
                    text=f"w{int(round(t))}",
                    words=None,
                )
            )
            t += 1.0
        return segs

    monkeypatch.setattr(transcription, "transcribe_media_segment", fake_segment)

    result = transcription.transcribe_media_chunked("video.mp4", duration=30.0)
    starts = [round(seg.start_sec, 3) for seg in result]

    # Contiguous coverage 0..29, strictly increasing, no duplicates at the
    # 10s / 20s chunk seams.
    assert starts == sorted(starts)
    assert len(starts) == len(set(starts))
    assert starts == [float(i) for i in range(30)]


def test_nvidia_asr_maps_word_timestamps_to_seconds():
    from types import SimpleNamespace

    response = SimpleNamespace(
        results=[
            SimpleNamespace(
                alternatives=[
                    SimpleNamespace(
                        transcript="привет мир",
                        words=[
                            SimpleNamespace(word="привет", start_time=200, end_time=600),
                            SimpleNamespace(word="мир", start_time=650, end_time=1000),
                        ],
                    )
                ]
            )
        ]
    )
    segments = nvidia_asr._map_response(response)
    assert len(segments) == 1
    seg = segments[0]
    assert seg.text == "привет мир"
    assert seg.start_sec == 0.2 and seg.end_sec == 1.0  # ms -> seconds
    assert [w.text for w in seg.words] == ["привет", "мир"]
    assert seg.words[0].start_sec == 0.2 and seg.words[1].end_sec == 1.0


def test_transcribe_media_routes_to_nvidia_backend(monkeypatch, configure):

    configure(AUTOCLIPS_ASR_BACKEND="nvidia")
    sentinel = [transcription.TranscriptSegment(0.0, 1.0, "cloud", None)]

    def fake_transcribe_wav(wav_path, *, language=None):
        assert wav_path.lower().endswith(".wav")
        return sentinel

    monkeypatch.setattr(transcription, "_extract_audio_wav", lambda p: "clip.wav")
    monkeypatch.setattr(nvidia_asr, "transcribe_wav", fake_transcribe_wav)

    assert transcription.transcribe_media("clip.mp4") is sentinel


def test_transcribe_media_chunked_falls_back_to_single_pass_when_short(monkeypatch, configure):

    configure(AUTOCLIPS_WHISPER_CHUNKED=1, AUTOCLIPS_WHISPER_CHUNK_SECONDS=300)
    calls = {"segment": 0, "full": 0}

    def fake_full(media_path, *, model_name=None, language=None):
        calls["full"] += 1
        return [transcription.TranscriptSegment(0, 5, "short")]

    def fake_segment(*a, **k):
        calls["segment"] += 1
        return []

    monkeypatch.setattr(transcription, "transcribe_media", fake_full)
    monkeypatch.setattr(transcription, "transcribe_media_segment", fake_segment)

    result = transcription.transcribe_media_chunked("video.mp4", duration=120.0)
    assert calls == {"segment": 0, "full": 1}
    assert result[0].text == "short"


def test_transcript_cache_invalidates_when_source_changes(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOCLIPS_DIR", str(tmp_path / "AutoClips"))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"version one")
    segments = [{"start_sec": 1.0, "end_sec": 2.0, "text": "first"}]

    transcript_cache.save_render_transcript(
        str(source),
        start_sec=1,
        end_sec=3,
        segments=segments,
    )
    assert transcript_cache.load_render_transcript(str(source), start_sec=1, end_sec=3)[0] == segments

    source.write_bytes(b"version two")

    assert transcript_cache.load_render_transcript(str(source), start_sec=1, end_sec=3) is None


def test_transcript_network_env_disables_inherited_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "socks4://127.0.0.1:10808")
    monkeypatch.setenv("HTTPS_PROXY", "socks4://127.0.0.1:10808")
    monkeypatch.delenv("AUTOCLIPS_WHISPER_PROXY", raising=False)

    with _transcript_network_env():
        assert "HTTP_PROXY" not in os.environ
        assert "HTTPS_PROXY" not in os.environ
        assert os.environ["NO_PROXY"] == "*"

    assert os.environ["HTTP_PROXY"] == "socks4://127.0.0.1:10808"


def test_transcribing_a_silent_video_says_so(tmp_path, monkeypatch):
    """Not `IndexError: tuple index out of range` from inside the decoder."""
    from app.adapters.asr import service
    from app.core.errors import PermanentError

    path = tmp_path / "job-7.f399.mp4"
    path.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(service.ffmpeg, "ffprobe_has_audio", lambda p: False)

    def unreachable(*a, **k):
        raise AssertionError("must not reach the decoder")

    monkeypatch.setattr(service.whisper, "transcribe_media_chunked", unreachable)

    with pytest.raises(PermanentError, match="no audio track"):
        service.transcribe(str(path))


def test_transcribing_a_normal_video_is_not_blocked(tmp_path, monkeypatch):
    from app.adapters.asr import service

    path = tmp_path / "job-4.mp4"
    path.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(service.ffmpeg, "ffprobe_has_audio", lambda p: True)
    monkeypatch.setattr(service.whisper, "transcribe_media_chunked", lambda *a, **k: ["ok"])

    assert service.transcribe(str(path)) == ["ok"]
