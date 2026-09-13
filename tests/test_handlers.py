"""Task handlers, driven end to end with the outside world faked.

Downloading, ffmpeg and TikTok are replaced; everything else — the queue, the
services, the status transitions — is real. That is what makes these the tests
that would have caught a job stuck in "slicing" with all its clips rendered.
"""
from __future__ import annotations

import os
from datetime import timedelta

import pytest
from sqlmodel import select

from app.core.clock import aware_utc_now
from app.core.errors import SessionExpiredError
from app.db.enums import ClipStatus, JobStatus, PublicationStatus, TaskKind, TaskStatus
from app.db.models import Account, Clip, ClipJob, Publication, Task
from app.db.session import session_scope
from app.domain.transcript import TranscriptSegment, TranscriptWord
from app.services import clip_jobs, publications
from app.tasks import queue, runner
from app.tasks.registry import load_handlers

load_handlers()


@pytest.fixture()
def source_file(tmp_path):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"\x00" * 2048)
    return str(path)


@pytest.fixture()
def account(db):
    """An account with a cookie file, so it passes the readiness checks.

    The file is the point: `has_valid_session` is only a cache of it, and an
    account whose cookies are gone must not read as signed in.
    """
    from app.adapters.tiktok import cookies as cookie_store

    cookie_store.save("tester", [{"name": "sessionid", "value": "abc", "domain": ".tiktok.com"}])
    row = Account(
        username="tester",
        cookie_path=str(cookie_store.cookie_path("tester")),
        has_valid_session=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def transcript(duration: float = 600.0) -> list[TranscriptSegment]:
    """Steady speech with word timings, long enough to cut several clips."""
    segments = []
    start = 0.0
    index = 0
    while start < duration:
        end = min(start + 5.0, duration)
        segments.append(
            TranscriptSegment(
                start_sec=start,
                end_sec=end,
                text=f"Sentence number {index}.",
                words=[TranscriptWord(start_sec=start, end_sec=start + 0.4, text=f"w{index}")],
            )
        )
        start = end + 0.2
        index += 1
    return segments


# ---- download --------------------------------------------------------------


class TestDownloadHandler:
    @pytest.fixture(autouse=True)
    def _fakes(self, monkeypatch, source_file):
        from app.adapters.media import ffmpeg
        from app.adapters.youtube import downloader
        from app.services import transcripts

        monkeypatch.setattr(
            downloader, "download_source",
            lambda ref, *, job_id, on_progress=None: (
                source_file, {"title": "Source Video", "duration": 600.0}
            ),
        )
        monkeypatch.setattr(ffmpeg, "ffprobe_duration", lambda path: 600.0)
        monkeypatch.setattr(ffmpeg, "prepare_source_thumbnail", lambda job_id, url: None)
        monkeypatch.setattr(ffmpeg, "best_thumbnail_url", lambda metadata: None)
        monkeypatch.setattr(
            transcripts, "load_or_build",
            lambda *a, **k: transcripts.TranscriptResult(transcript(), "fake", {}),
        )

    def test_a_job_downloads_transcribes_and_plans_clips(self, db):
        job = clip_jobs.create(db, source_ref="https://youtu.be/abc", start_immediately=True)
        db.commit()

        assert runner.run_once([TaskKind.DOWNLOAD]) is True

        db.expire_all()
        refreshed = db.get(ClipJob, job.id)
        assert refreshed.status == JobStatus.RENDERING
        assert refreshed.duration_seconds == 600.0
        assert refreshed.title == "Source Video"

        clips = db.query(Clip).all()
        assert len(clips) > 1
        assert all(clip.status == ClipStatus.PLANNED for clip in clips)
        # One render task per clip — that is where parallelism comes from.
        renders = [t for t in db.query(Task).all() if t.kind == TaskKind.RENDER]
        assert len(renders) == len(clips)

    def test_a_source_with_no_speech_fails_permanently(self, db, monkeypatch):
        from app.services import transcripts

        monkeypatch.setattr(
            transcripts, "load_or_build",
            lambda *a, **k: transcripts.TranscriptResult([], "fake", {}),
        )
        job = clip_jobs.create(db, source_ref="https://youtu.be/silent", start_immediately=True)
        db.commit()

        runner.run_once([TaskKind.DOWNLOAD])

        db.expire_all()
        task = db.query(Task).first()
        # No point retrying: the video will still have no speech next time.
        assert task.status == TaskStatus.FAILED
        assert task.attempts == 1
        assert db.get(ClipJob, job.id).status == JobStatus.FAILED

    def test_a_retrying_download_stops_the_job_from_claiming_it_is_queued(
        self, db, monkeypatch
    ):
        """A job in backoff must not look like one nobody picked up.

        The retry delay is minutes long. While a failed attempt left the job at
        "queued", 0%, the only honest reading of the UI was that no worker was
        running at all — and the error that would have explained it was only in
        the task row.
        """
        from app.adapters.youtube import downloader

        def unreachable(ref, *, job_id, on_progress=None):
            raise RuntimeError("YouTube could not be reached at all")

        monkeypatch.setattr(downloader, "download_source", unreachable)
        job = clip_jobs.create(db, source_ref="https://youtu.be/abc", start_immediately=True)
        db.commit()

        runner.run_once([TaskKind.DOWNLOAD])

        db.expire_all()
        task = db.query(Task).first()
        assert task.status == TaskStatus.QUEUED  # attempts remain
        refreshed = db.get(ClipJob, job.id)
        assert refreshed.status == JobStatus.DOWNLOADING  # not a failure yet
        assert refreshed.stage != "queued"
        assert "could not be reached" in refreshed.error

    def test_a_download_in_flight_reports_itself_on_the_job(self, db, monkeypatch):
        """Fetching a source is the longest step; the job must say so while it
        happens rather than after it returns."""
        from app.adapters.youtube import downloader

        seen = {}

        def capture(ref, *, job_id, on_progress=None):
            with session_scope() as session:
                job = session.get(ClipJob, job_id)
                seen["status"], seen["stage"] = job.status, job.stage
            raise RuntimeError("stop here")

        monkeypatch.setattr(downloader, "download_source", capture)
        job = clip_jobs.create(db, source_ref="https://youtu.be/abc", start_immediately=True)
        db.commit()

        runner.run_once([TaskKind.DOWNLOAD])

        assert seen["status"] == JobStatus.DOWNLOADING
        assert seen["stage"] != "queued"

    def test_a_slow_download_moves_the_job_while_it_runs(self, db, monkeypatch, source_file):
        """Bytes arriving must show up as progress.

        A twenty-minute source held the job at a fixed 5% from the first second
        to the last, which is indistinguishable from a download that stalled.
        """
        from app.adapters.youtube import downloader

        def slow(ref, *, job_id, on_progress=None):
            for fraction in (0.25, 0.5, 1.0):
                on_progress(fraction)
            return source_file, {"title": "Source Video", "duration": 600.0}

        monkeypatch.setattr(downloader, "download_source", slow)
        job = clip_jobs.create(db, source_ref="https://youtu.be/slow", start_immediately=True)
        db.commit()

        seen = []
        original = clip_jobs.set_progress

        def record(session, job_id, status, stage, progress):
            seen.append((stage, round(progress, 3)))
            original(session, job_id, status, stage, progress)

        monkeypatch.setattr(clip_jobs, "set_progress", record)
        runner.run_once([TaskKind.DOWNLOAD])

        downloading = [entry for entry in seen if entry[0].startswith("downloading")]
        assert downloading == [("downloading 25%", 0.075), ("downloading 50%", 0.1),
                               ("downloading 100%", 0.15)]
        # The band it owns hands over to transcription without going backwards.
        # ("rendering" restarts at 0 on purpose: renders count clips, not bytes.)
        upto_render = [p for stage, p in seen if stage != "rendering"]
        assert upto_render == sorted(upto_render)

    def test_the_film_profile_cuts_without_a_transcript(self, db, monkeypatch):
        """An action scene with no dialogue produced no clips at all before
        profiles existed — the job failed outright."""
        from app.adapters.media import scenes
        from app.services import transcripts

        def explode(*a, **k):
            raise AssertionError("the film profile must not transcribe")

        monkeypatch.setattr(transcripts, "load_or_build", explode)
        monkeypatch.setattr(
            scenes, "analyse",
            lambda path, **k: scenes.SceneAnalysis(
                scene_changes=tuple(float(at) for at in range(95, 600, 95)),
                loudness=tuple((float(s), -20.0) for s in range(600)),
            ),
        )
        job = clip_jobs.create(
            db, source_ref="https://youtu.be/film", profile="film", start_immediately=True
        )
        db.commit()

        assert runner.run_once([TaskKind.DOWNLOAD]) is True

        db.expire_all()
        assert db.get(ClipJob, job.id).status == JobStatus.RENDERING
        clips = db.query(Clip).all()
        assert len(clips) > 1
        # Cut where the picture cuts, not at a round number of seconds.
        assert clips[0].end_sec == 95.0

    def test_a_profile_cannot_make_the_scene_cutter_transcribe(self, db, monkeypatch):
        """The coupling this replaced.

        `requires_transcript` was the montage side's field, and it gated ASR at
        the cutting stage: a talking profile pointed at the scene cutter used to
        transcribe a whole source that its cutter never read a word of. Whether
        the subtitles later want a transcript is a separate question, asked by
        whoever burns them.
        """
        from app.adapters.media import scenes
        from app.services import transcripts

        def explode(*a, **k):
            raise AssertionError("the scene cutter must not transcribe")

        monkeypatch.setattr(transcripts, "load_or_build", explode)
        monkeypatch.setattr(
            scenes, "analyse",
            lambda path, **k: scenes.SceneAnalysis(
                scene_changes=tuple(float(at) for at in range(95, 600, 95)),
                loudness=tuple((float(s), -20.0) for s in range(600)),
            ),
        )
        job = clip_jobs.create(
            db, source_ref="https://youtu.be/abc", profile="talking",
            start_immediately=False,
        )
        clip_jobs.start(db, job.id, render={"cutter": "scenes"})
        db.commit()

        assert runner.run_once([TaskKind.DOWNLOAD]) is True

        db.expire_all()
        assert db.get(ClipJob, job.id).status == JobStatus.RENDERING
        assert db.query(Clip).count() > 1

    def test_the_job_remembers_what_its_source_was_transcribed_with(self, db, monkeypatch):
        """A fact the cutting stage learned, kept so the montage side finds the
        same cached transcript instead of paying ASR a second time."""
        from app.services import transcripts

        monkeypatch.setattr(
            transcripts, "load_or_build",
            lambda *a, **k: transcripts.TranscriptResult(
                transcript(), "fake", {"model": "small", "language": "ru"}
            ),
        )
        job = clip_jobs.create(db, source_ref="https://youtu.be/abc", start_immediately=True)
        db.commit()

        assert runner.run_once([TaskKind.DOWNLOAD]) is True

        db.expire_all()
        refreshed = db.get(ClipJob, job.id)
        assert clip_jobs.transcript_settings(refreshed) == {
            "model": "small", "language": "ru",
        }

    def test_a_job_that_was_never_transcribed_says_so_rather_than_guessing(
        self, db, monkeypatch
    ):
        from app.adapters.media import scenes
        from app.services import transcripts

        monkeypatch.setattr(transcripts, "load_or_build", lambda *a, **k: None)
        monkeypatch.setattr(
            scenes, "analyse",
            lambda path, **k: scenes.SceneAnalysis(
                scene_changes=tuple(float(at) for at in range(95, 600, 95)),
                loudness=tuple((float(s), -20.0) for s in range(600)),
            ),
        )
        job = clip_jobs.create(
            db, source_ref="https://youtu.be/film", profile="film", start_immediately=True
        )
        db.commit()

        assert runner.run_once([TaskKind.DOWNLOAD]) is True

        db.expire_all()
        assert clip_jobs.transcript_settings(db.get(ClipJob, job.id)) is None

    def test_planning_hands_the_render_nothing_but_the_clip(self, db):
        """Cutting's output is boundaries. How they are dressed is on the job,
        and a render reads it there rather than being couriered a copy."""
        job = clip_jobs.create(db, source_ref="https://youtu.be/abc", start_immediately=True)
        db.commit()
        runner.run_once([TaskKind.DOWNLOAD])

        db.expire_all()
        render = next(t for t in db.query(Task).all() if t.kind == TaskKind.RENDER)

        assert set(queue.payload_of(render)) == {"clip_id", "job_id"}

    def test_a_film_whose_scenes_cannot_be_read_still_produces_clips(self, db, monkeypatch):
        """Losing the scene signal should cost worse cut points, not the job."""
        from app.adapters.media import scenes
        from app.services import transcripts

        monkeypatch.setattr(transcripts, "load_or_build", lambda *a, **k: None)
        monkeypatch.setattr(
            scenes, "analyse",
            lambda path, **k: scenes.SceneAnalysis(ok=False, detail="ffmpeg is not installed"),
        )
        clip_jobs.create(
            db, source_ref="https://youtu.be/film", profile="film", start_immediately=True
        )
        db.commit()

        assert runner.run_once([TaskKind.DOWNLOAD]) is True

        db.expire_all()
        assert len(db.query(Clip).all()) > 1

    def test_the_error_for_a_silent_video_names_the_profile_that_would_work(self, db, monkeypatch):
        from app.services import transcripts

        monkeypatch.setattr(
            transcripts, "load_or_build",
            lambda *a, **k: transcripts.TranscriptResult([], "fake", {}),
        )
        clip_jobs.create(db, source_ref="https://youtu.be/silent", start_immediately=True)
        db.commit()

        runner.run_once([TaskKind.DOWNLOAD])

        db.expire_all()
        assert "film" in db.query(Task).first().error

    def test_a_cancelled_job_stops_before_planning(self, db):
        job = clip_jobs.create(db, source_ref="https://youtu.be/abc", start_immediately=True)
        db.commit()
        task = db.query(Task).first()
        from app.tasks import queue

        queue.request_cancel(db, task.id)
        db.commit()

        # A cancelled task is never claimed.
        assert runner.run_once([TaskKind.DOWNLOAD]) is False
        assert db.query(Clip).all() == []


# ---- render ----------------------------------------------------------------


class TestRenderHandler:
    @pytest.fixture(autouse=True)
    def _fakes(self, monkeypatch, tmp_path, source_file):
        from app.adapters.asr import cache, selection
        from app.adapters.media import ffmpeg
        from montage.render import compiler
        from montage.render import probe as media_probe

        rendered = tmp_path / "rendered.mp4"
        rendered.write_bytes(b"\x00" * 512)
        cover = tmp_path / "rendered.cover.jpg"
        cover.write_bytes(b"\x00" * 64)

        monkeypatch.setattr(cache, "load_media_transcript", lambda *a, **k: None)
        # Word timings, not just text: they are what subtitle cues and b-roll
        # placement are both built from, so faking them away would leave the
        # handler exercising a path it never takes in production.
        transcript = [
            {
                "start_sec": 10.0,
                "end_sec": 41.0,
                "text": "spoken words again",
                "words": [
                    {"text": "spoken", "start_sec": 12.0, "end_sec": 12.6},
                    {"text": "words", "start_sec": 26.0, "end_sec": 26.6},
                    {"text": "again", "start_sec": 40.0, "end_sec": 40.6},
                ],
            }
        ]
        monkeypatch.setattr(
            selection, "select_subtitle_transcript",
            lambda *a, **k: (transcript, {"source": "fake"}),
        )
        monkeypatch.setattr(ffmpeg, "clip_output_path", lambda *a, **k: str(rendered))
        monkeypatch.setattr(media_probe, "cover_path_for", lambda path: str(cover))
        # Only the encode is faked. Planning runs for real, so what these tests
        # exercise is the composition the handler actually hands to ffmpeg.
        self.rendered_compositions = []
        self.rendered_kwargs = []

        def fake_render(composition, output_path, **kwargs):
            self.rendered_compositions.append(composition)
            self.rendered_kwargs.append(kwargs)
            return compiler.ClipRenderResult(
                output_path=str(rendered), output_duration=60.0, segment_count=1,
                subtitles_path=None, subtitle_count=0, silence_removed_seconds=0.0,
                strategy=compiler.STRATEGY_ONE_PASS, fragments_reused=0, qa={},
            )

        monkeypatch.setattr(compiler, "render", fake_render)
        monkeypatch.setattr(media_probe, "render_clip_cover", lambda **k: str(cover))
        self.rendered = str(rendered)

    def _planned_clip(self, db, source_file, render_options=None) -> tuple[int, int]:
        """A job with a source and one planned clip, as cutting would leave it.

        The montage options go on the job, which is where a render reads them
        from; planning is handed boundaries and nothing else. The source path
        and title are the job's own fields rather than options, because a
        render that needs them looks them up there.
        """
        from app.core.jsonutil import dumps
        from app.domain.cutting import SliceSpec
        from app.services import clips

        job = clip_jobs.create(db, source_ref="https://youtu.be/abc", start_immediately=False)
        job.original_path = source_file
        job.title = "Source"
        job.render_options_json = dumps(render_options or {})
        db.add(job)
        db.commit()
        created = clips.plan(db, job.id, [SliceSpec(1, 0.0, 60.0, "headline", "spoken words", 3)])
        db.commit()
        return job.id, created[0].id

    def _queue_options_on_the_task(self, db, clip_id, options) -> None:
        """Put montage options in the render task's payload.

        Where they used to live, and where a re-render still puts the caller's
        own. A test that sets them on the job instead is not exercising either.
        """
        from app.core.jsonutil import dumps

        clip = db.get(Clip, clip_id)
        task = db.get(Task, clip.task_id)
        payload = {**queue.payload_of(task), "render": options}
        task.payload_json = dumps(payload)
        db.add(task)
        db.commit()

    def test_rendering_marks_the_clip_ready_and_the_job_ready(self, db, source_file):
        job_id, clip_id = self._planned_clip(db, source_file)

        assert runner.run_once([TaskKind.RENDER]) is True

        db.expire_all()
        clip = db.get(Clip, clip_id)
        assert clip.status == ClipStatus.READY
        assert clip.video_path == self.rendered
        assert clip.cover_path is not None
        # The job's status follows from its clips, with no separate bookkeeping.
        assert db.get(ClipJob, job_id).status == JobStatus.READY

    def test_broll_from_the_library_reaches_the_composition(self, db, source_file):
        """The end of the chain: an uploaded fragment, tagged with a word the
        clip actually says, ends up as an insert in what gets rendered."""
        from app.services import assets

        asset = assets.save_upload(
            db, filename="broll.mp4", data=b"pretend video", tags="spoken"
        )
        db.commit()
        job_id, clip_id = self._planned_clip(db, source_file)

        assert runner.run_once([TaskKind.RENDER]) is True

        composition = self.rendered_compositions[-1]
        assert [insert.source_path for insert in composition.inserts] == [asset.path]
        # Placed on the word itself, not somewhere near it.
        assert composition.inserts[0].at_sec == 12.0
        # Using a fragment is recorded, so the next clip reaches for another one.
        db.expire_all()
        assert db.get(type(asset), asset.id).use_count == 1

    def test_broll_can_be_switched_off_per_job(self, db, source_file):
        from app.services import assets

        assets.save_upload(db, filename="broll.mp4", data=b"pretend video", tags="spoken")
        db.commit()
        job_id, clip_id = self._planned_clip(db, source_file, render_options={"inserts": False})

        assert runner.run_once([TaskKind.RENDER]) is True

        assert self.rendered_compositions[-1].inserts == ()

    def test_the_split_profile_puts_a_background_under_the_speaker(self, db, source_file):
        from app.services import assets

        background = assets.save_upload(
            db, filename="gameplay.mp4", data=b"pretend gameplay", tags="background"
        )
        db.commit()
        job_id, clip_id = self._planned_clip(
            db, source_file, render_options={"layout": "split", "inserts": False}
        )

        assert runner.run_once([TaskKind.RENDER]) is True

        composition = self.rendered_compositions[-1]
        assert composition.layouts == frozenset({"split"})
        assert composition.segments[0].companion_path == background.path

    def test_a_split_screen_with_nothing_to_put_under_it_falls_back(self, db, source_file):
        """A missing background should cost the split screen, not the clip."""
        job_id, clip_id = self._planned_clip(
            db, source_file, render_options={"layout": "split"}
        )

        assert runner.run_once([TaskKind.RENDER]) is True

        db.expire_all()
        assert self.rendered_compositions[-1].layouts == frozenset({"blur"})
        assert db.get(Clip, clip_id).status == ClipStatus.READY

    def test_a_soundtrack_is_chosen_from_the_library(self, db, source_file):
        """Music, b-roll and a transition sound all come out of the same
        library, tagged for the job each does."""
        from app.services import assets

        track = assets.save_upload(db, filename="bed.mp3", data=b"a track", tags="music")
        whoosh = assets.save_upload(db, filename="whoosh.wav", data=b"a whoosh", tags="sfx")
        assets.save_upload(db, filename="broll.mp4", data=b"pretend video", tags="spoken")
        db.commit()
        job_id, clip_id = self._planned_clip(
            db, source_file, render_options={"music": True, "sfx": True}
        )

        assert runner.run_once([TaskKind.RENDER]) is True

        composition = self.rendered_compositions[-1]
        assert composition.music is not None
        assert composition.music.source_path == track.path
        # The b-roll appears at 12s, so the transition sound leads it.
        assert [effect.source_path for effect in composition.effects] == [whoosh.path]
        assert composition.effects[0].at_sec == pytest.approx(11.88)

    def test_a_soundtrack_is_recorded_as_used_like_any_other_asset(self, db, source_file):
        from app.services import assets

        track = assets.save_upload(db, filename="bed.mp3", data=b"a track", tags="music")
        db.commit()
        self._planned_clip(db, source_file, render_options={"music": True, "inserts": False})

        assert runner.run_once([TaskKind.RENDER]) is True

        db.expire_all()
        assert db.get(type(track), track.id).use_count == 1

    def test_music_can_be_switched_off_per_job(self, db, source_file):
        from app.services import assets

        assets.save_upload(db, filename="bed.mp3", data=b"a track", tags="music")
        db.commit()
        self._planned_clip(db, source_file, render_options={"music": False, "sfx": False})

        assert runner.run_once([TaskKind.RENDER]) is True

        assert self.rendered_compositions[-1].music is None
        assert self.rendered_compositions[-1].effects == ()

    def test_a_library_without_music_leaves_the_clip_alone(self, db, source_file):
        self._planned_clip(db, source_file, render_options={"music": True, "sfx": True})

        assert runner.run_once([TaskKind.RENDER]) is True

        assert self.rendered_compositions[-1].music is None

    def test_a_failed_render_marks_only_that_clip(self, db, source_file, monkeypatch):
        from montage.render import compiler

        def explode(*a, **k):
            raise RuntimeError("ffmpeg exited with code 1")

        monkeypatch.setattr(compiler, "render", explode)
        job_id, clip_id = self._planned_clip(db, source_file)

        # Exhaust the retries.
        for _ in range(5):
            with session_scope() as session:
                task = session.get(Task, db.get(Clip, clip_id).task_id)
                if task and task.status == TaskStatus.QUEUED:
                    task.run_at = aware_utc_now().replace(tzinfo=None)
                    session.add(task)
            if not runner.run_once([TaskKind.RENDER]):
                break

        db.expire_all()
        assert db.get(Clip, clip_id).status == ClipStatus.FAILED
        assert db.get(ClipJob, job_id).status == JobStatus.FAILED


# ---- publish ---------------------------------------------------------------


class TestRenderRefusesAMissingSource:
    def test_a_swept_source_fails_at_once_instead_of_retrying(self, db, tmp_path):
        """Named but not on disk: three attempts used to be spent on it.

        Retention deletes a job's source a few days after it finishes, so a
        re-render queued after that arrives with a path to nothing. The
        failure came out of ffmpeg, deep in the compiler, and the queue tried
        it twice more over fifteen minutes.
        """
        job = clip_jobs.create(
            db, source_ref="https://youtu.be/swept", start_immediately=False
        )
        job.original_path = str(tmp_path / "swept-away.mp4")
        db.add(job)
        db.commit()
        clip = Clip(
            job_id=job.id, index=1, start_sec=0.0, end_sec=60.0, duration_sec=60.0,
            title="clip 1", text="", status=ClipStatus.PLANNED,
        )
        db.add(clip)
        db.commit()

        queue.enqueue(db, TaskKind.RENDER, {"clip_id": clip.id, "job_id": job.id})
        db.commit()
        runner.run_once([TaskKind.RENDER])

        db.expire_all()
        task = db.exec(select(Task).where(Task.kind == TaskKind.RENDER)).one()
        assert task.status == TaskStatus.FAILED
        assert task.attempts == 1, "a missing file will not appear on a retry"
        assert "source file is gone" in (task.error or "")


class TestPublishHandler:
    def _scheduled(self, db, account, tmp_path) -> int:
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"\x00" * 256)
        job = clip_jobs.create(db, source_ref="https://youtu.be/abc", start_immediately=False)
        db.commit()
        clip = Clip(
            job_id=job.id, index=1, start_sec=0, end_sec=60, duration_sec=60,
            status=ClipStatus.READY, video_path=str(video), title="t", text="words",
        )
        db.add(clip)
        db.commit()
        db.refresh(clip)
        row = publications.schedule_clip(
            db,
            clip_id=clip.id,
            username=account.username,
            scheduled_at=aware_utc_now() + timedelta(seconds=1),
        )
        db.commit()
        # Make it due now.
        task = db.get(Task, row.task_id)
        task.run_at = aware_utc_now().replace(tzinfo=None) - timedelta(seconds=1)
        db.add(task)
        db.commit()
        return row.id

    def test_a_successful_upload_marks_the_publication_published(
        self, db, account, tmp_path, monkeypatch
    ):
        from app.adapters.tiktok import client

        monkeypatch.setattr(client, "upload_video", lambda *a, **k: "7123456789")
        publication_id = self._scheduled(db, account, tmp_path)

        assert runner.run_once([TaskKind.PUBLISH]) is True

        db.expire_all()
        row = db.get(Publication, publication_id)
        assert row.status == PublicationStatus.PUBLISHED
        assert "7123456789" in row.result_text
        assert db.get(Account, account.id).last_used_at is not None

    def test_an_expired_session_fails_permanently_and_flags_the_account(
        self, db, account, tmp_path, monkeypatch
    ):
        from app.adapters.tiktok import client

        def expired(*a, **k):
            raise SessionExpiredError("TikTok rejected the session")

        monkeypatch.setattr(client, "upload_video", expired)
        publication_id = self._scheduled(db, account, tmp_path)

        runner.run_once([TaskKind.PUBLISH])

        db.expire_all()
        row = db.get(Publication, publication_id)
        assert row.status == PublicationStatus.FAILED
        # Retrying an expired login can only fail the same way.
        assert db.get(Task, row.task_id).status == TaskStatus.FAILED
        assert db.get(Task, row.task_id).attempts == 1
        assert db.get(Account, account.id).has_valid_session is False
        # Nothing was uploaded: both places this error comes from are earlier
        # than the first byte of video, so the publication must not be left
        # looking like one that might already be live.
        assert db.get(Task, row.task_id).irreversible is False

    def test_the_task_is_marked_irreversible_before_uploading(
        self, db, account, tmp_path, monkeypatch
    ):
        """The duplicate-post guard, checked where it matters."""
        from app.adapters.tiktok import client

        seen: dict[str, bool] = {}

        def capture(*a, **k):
            with session_scope() as session:
                task = session.query(Task).filter(Task.kind == TaskKind.PUBLISH).first()
                seen["irreversible"] = bool(task.irreversible)
            return "7000"

        monkeypatch.setattr(client, "upload_video", capture)
        self._scheduled(db, account, tmp_path)
        runner.run_once([TaskKind.PUBLISH])

        # Set before the upload starts, so a crash mid-publish cannot retry.
        assert seen["irreversible"] is True

    def test_a_failure_mid_upload_warns_that_the_video_may_be_live(
        self, db, account, tmp_path, monkeypatch
    ):
        """A dropped connection during upload is ambiguous: TikTok may have
        accepted the video. The publication has to say so, because the next
        step a person takes is clicking retry."""
        from app.adapters.tiktok import client

        def drop(*a, **k):
            raise ConnectionError("Connection aborted, RemoteDisconnected")

        monkeypatch.setattr(client, "upload_video", drop)
        publication_id = self._scheduled(db, account, tmp_path)

        runner.run_once([TaskKind.PUBLISH])

        db.expire_all()
        row = db.get(Publication, publication_id)
        assert row.status == PublicationStatus.FAILED
        assert "check the account" in row.result_text
        assert account.username in row.result_text
        # And it is not retried on its own, which could post a duplicate.
        assert db.get(Task, row.task_id).status == TaskStatus.FAILED

    def test_an_already_published_row_is_not_published_again(
        self, db, account, tmp_path, monkeypatch
    ):
        from app.adapters.tiktok import client

        calls = []
        monkeypatch.setattr(client, "upload_video", lambda *a, **k: calls.append(1) or "7001")
        publication_id = self._scheduled(db, account, tmp_path)
        publications.mark_published(db, publication_id, video_id="already")
        db.commit()

        runner.run_once([TaskKind.PUBLISH])
        assert calls == []

    def test_a_publication_never_stays_stuck_when_its_worker_dies(
        self, db, account, tmp_path
    ):
        """A killed worker leaves a task with an expired lease and nothing
        running. The publication must not sit in 'publishing' forever — that
        state refuses both cancel and retry, so nobody could act on it."""
        from app.tasks import queue

        publication_id = self._scheduled(db, account, tmp_path)
        publications.mark_publishing(db, publication_id)
        db.commit()

        # Simulate the worker dying after the point of no return.
        task = db.get(Task, db.get(Publication, publication_id).task_id)
        task.status = TaskStatus.RUNNING
        task.irreversible = True
        task.attempts = 1
        task.lease_expires_at = aware_utc_now().replace(tzinfo=None) - timedelta(seconds=1)
        db.add(task)
        db.commit()

        runner.run_once([TaskKind.PUBLISH])

        db.expire_all()
        row = db.get(Publication, publication_id)
        assert row.status == PublicationStatus.FAILED
        assert db.get(Task, row.task_id).status == TaskStatus.FAILED
        # And the message says the video may be live, because it might be.
        assert "verify before retrying" in row.result_text

    def test_a_missing_video_file_fails_permanently(self, db, account, tmp_path):
        publication_id = self._scheduled(db, account, tmp_path)
        with session_scope() as session:
            row = session.get(Publication, publication_id)
            row.source_ref = "/does/not/exist.mp4"
            session.add(row)

        runner.run_once([TaskKind.PUBLISH])

        db.expire_all()
        task = db.get(Task, db.get(Publication, publication_id).task_id)
        assert task.status == TaskStatus.FAILED
        assert task.attempts == 1


# ---- cleanup ---------------------------------------------------------------


class TestCleanupHandler:
    def test_sources_of_finished_jobs_are_deleted_after_the_retention_window(
        self, db, tmp_path, configure
    ):
        from app.tasks import queue

        configure(APP_RETENTION_ORIGINALS_DAYS=1)
        source = tmp_path / "old-source.mp4"
        source.write_bytes(b"\x00" * 4096)

        job = ClipJob(
            source_platform="youtube",
            source_ref="https://youtu.be/old",
            status=JobStatus.READY,
            original_path=str(source),
            updated_at=aware_utc_now().replace(tzinfo=None) - timedelta(days=5),
        )
        db.add(job)
        db.commit()

        queue.enqueue(db, TaskKind.CLEANUP, {})
        db.commit()
        runner.run_once([TaskKind.CLEANUP])

        assert not source.exists()
        db.expire_all()
        assert db.get(ClipJob, job.id).original_path is None

    def test_a_source_two_jobs_share_survives_until_both_expire(
        self, db, tmp_path, configure
    ):
        """Re-running a video reuses its download, so one file can have two owners.

        Deleting it for the older job would leave the newer one pointing at a
        path that is gone, and its next render fails looking for it.
        """
        from app.tasks import queue

        configure(APP_RETENTION_ORIGINALS_DAYS=1)
        source = tmp_path / "shared-source.mp4"
        source.write_bytes(b"\x00" * 4096)
        old = ClipJob(
            source_platform="youtube", source_ref="https://youtu.be/shared",
            status=JobStatus.READY, original_path=str(source),
            updated_at=aware_utc_now().replace(tzinfo=None) - timedelta(days=5),
        )
        recent = ClipJob(
            source_platform="youtube", source_ref="https://youtu.be/shared",
            status=JobStatus.READY, original_path=str(source),
        )
        db.add(old)
        db.add(recent)
        db.commit()

        queue.enqueue(db, TaskKind.CLEANUP, {})
        db.commit()
        runner.run_once([TaskKind.CLEANUP])

        assert source.exists()
        db.expire_all()
        assert db.get(ClipJob, old.id).original_path == str(source)

    def test_leftovers_from_an_unfinished_download_are_swept(self, db, tmp_path, configure):
        """A failed download leaves files no rule above ever looks at.

        `.part`, its per-fragment pieces, `.ytdl` and the format intermediates
        written before a merge that never happened were never any job's
        `original_path`, so nothing deleted them: 63 MB from two jobs that had
        failed three days earlier.
        """
        from app.tasks import queue

        originals = tmp_path / "media" / "originals"
        originals.mkdir(parents=True)
        configure(AUTOCLIPS_DIR=str(tmp_path / "media"))

        stale = aware_utc_now().timestamp() - 48 * 3600
        leftovers = ["job-4.f616.mp4.part", "job-4.f616.mp4.part-Frag15.part",
                     "job-4.f616.mp4.ytdl", "job-7.f399.mp4"]
        for name in leftovers:
            path = originals / name
            path.write_bytes(b"\x00" * 1024)
            os.utime(path, (stale, stale))
        keep = originals / "job-4.mp4"
        keep.write_bytes(b"\x00" * 1024)
        os.utime(keep, (stale, stale))
        in_flight = originals / "job-9.mp4.part"
        in_flight.write_bytes(b"\x00" * 1024)

        queue.enqueue(db, TaskKind.CLEANUP, {})
        db.commit()
        runner.run_once([TaskKind.CLEANUP])

        assert not any((originals / name).exists() for name in leftovers)
        assert keep.exists(), "a finished download is not a leftover"
        assert in_flight.exists(), "a download still being written must survive"

    def test_a_job_stops_naming_a_source_that_is_already_gone(self, db, tmp_path, configure):
        """The sweep skipped a vanished file and left the row pointing at it.

        `_remove` reported bytes freed, so a path that had already gone said
        zero — indistinguishable from a failed delete — and the sweep left
        `original_path` set. Every later sweep read it the same way, and the
        job's next render died looking for a file nothing could read.
        """
        from app.tasks.handlers import cleanup

        job = clip_jobs.create(
            db, source_ref="https://youtu.be/gone", start_immediately=False
        )
        job.status = JobStatus.READY
        job.original_path = str(tmp_path / "never-existed.mp4")
        job.updated_at = aware_utc_now().replace(tzinfo=None) - timedelta(days=90)
        db.add(job)
        db.commit()

        removed, freed = cleanup._remove_sources(db, [job])
        db.commit()

        assert (removed, freed) == (0, 0), "nothing was on disk to free"
        db.expire_all()
        assert db.get(ClipJob, job.id).original_path is None

    def test_an_undeletable_source_keeps_its_row(self, db, tmp_path, monkeypatch):
        """A file that is still there must keep being named."""
        from app.tasks.handlers import cleanup

        source = tmp_path / "locked.mp4"
        source.write_bytes(bytes(64))
        job = clip_jobs.create(
            db, source_ref="https://youtu.be/locked", start_immediately=False
        )
        job.original_path = str(source)
        db.add(job)
        db.commit()

        def refuse(_path):
            raise OSError("in use")

        monkeypatch.setattr(cleanup.os, "remove", refuse)
        removed, freed = cleanup._remove_sources(db, [job])
        db.commit()

        assert (removed, freed) == (0, 0)
        db.expire_all()
        assert db.get(ClipJob, job.id).original_path == str(source)

    def test_stale_yt_dlp_cookie_jars_are_swept(self, db, tmp_path, configure):
        """One jar per worker thread per start, and nothing removed them.

        `options._writable_cookie_jar` copies the read-only YouTube export so
        yt-dlp can save its own jar back; the thread id in the name is new
        every start. Seventeen had piled up in the directory that also holds
        the TikTok sessions.
        """
        from app.tasks import queue

        cookies = tmp_path / "cookies"
        cookies.mkdir(exist_ok=True)
        configure(APP_COOKIES_DIR=str(cookies))

        stale = aware_utc_now().timestamp() - 48 * 3600
        old_jar = cookies / "ytdlp-jar.1.123456789.txt"
        old_jar.write_text("# stale", encoding="utf-8")
        os.utime(old_jar, (stale, stale))
        fresh_jar = cookies / "ytdlp-jar.1.987654321.txt"
        fresh_jar.write_text("# in use", encoding="utf-8")
        session_file = cookies / "tiktok_session-tester.cookie"
        session_file.write_bytes(b"a tiktok session")
        os.utime(session_file, (stale, stale))

        queue.enqueue(db, TaskKind.CLEANUP, {})
        db.commit()
        runner.run_once([TaskKind.CLEANUP])

        assert not old_jar.exists()
        assert fresh_jar.exists(), "a jar a running download may still write to"
        assert session_file.exists(), "a TikTok session is not a yt-dlp jar"

    def test_a_sweep_queues_the_next_one(self, db):
        """The chain is what makes retention periodic at all.

        Nothing runs cleanup on a timer; each sweep books its successor, and
        the API seeds the first. Without this the handler ran once, if ever.
        """
        from app.db.models import Task
        from app.tasks import queue

        queue.enqueue(db, TaskKind.CLEANUP, {}, dedupe_key="cleanup:periodic")
        db.commit()
        runner.run_once([TaskKind.CLEANUP])

        db.expire_all()
        pending = [
            t for t in db.query(Task).all()
            if t.kind == TaskKind.CLEANUP and t.status == TaskStatus.QUEUED
        ]
        assert len(pending) == 1
        assert pending[0].run_at > aware_utc_now().replace(tzinfo=None)

    def test_recent_jobs_are_left_alone(self, db, tmp_path):
        from app.tasks import queue

        source = tmp_path / "fresh-source.mp4"
        source.write_bytes(b"\x00" * 4096)
        job = ClipJob(
            source_platform="youtube",
            source_ref="https://youtu.be/fresh",
            status=JobStatus.READY,
            original_path=str(source),
        )
        db.add(job)
        db.commit()

        queue.enqueue(db, TaskKind.CLEANUP, {})
        db.commit()
        runner.run_once([TaskKind.CLEANUP])

        assert source.exists()


class TestRenderedStyle(TestRenderHandler):
    """What the style is for: it reaches the render, and it is written down.

    Inherits the fakes above — only the encode is stubbed, so the composition
    these assert on is the one the handler really hands to ffmpeg.
    """

    def test_a_job_style_reaches_the_composition(self, db, source_file):
        self._planned_clip(
            db, source_file,
            render_options={"style": {"subtitles": {"font_size": 120}, "framing": {"zoom": 1.4}}},
        )

        assert runner.run_once([TaskKind.RENDER]) is True

        style = self.rendered_compositions[-1].style
        assert style.subtitles.font_size == 120
        assert style.framing.zoom == 1.4

    def test_the_profile_still_decides_what_the_style_is_silent_about(self, db, source_file):
        self._planned_clip(db, source_file, render_options={"style": {"grade": {"contrast": 1.1}}})

        assert runner.run_once([TaskKind.RENDER]) is True

        style = self.rendered_compositions[-1].style
        assert style.grade.contrast == 1.1
        # `talking` is the default profile, and it removes silence.
        assert style.pacing.remove_silence is True

    def test_a_task_queued_before_styles_existed_still_renders(self, db, source_file):
        """The queue can be full during an upgrade. Those tasks carry the old
        flat switches in their own payload, and their job carries nothing."""
        _, clip_id = self._planned_clip(db, source_file)
        self._queue_options_on_the_task(
            db, clip_id,
            {"auto_montage": False, "inserts": False, "subtitle_font_size": 64},
        )

        assert runner.run_once([TaskKind.RENDER]) is True

        style = self.rendered_compositions[-1].style
        assert style.pacing.remove_silence is False
        assert style.inserts.enabled is False
        assert style.subtitles.font_size == 64

    def test_what_the_task_asks_for_wins_over_the_job_without_erasing_it(
        self, db, source_file
    ):
        """A re-render changes one thing and must not silently drop the rest.

        The look is not the example to test that with: a style is resolved
        whole before it is queued, so a task carrying one carries all of it and
        replacing it is correct. `strategy` is the option a style has no say
        over, which makes it the one that shows whether the job's side of the
        merge survived at all.
        """
        _, clip_id = self._planned_clip(
            db, source_file,
            render_options={
                "strategy": "two_stage", "style": {"subtitles": {"font_size": 40}},
            },
        )
        self._queue_options_on_the_task(
            db, clip_id, {"style": {"subtitles": {"font_size": 96}}}
        )

        assert runner.run_once([TaskKind.RENDER]) is True

        assert self.rendered_compositions[-1].style.subtitles.font_size == 96  # the task's
        assert self.rendered_kwargs[-1]["strategy"] == "two_stage"             # still the job's

    def test_a_finished_clip_records_what_it_was_made_of(self, db, source_file):
        """Stored so the clip can be inspected, and re-rendered without
        re-deciding anything."""
        import json

        _, clip_id = self._planned_clip(db, source_file)

        assert runner.run_once([TaskKind.RENDER]) is True

        db.expire_all()
        stored = json.loads(db.get(Clip, clip_id).composition_json)
        assert stored["segments"]
        assert stored["style"]["delivery"]["width"] == 1080
        # And it loads back into the very thing that produced it.
        from montage import composition as comp

        assert comp.from_dict(stored) == self.rendered_compositions[-1]
