"""NVIDIA-hosted ASR backend (Riva / NVCF), isolated from the Whisper path.

This is an optional alternative transcription backend that runs on NVIDIA's
cloud GPUs instead of local faster-whisper. It produces the exact same
``TranscriptSegment`` / ``TranscriptWord`` shape so the rest of the Auto Clips
pipeline (slicing, karaoke subtitles) is unaffected.

Selection is via ``AUTOCLIPS_ASR_BACKEND=nvidia``. It talks gRPC to
``grpc.nvcf.nvidia.com:443`` using the ``nvidia-riva-client`` package, an
``nvapi-`` key, and a per-model ``function-id`` copied from the model's API tab
on build.nvidia.com (canary-1b supports ru-RU).

NVCF offline recognition caps audio length per request, so long media must be
chunked (see AUTOCLIPS_WHISPER_CHUNKED / CHUNK_SECONDS) — each chunk becomes one
offline_recognize call.
"""
from __future__ import annotations

import os
import threading
from typing import Any

from app.domain.transcript import TranscriptSegment, TranscriptWord
from app.core.config import get_settings

_SERVICE_LOCK = threading.Lock()
_SERVICE: Any = None


def enabled() -> bool:
    return bool(_api_key() and _function_id())


def transcribe_wav(wav_path: str, *, language: str | None = None) -> list[TranscriptSegment]:
    """Transcribe a 16-bit mono WAV via NVIDIA hosted ASR.

    The caller supplies a WAV whose timeline starts at 0; absolute offsets are
    applied upstream (transcribe_media_segment), exactly as with Whisper.
    """
    service = _get_service()
    config = _build_config(language or _language())
    with open(wav_path, "rb") as handle:
        audio = handle.read()
    response = service.offline_recognize(audio, config)
    return _map_response(response)


def _get_service() -> Any:
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                try:
                    import riva.client
                except ImportError as exc:  # pragma: no cover - optional dependency
                    raise RuntimeError(
                        "nvidia-riva-client is not installed. Run "
                        "`pip install nvidia-riva-client` to use AUTOCLIPS_ASR_BACKEND=nvidia."
                    ) from exc
                if not _function_id():
                    raise RuntimeError(
                        "AUTOCLIPS_NVIDIA_ASR_FUNCTION_ID is not set. Copy the function-id "
                        "from the model's API tab on build.nvidia.com (e.g. canary-1b-asr)."
                    )
                if not _api_key():
                    raise RuntimeError(
                        "No NVIDIA API key. Set AUTOCLIPS_NVIDIA_ASR_API_KEY (or reuse OPENAI_API_KEY)."
                    )
                auth = riva.client.Auth(
                    uri=_server(),
                    use_ssl=True,
                    metadata_args=[
                        ["function-id", _function_id()],
                        ["authorization", f"Bearer {_api_key()}"],
                    ],
                )
                _SERVICE = riva.client.ASRService(auth)
    return _SERVICE


def _build_config(language: str) -> Any:
    import riva.client

    # We always feed 16 kHz mono WAV; NVCF returns empty results if the sample
    # rate isn't declared explicitly.
    return riva.client.RecognitionConfig(
        language_code=language,
        max_alternatives=1,
        enable_automatic_punctuation=_env_bool("AUTOCLIPS_NVIDIA_ASR_PUNCTUATION", default=True),
        enable_word_time_offsets=True,
        audio_channel_count=1,
        sample_rate_hertz=_sample_rate(),
    )


def _sample_rate() -> int:
    return max(8000, get_settings().asr.nvidia.sample_rate)


def _map_response(response: Any) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    for result in getattr(response, "results", None) or []:
        alternatives = getattr(result, "alternatives", None) or []
        if not alternatives:
            continue
        alternative = alternatives[0]
        text = str(getattr(alternative, "transcript", "") or "").strip()
        words = _parse_words(getattr(alternative, "words", None) or [])
        if not text and words:
            text = " ".join(word.text for word in words)
        if not text:
            continue
        if words:
            seg_start, seg_end = words[0].start_sec, words[-1].end_sec
        else:
            seg_start = _ms_to_sec(getattr(result, "start_time", None)) or 0.0
            seg_end = _ms_to_sec(getattr(result, "end_time", None)) or seg_start
        if seg_end <= seg_start:
            continue
        segments.append(
            TranscriptSegment(
                start_sec=round(seg_start, 3),
                end_sec=round(seg_end, 3),
                text=text,
                words=words or None,
            )
        )
    segments.sort(key=lambda seg: seg.start_sec)
    return segments


def _parse_words(raw_words: Any) -> list[TranscriptWord]:
    # RNNT/CTC decoders often emit a single-frame timestamp (start == end) for
    # short words. Keep the onset (which is what karaoke needs) but give each word
    # a positive duration bounded by the next word's onset, so nothing downstream
    # drops it for having end <= start.
    parsed: list[tuple[float, float | None, str]] = []
    for word in raw_words:
        start = _ms_to_sec(getattr(word, "start_time", None))
        end = _ms_to_sec(getattr(word, "end_time", None))
        token = str(getattr(word, "word", "") or "").strip()
        if token and start is not None:
            parsed.append((start, end, token))

    words: list[TranscriptWord] = []
    for index, (start, end, token) in enumerate(parsed):
        if end is None or end <= start:
            next_start = parsed[index + 1][0] if index + 1 < len(parsed) else None
            if next_start is not None and next_start - 0.01 > start:
                end = next_start - 0.01
            else:
                end = start + 0.1
        words.append(TranscriptWord(start_sec=round(start, 3), end_sec=round(end, 3), text=token))
    return words


def _ms_to_sec(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def _api_key() -> str:
    return get_settings().asr.nvidia.resolved_api_key().strip()


def _function_id() -> str:
    return get_settings().asr.nvidia.function_id.strip()


def _server() -> str:
    return get_settings().asr.nvidia.server.strip()


def _language() -> str:
    return get_settings().asr.nvidia.language.strip()
