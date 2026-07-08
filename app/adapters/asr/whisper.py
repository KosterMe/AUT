"""Local speech-to-text via faster-whisper.

Reads its tuning from `settings.asr.whisper` rather than the environment
directly, so the knobs are discoverable and a test can set them without
touching `os.environ`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import lru_cache
from collections.abc import Iterator
from typing import Any

from app.core.config import get_settings
from app.domain.transcript import TranscriptSegment, TranscriptWord


def model_settings() -> dict[str, str]:
    """Which model to load. Part of the transcript cache key."""
    whisper = get_settings().asr.whisper
    compute_type = whisper.compute_type
    if whisper.device != "cpu" and compute_type == "int8":
        # int8 is the sensible CPU default but wastes a GPU.
        compute_type = "float16"
    return {
        "model": whisper.model,
        "device": whisper.device,
        "compute_type": compute_type,
    }


def runtime_settings() -> dict[str, int]:
    """Performance-only knobs for model construction.

    Kept separate from `transcription_settings` on purpose: these change
    decoding speed, not the transcript text, so they must never leak into the
    transcript cache key. `cpu_threads=0` lets CTranslate2 use all cores.
    """
    whisper = get_settings().asr.whisper
    return {
        "cpu_threads": max(0, whisper.cpu_threads),
        "num_workers": max(1, whisper.num_workers),
    }


def chunked_enabled() -> bool:
    return get_settings().asr.whisper.chunked


def chunk_seconds() -> float:
    return max(0.0, get_settings().asr.whisper.chunk_seconds)


def chunk_overlap_seconds() -> float:
    return max(0.0, get_settings().asr.whisper.chunk_overlap_seconds)


def asr_backend() -> str:
    """Which engine transcribes: 'whisper' (local) or 'nvidia' (hosted)."""
    return str(get_settings().asr.backend)


def vad_parameters() -> dict[str, Any]:
    """Overrides for faster-whisper's speech detector.

    The VAD trims audio it judges as silence *before* decoding, which is the
    usual cause of dropped quiet or trailing words. Padding each speech span
    and requiring a longer silence before closing one are the two knobs that
    fix it; lower the threshold to keep faint speech.
    """
    whisper = get_settings().asr.whisper
    return {
        "threshold": whisper.vad_threshold,
        "min_silence_duration_ms": whisper.vad_min_silence_ms,
        "speech_pad_ms": whisper.vad_speech_pad_ms,
    }


def transcription_settings() -> dict[str, Any]:
    """Everything that can change the transcript text.

    The transcript cache is keyed on this dict, so anything added here
    correctly invalidates cached transcripts — and anything that does *not*
    affect the text must stay out (see `runtime_settings`).
    """
    whisper = get_settings().asr.whisper
    return {
        **model_settings(),
        "language": whisper.language or None,
        "vad_filter": whisper.vad_filter,
        "vad_parameters": vad_parameters() if whisper.vad_filter else None,
        "word_timestamps": whisper.word_timestamps,
        "beam_size": whisper.beam_size,
        "best_of": whisper.best_of,
        # A ladder rather than a single 0.0: Whisper re-decodes a segment at a
        # higher temperature when it trips the compression-ratio or log-prob
        # guard, instead of silently dropping it.
        "temperature": whisper.temperature,
        "no_speech_threshold": whisper.no_speech_threshold,
        "log_prob_threshold": whisper.log_prob_threshold,
        "compression_ratio_threshold": whisper.compression_ratio_threshold,
        "condition_on_previous_text": whisper.condition_on_previous_text,
        "initial_prompt": whisper.initial_prompt or None,
    }


def transcribe_media(
    media_path: str,
    *,
    model_name: str | None = None,
    language: str | None = None,
) -> list[TranscriptSegment]:
    if asr_backend() == "nvidia":
        return _transcribe_media_nvidia(media_path, language=language)

    settings = transcription_settings()
    runtime = runtime_settings()
    model = _get_model(
        model_name or settings["model"],
        settings["device"],
        settings["compute_type"],
        runtime["cpu_threads"],
        runtime["num_workers"],
    )
    transcribe_kwargs: dict[str, Any] = dict(
        language=language or settings["language"],
        vad_filter=bool(settings["vad_filter"]),
        word_timestamps=bool(settings["word_timestamps"]),
        beam_size=int(settings["beam_size"]),
        best_of=int(settings["best_of"]),
        temperature=settings["temperature"],
        condition_on_previous_text=bool(settings["condition_on_previous_text"]),
        initial_prompt=settings["initial_prompt"],
    )
    # Only forward optional knobs when set, so faster-whisper keeps its own
    # defaults otherwise (passing None would override the default, not preserve it).
    if settings.get("vad_parameters"):
        transcribe_kwargs["vad_parameters"] = settings["vad_parameters"]
    for key in ("no_speech_threshold", "log_prob_threshold", "compression_ratio_threshold"):
        if settings.get(key) is not None:
            transcribe_kwargs[key] = settings[key]
    segments, _info = model.transcribe(media_path, **transcribe_kwargs)

    result: list[TranscriptSegment] = []
    for segment in segments:
        text = str(getattr(segment, "text", "") or "").strip()
        if not text:
            continue
        start = _as_float(getattr(segment, "start", None))
        end = _as_float(getattr(segment, "end", None))
        if start is None or end is None or end <= start:
            continue
        words = _coerce_words(getattr(segment, "words", None))
        result.append(TranscriptSegment(start_sec=start, end_sec=end, text=text, words=words))
    return result


def _transcribe_media_nvidia(media_path: str, *, language: str | None = None) -> list[TranscriptSegment]:
    from app.adapters.asr import nvidia as nvidia_asr

    # transcribe_media_segment already hands us a 16 kHz mono WAV; only re-extract
    # when we were given a container/video (e.g. the short-media, non-chunked path).
    if media_path.lower().endswith(".wav"):
        return nvidia_asr.transcribe_wav(media_path, language=language)
    wav_path = _extract_audio_wav(media_path)
    try:
        return nvidia_asr.transcribe_wav(wav_path, language=language)
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


def _extract_audio_wav(media_path: str) -> str:
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed or not on PATH")
    fd, output_path = tempfile.mkstemp(prefix="autoclip-nvasr-", suffix=".wav")
    os.close(fd)
    subprocess.run(
        [ffmpeg, "-y", "-i", media_path, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", output_path],
        check=True,
        capture_output=True,
    )
    return output_path


def transcribe_media_chunked(
    media_path: str,
    *,
    duration: float,
    model_name: str | None = None,
    language: str | None = None,
) -> list[TranscriptSegment]:
    """Transcribe a long file as parallel chunks, then stitch by start time.

    Each chunk is transcribed with a little pre/post-roll for audio context but
    only owns segments whose START falls inside its window, so the overlapping
    context never yields duplicate words at chunk seams. Falls back to a single
    serial pass for short media where the split/stitch overhead isn't worth it.
    """
    # NVIDIA NVCF caps audio length per request, so cloud ASR must always chunk
    # long media; Whisper only chunks when explicitly enabled.
    is_nvidia = asr_backend() == "nvidia"
    chunk_len = chunk_seconds()
    if chunk_len <= 0 or duration <= chunk_len * 1.5 or (not chunked_enabled() and not is_nvidia):
        return transcribe_media(media_path, model_name=model_name, language=language)

    roll = chunk_overlap_seconds()
    windows: list[tuple[float, float]] = []
    cursor = 0.0
    while cursor < duration:
        window_end = min(duration, cursor + chunk_len)
        windows.append((cursor, window_end))
        cursor = window_end

    runtime = runtime_settings()
    # Warm the shared Whisper model once (single-threaded download guard) before
    # threads fan out. Not applicable to the cloud backend.
    if not is_nvidia:
        settings = model_settings()
        _get_model(
            model_name or settings["model"],
            settings["device"],
            settings["compute_type"],
            runtime["cpu_threads"],
            runtime["num_workers"],
        )

    results: list[list[TranscriptSegment]] = [[] for _ in windows]

    def _work(index: int) -> None:
        start, end = windows[index]
        is_last = index == len(windows) - 1
        ext_start = max(0.0, start - roll)
        ext_end = min(duration, end + roll)
        segments = transcribe_media_segment(
            media_path,
            start_sec=ext_start,
            end_sec=ext_end,
            model_name=model_name,
            language=language,
        )
        results[index] = [
            seg
            for seg in segments
            if (start <= seg.start_sec < end) or (is_last and seg.start_sec >= start)
        ]

    # Cap at num_workers: the shared CTranslate2 model only runs that many
    # inferences at once, so extra threads would just queue.
    max_workers = max(1, min(len(windows), runtime["num_workers"]))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for _ in pool.map(_work, range(len(windows))):
            pass

    merged = [seg for chunk in results for seg in chunk]
    merged.sort(key=lambda seg: seg.start_sec)
    return merged


def transcribe_media_segment(
    media_path: str,
    *,
    start_sec: float,
    end_sec: float,
    model_name: str | None = None,
    language: str | None = None,
) -> list[TranscriptSegment]:
    if end_sec <= start_sec:
        return []
    segment_audio = _extract_audio_segment(media_path, start_sec=start_sec, end_sec=end_sec)
    try:
        relative_segments = transcribe_media(
            segment_audio,
            model_name=model_name,
            language=language,
        )
    finally:
        try:
            os.unlink(segment_audio)
        except OSError:
            pass

    absolute_segments: list[TranscriptSegment] = []
    for segment in relative_segments:
        words = [
            TranscriptWord(
                start_sec=round(word.start_sec + start_sec, 3),
                end_sec=round(word.end_sec + start_sec, 3),
                text=word.text,
            )
            for word in (segment.words or [])
        ]
        absolute_segments.append(
            TranscriptSegment(
                start_sec=round(segment.start_sec + start_sec, 3),
                end_sec=round(segment.end_sec + start_sec, 3),
                text=segment.text,
                words=words,
            )
        )
    return absolute_segments


def _coerce_words(raw_words: Any) -> list[TranscriptWord]:
    result: list[TranscriptWord] = []
    for word in raw_words or []:
        text = str(getattr(word, "word", "") or "").strip()
        start = _as_float(getattr(word, "start", None))
        end = _as_float(getattr(word, "end", None))
        if start is None or end is None or end <= start or not text:
            continue
        result.append(TranscriptWord(start_sec=start, end_sec=end, text=text))
    return result


def _extract_audio_segment(media_path: str, *, start_sec: float, end_sec: float) -> str:
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed or not on PATH")
    fd, output_path = tempfile.mkstemp(prefix="autoclip-transcribe-", suffix=".wav")
    os.close(fd)
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-ss",
            f"{start_sec:.3f}",
            "-t",
            f"{max(0.1, end_sec - start_sec):.3f}",
            "-i",
            media_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            output_path,
        ],
        check=True,
        capture_output=True,
    )
    return output_path


def _ffmpeg_exe() -> str | None:
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


_MODEL_LOAD_LOCK = threading.Lock()
_LOADED_MODEL_KEYS: set[tuple[str, str, str, int, int]] = set()


def _get_model(
    model_name: str,
    device: str,
    compute_type: str,
    cpu_threads: int,
    num_workers: int,
) -> Any:
    """Return the shared model, loading it at most once under the proxy env.

    The HuggingFace download is the only step that needs the network-env tweak,
    and it must run single-threaded — otherwise concurrent chunk workers race on
    os.environ. Once loaded, subsequent calls hit the lru_cache with no env
    manipulation, so parallel inference is safe.
    """
    key = (model_name, device, compute_type, cpu_threads, num_workers)
    if key not in _LOADED_MODEL_KEYS:
        with _MODEL_LOAD_LOCK:
            if key not in _LOADED_MODEL_KEYS:
                with _transcript_network_env():
                    _load_model(*key)
                _LOADED_MODEL_KEYS.add(key)
    return _load_model(*key)


@lru_cache(maxsize=4)
def _load_model(
    model_name: str,
    device: str,
    compute_type: str,
    cpu_threads: int = 0,
    num_workers: int = 1,
) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Install requirements or disable transcript-based slicing."
        ) from exc

    try:
        return WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
            num_workers=num_workers,
        )
    except Exception as exc:
        raise RuntimeError(_explain_model_load_failure(model_name, exc)) from exc


def _explain_model_load_failure(model_name: str, exc: BaseException) -> str:
    """Turn a Hub error into where the weights go and how to put them there.

    faster-whisper reports a missing model as an `IncompleteSnapshotError` with
    a repo id and a commit hash, which says nothing about what to do — least of
    all on a link too slow to fetch the weights at all, where the answer is to
    carry the file in from somewhere else.
    """
    text = str(exc)
    if "IncompleteSnapshot" not in type(exc).__name__ and "is incomplete" not in text:
        return f"{type(exc).__name__}: {exc}"

    repo = model_name if "/" in model_name else f"Systran/faster-whisper-{model_name}"
    home = os.environ.get("HF_HOME") or "~/.cache/huggingface"
    snapshot = f"{home}/hub/models--{repo.replace('/', '--')}/snapshots/<commit>/"
    return (
        f"the {model_name} weights are not on this machine, and the Hub could not "
        f"be reached to fetch them.\n"
        f"On a link too slow to download them, carry them in instead: fetch "
        f"model.bin, config.json, tokenizer.json, vocabulary.json and "
        f"preprocessor_config.json from https://huggingface.co/{repo}/tree/main on "
        f"any working connection, and place them in {snapshot} — which lives on the "
        f"`models` volume, so it survives container recreation.\n"
        f"Underlying error: {exc}"
    )


_PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "FTP_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "ftp_proxy",
    "no_proxy",
)


@contextmanager
def _transcript_network_env() -> Iterator[None]:
    proxy = get_settings().asr.whisper.proxy.strip().strip("\"'")
    if proxy.lower().startswith("socks4://"):
        raise RuntimeError(
            "AUTOCLIPS_WHISPER_PROXY uses socks4://, which the Whisper model "
            "downloader does not support. Use socks5://, http://, https://, or "
            "unset it."
        )

    saved = {key: os.environ.get(key) for key in _PROXY_ENV_VARS}
    try:
        for key in _PROXY_ENV_VARS:
            os.environ.pop(key, None)
        if proxy:
            os.environ["HTTP_PROXY"] = proxy
            os.environ["HTTPS_PROXY"] = proxy
            os.environ["ALL_PROXY"] = proxy
        else:
            # Avoid inherited desktop/system proxy settings such as
            # socks4://127.0.0.1:10808, which httpx/huggingface-hub rejects.
            os.environ["NO_PROXY"] = "*"
            os.environ["no_proxy"] = "*"
        yield
    finally:
        for key in _PROXY_ENV_VARS:
            value = saved[key]
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
