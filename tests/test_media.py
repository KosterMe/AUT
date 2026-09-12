"""Parsers around the media subprocess calls, and yt-dlp output handling.

Cheap to test, and the source of failures that are otherwise only visible in a
mangled render. Filter-graph construction moved to tests/test_compiler.py along
with the code that builds it.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError

from app.adapters.media.ffmpeg import _parse_silencedetect
from app.adapters.youtube import downloader
from app.adapters.youtube.downloader import (
    _cached_metadata,
    _download_candidates,
    _recover_downloaded_file,
)
from app.adapters.youtube.options import apply_ytdlp_auth_options, explain_ytdlp_auth_error



def test_parse_silencedetect_handles_open_ended_silence():
    stderr = """
    [silencedetect @ test] silence_start: 2.1
    [silencedetect @ test] silence_end: 3.4 | silence_duration: 1.3
    [silencedetect @ test] silence_start: 8.0
    """

    assert _parse_silencedetect(stderr, duration=10.0) == [(2.1, 3.4), (8.0, 10.0)]


def test_download_candidates_prefer_merged_file(tmp_path):
    merged = tmp_path / "job-11.mp4"
    intermediate = tmp_path / "job-11.f401.mp4"
    merged.write_bytes(b"merged")
    intermediate.write_bytes(b"video-only")

    paths = _download_candidates({}, str(tmp_path), "job-11")

    assert paths[0] == str(merged)


def test_the_source_thumbnail_is_not_mistaken_for_the_download(tmp_path):
    """`job-9.thumb` sits in the same directory and matches the same glob.

    A thumbnail outlives the source retention deletes, so re-running that job
    found the picture, treated it as the already-downloaded video and handed a
    77 KB JPEG to the transcriber, which complained about a missing audio
    track three steps away from the actual problem.
    """
    (tmp_path / "job-9.thumb").write_bytes(b"jpeg")

    assert _download_candidates({}, str(tmp_path), "job-9") == []
    assert _recover_downloaded_file(str(tmp_path), "job-9") is None


def test_recover_downloaded_file_ignores_part(tmp_path):
    # A .part file is an incomplete download; promoting it to the final name
    # would poison the cache with a truncated video on every retry.
    part = tmp_path / "job-11.mp4.part"
    part.write_bytes(b"truncated")

    recovered = _recover_downloaded_file(str(tmp_path), "job-11")

    assert recovered is None
    assert part.exists()
    assert not (tmp_path / "job-11.mp4").exists()


def test_recover_downloaded_file_returns_completed_file(tmp_path):
    completed = tmp_path / "job-11.mp4"
    completed.write_bytes(b"complete")
    (tmp_path / "job-11.webm.part").write_bytes(b"truncated")

    recovered = _recover_downloaded_file(str(tmp_path), "job-11")

    assert recovered == str(completed)


def test_half_a_download_is_recognised_wherever_it_is_found():
    """A file yt-dlp wrote before merging looks complete and has no audio.

    Job 10 reused one recorded by an earlier attempt, took its transcript from
    YouTube's captions instead of the file, and rendered twenty silent clips
    without raising anything.
    """
    assert not downloader.is_usable_source("/media/originals/job-7.f399.mp4")
    assert not downloader.is_usable_source("/media/originals/job-7.f251-1.webm")
    # The other way a recorded path turns out not to be the video.
    assert not downloader.is_usable_source("/media/originals/job-9.thumb")
    assert downloader.is_usable_source("/media/originals/job-7.mp4")
    assert downloader.is_usable_source("/media/originals/url-a1b2c3d4e5f6.mp4")


def test_a_publication_download_is_keyed_by_its_url(tmp_path, monkeypatch):
    """Two YouTube publications must not share one cached file.

    Publishing a YouTube URL directly has no job to key the download on, and
    the constant that stood in for one put every such download at `job-0.mp4`.
    The cache check at the top of the download returns an existing file
    without asking where it came from, so the second publication would have
    uploaded the first video under the second one's caption.
    """
    seen: list[str] = []

    def fake_download(source_ref, *, stem, on_progress=None):
        seen.append(stem)
        return f"/media/originals/{stem}.mp4", {}

    monkeypatch.setattr(downloader, "_download", fake_download)

    downloader.download_for_publication("https://www.youtube.com/watch?v=aaaaaaaaaaa")
    downloader.download_for_publication("https://www.youtube.com/watch?v=bbbbbbbbbbb")
    downloader.download_for_publication("https://www.youtube.com/watch?v=aaaaaaaaaaa")

    assert seen[0] != seen[1], "different videos must not land on the same file"
    assert seen[0] == seen[2], "the same video must still reuse its download"


def test_cached_metadata_reports_no_title(tmp_path):
    """A cached file knows its path, not the video's name. Returning the
    filename as a title put "job-11 - часть 1" into TikTok captions."""
    path = tmp_path / "job-11.mp4"
    path.write_bytes(b"not a real video")

    metadata = _cached_metadata("https://youtu.be/example", str(path))

    assert metadata["title"] is None
    assert metadata["webpage_url"] == "https://youtu.be/example"
    assert metadata["extractor"] == "cached"


def test_pot_base_url_goes_to_the_provider_namespace(monkeypatch):
    # The bgutil plugin reads extractor_args["youtubepot-bgutilhttp"], not
    # ["youtube"]. Putting it in the wrong one fails silently: the plugin keeps
    # asking 127.0.0.1:4416, which in a container is the container itself.
    monkeypatch.setenv("YTDLP_POT_BASE_URL", "http://bgutil:4416")

    options = apply_ytdlp_auth_options({})

    assert options["extractor_args"]["youtubepot-bgutilhttp"] == {
        "base_url": ["http://bgutil:4416"]
    }


def test_no_pot_base_url_leaves_the_plugin_default_alone():
    options = apply_ytdlp_auth_options({})

    assert "youtubepot-bgutilhttp" not in options["extractor_args"]


def test_cookie_file_is_never_handed_to_ytdlp_directly(tmp_path, monkeypatch):
    # yt-dlp writes its jar back in YoutubeDL.close(), so a read-only export —
    # which is how compose mounts it — made every download raise EROFS from the
    # `with` block's exit, masking whatever really happened inside it.
    export = tmp_path / "youtube-cookies.txt"
    export.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setenv("YTDLP_COOKIES_FILE", str(export))

    jar = apply_ytdlp_auth_options({})["cookiefile"]

    assert jar != str(export)
    assert Path(jar).read_text(encoding="utf-8") == export.read_text(encoding="utf-8")

    # And it must be writable, which is the entire point of the copy.
    Path(jar).write_text("# rewritten by yt-dlp\n", encoding="utf-8")


def test_cookie_jar_follows_a_re_exported_file(tmp_path, monkeypatch):
    export = tmp_path / "youtube-cookies.txt"
    export.write_text("first\n", encoding="utf-8")
    monkeypatch.setenv("YTDLP_COOKIES_FILE", str(export))

    first = Path(apply_ytdlp_auth_options({})["cookiefile"])
    assert first.read_text(encoding="utf-8") == "first\n"

    # yt-dlp refreshing its own jar must survive the next download...
    first.write_text("refreshed\n", encoding="utf-8")
    assert Path(apply_ytdlp_auth_options({})["cookiefile"]).read_text(encoding="utf-8") == (
        "refreshed\n"
    )

    # ...but a newly exported file must win over it.
    os.utime(export, (time.time() + 10, time.time() + 10))
    export.write_text("second\n", encoding="utf-8")
    assert Path(apply_ytdlp_auth_options({})["cookiefile"]).read_text(encoding="utf-8") == (
        "second\n"
    )


def test_missing_cookie_file_still_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv("YTDLP_COOKIES_FILE", str(tmp_path / "nope.txt"))

    with pytest.raises(FileNotFoundError, match="missing file"):
        apply_ytdlp_auth_options({})


def test_error_explanation_looks_past_the_unwinding_failure():
    """The real cause is under the exception raised while yt-dlp closed.

    This is what turned a blocked network into "Read-only file system": the
    OSError from saving the cookie jar replaced the DNS failure, and the job
    reported a filesystem problem that did not exist.
    """
    try:
        try:
            raise DownloadError(
                "ERROR: [youtube] abc: Unable to download API page: "
                "Failed to resolve 'www.youtube.com' "
                "([Errno -5] No address associated with hostname)"
            )
        except DownloadError:
            raise OSError(30, "Read-only file system: '/data/youtube-cookies.txt'")
    except OSError as exc:
        explanation = explain_ytdlp_auth_error(exc)

    assert "DNS lookup" in explanation
    assert "YTDLP_PROXY" in explanation
    assert "Read-only" not in explanation


def test_error_explanation_falls_back_to_the_ytdlp_message():
    try:
        try:
            raise DownloadError("ERROR: [youtube] abc: Video unavailable")
        except DownloadError:
            raise OSError(30, "Read-only file system: '/data/youtube-cookies.txt'")
    except OSError as exc:
        assert explain_ytdlp_auth_error(exc) == "ERROR: [youtube] abc: Video unavailable"


def test_error_explanation_keeps_a_plain_error_intact():
    assert explain_ytdlp_auth_error(RuntimeError("something else")) == "something else"


def test_an_unmerged_download_is_not_recovered_as_the_source(tmp_path):
    """yt-dlp writes video and audio separately and merges them last.

    Interrupted before the merge, `job-7.f399.mp4` is a complete file with no
    audio track. Recovering it hands the pipeline a silent video, and the
    failure surfaces hours later inside PyAV as "tuple index out of range".
    """
    (tmp_path / "job-7.f399.mp4").write_bytes(b"video only")
    (tmp_path / "job-7.f251-1.webm.part").write_bytes(b"audio, still arriving")

    assert _recover_downloaded_file(str(tmp_path), "job-7") is None


def test_a_merged_download_is_still_recovered(tmp_path):
    """The intermediates stay on disk after a merge; the merged file wins."""
    (tmp_path / "job-7.f399.mp4").write_bytes(b"video only")
    merged = tmp_path / "job-7.mp4"
    merged.write_bytes(b"video and audio")

    assert _recover_downloaded_file(str(tmp_path), "job-7") == str(merged)


class TestSettingsThatFailQuietly:
    """Two values the model accepted that broke the service without saying so."""

    def test_a_negative_retention_age_is_refused(self, configure):
        """It does not mean "keep forever" — it deletes everything.

        Each sweep cuts at `now - timedelta(days=setting)`. At -10 the cutoff
        lands ten days in the future, so every finished job is past it and
        every source download goes on the next sweep.
        """
        from pydantic import ValidationError

        from app.core.config import get_settings

        configure(APP_RETENTION_ORIGINALS_DAYS=-10)
        with pytest.raises(ValidationError, match="negative"):
            get_settings()

    def test_a_worker_with_no_threads_is_refused(self, configure):
        """Zero came up healthy, logged "worker started" and ran nothing."""
        from pydantic import ValidationError

        from app.core.config import get_settings

        configure(QUEUE_CONCURRENCY=0)
        with pytest.raises(ValidationError, match="at least 1"):
            get_settings()
