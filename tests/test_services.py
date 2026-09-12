"""Service rules: job status derivation, scheduling, and the duplicate guards.

These cover the behaviour that was previously spread across a router, a task
module and a scheduler — and that the three of them disagreed about.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import select

from app.core.clock import aware_utc_now, utc_now
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.enums import ClipStatus, JobStatus, PublicationStatus, TaskStatus
from app.db.models import Account, Clip, ClipJob, Publication, Task
from app.domain.cutting import SliceSpec
from app.services import accounts, clip_jobs, clips, publications, styles
from app.tasks import queue


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


@pytest.fixture()
def job(db):
    row = clip_jobs.create(
        db, source_ref="https://www.youtube.com/watch?v=abc123", start_immediately=False
    )
    db.commit()
    return row


def make_clip(db, job_id: int, index: int, *, status=ClipStatus.READY, path="/tmp/clip.mp4") -> Clip:
    clip = Clip(
        job_id=job_id,
        index=index,
        start_sec=(index - 1) * 60.0,
        end_sec=index * 60.0,
        duration_sec=60.0,
        title=f"clip {index}",
        text="some words",
        status=status,
        video_path=path if status == ClipStatus.READY else None,
    )
    db.add(clip)
    db.commit()
    db.refresh(clip)
    return clip


def future(minutes: int = 30):
    return aware_utc_now() + timedelta(minutes=minutes)


# ---- clip jobs -------------------------------------------------------------


class TestClipJobCreation:
    def test_platform_is_inferred_from_the_url(self, db):
        job = clip_jobs.create(db, source_ref="https://youtu.be/xyz", start_immediately=False)
        assert job.source_platform == "youtube"

    def test_a_missing_local_file_is_rejected_up_front(self, db):
        with pytest.raises(ValidationError, match="does not exist"):
            clip_jobs.create(db, source_ref="/nope/missing.mp4", source_platform="local")

    def test_starting_immediately_queues_exactly_one_task(self, db):
        clip_jobs.create(db, source_ref="https://youtu.be/xyz", start_immediately=True)
        db.commit()
        assert len(db.query(Task).all()) == 1

    def test_starting_twice_does_not_queue_a_second_download(self, db, job):
        first = clip_jobs.start(db, job.id)
        second = clip_jobs.start(db, job.id)
        db.commit()
        # A double-clicked button must not download the same video twice.
        assert first.id == second.id
        assert len(db.query(Task).all()) == 1

    def test_restarting_a_job_keeps_the_settings_it_was_created_with(self, db):
        """The restart endpoint sends an empty body, and used to mean "defaults".

        A job created for two 120-second clips came back from a retry as
        twenty clips of the default length, with nothing said about it.
        """
        from app.tasks import queue

        job = clip_jobs.create(
            db, source_ref="https://youtu.be/xyz", start_immediately=True,
            max_clips=2, max_clip_seconds=120.0, min_clip_seconds=90.0, gap_seconds=1.0,
        )
        db.commit()
        first = clip_jobs._active_task_for_job(db, job.id)
        queue.finish_failure(db, first.id, "boom", permanent=True)
        db.commit()

        restarted = clip_jobs.start(db, job.id)
        db.commit()

        payload = queue.payload_of(restarted)
        assert payload["max_clips"] == 2
        assert payload["max_clip_seconds"] == 120.0
        assert payload["min_clip_seconds"] == 90.0

    def test_an_unknown_job_is_not_found(self, db):
        with pytest.raises(NotFoundError):
            clip_jobs.get(db, 999)


class TestJobTitle:
    def test_a_custom_title_wins_over_the_detected_one(self, db, job):
        clip_jobs.set_custom_title(db, job.id, "  Мой   стрим  ")
        db.commit()
        refreshed = clip_jobs.get(db, job.id)
        assert refreshed.custom_title == "Мой стрим"
        assert refreshed.display_title == "Мой стрим"

    def test_an_empty_custom_title_clears_the_override(self, db, job):
        clip_jobs.set_custom_title(db, job.id, "Temporary")
        clip_jobs.set_custom_title(db, job.id, "")
        db.commit()
        assert clip_jobs.get(db, job.id).custom_title is None

    def test_a_cached_source_without_a_title_keeps_the_one_already_known(self, db, job):
        """Re-running a job reuses the downloaded file, whose name is
        `job-3.mp4`. Letting that become the title puts "job-3 - часть 1" in
        every TikTok caption."""
        clip_jobs.record_source(
            db, job.id, original_path="/tmp/job-3.mp4", duration_seconds=828.0,
            title="Это скучно, но это навсегда", thumbnail_path=None, metadata={},
        )
        # Second pass: the file is cached, so no title comes back.
        clip_jobs.record_source(
            db, job.id, original_path="/tmp/job-3.mp4", duration_seconds=828.0,
            title=None, thumbnail_path=None, metadata={},
        )
        db.commit()
        assert clip_jobs.get(db, job.id).title == "Это скучно, но это навсегда"

    def test_a_job_with_no_title_at_all_falls_back_to_the_filename(self, db, job):
        clip_jobs.record_source(
            db, job.id, original_path="/tmp/job-3.mp4", duration_seconds=1.0,
            title=None, thumbnail_path=None, metadata={},
        )
        db.commit()
        assert clip_jobs.get(db, job.id).title == "job-3.mp4"

    def test_recording_a_source_does_not_overwrite_a_custom_title(self, db, job):
        clip_jobs.set_custom_title(db, job.id, "Chosen by hand")
        clip_jobs.record_source(
            db,
            job.id,
            original_path="/tmp/source.mp4",
            duration_seconds=600.0,
            title="Whatever YouTube Called It",
            thumbnail_path=None,
            metadata={"title": "Whatever YouTube Called It"},
        )
        db.commit()
        assert clip_jobs.get(db, job.id).display_title == "Chosen by hand"


class TestJobStatusDerivation:
    """Status comes from the clips, and from nowhere else."""

    def test_a_job_is_rendering_while_any_clip_is_unfinished(self, db, job):
        make_clip(db, job.id, 1, status=ClipStatus.READY)
        make_clip(db, job.id, 2, status=ClipStatus.PLANNED)
        clip_jobs.refresh_status(db, job.id)
        db.commit()
        assert clip_jobs.get(db, job.id).status == JobStatus.RENDERING

    def test_a_job_is_ready_once_every_clip_has_finished(self, db, job):
        make_clip(db, job.id, 1)
        make_clip(db, job.id, 2)
        clip_jobs.refresh_status(db, job.id)
        db.commit()
        assert clip_jobs.get(db, job.id).status == JobStatus.READY

    def test_one_failed_clip_does_not_fail_the_job(self, db, job):
        make_clip(db, job.id, 1, status=ClipStatus.READY)
        make_clip(db, job.id, 2, status=ClipStatus.FAILED)
        clip_jobs.refresh_status(db, job.id)
        db.commit()
        refreshed = clip_jobs.get(db, job.id)
        # The other clips are publishable, which is the point of many clips.
        assert refreshed.status == JobStatus.READY
        assert "1 of 2 clips failed" in refreshed.error

    def test_a_job_fails_only_when_nothing_rendered(self, db, job):
        make_clip(db, job.id, 1, status=ClipStatus.FAILED)
        make_clip(db, job.id, 2, status=ClipStatus.FAILED)
        clip_jobs.refresh_status(db, job.id)
        db.commit()
        assert clip_jobs.get(db, job.id).status == JobStatus.FAILED


class TestSourceReuse:
    def test_a_rerun_of_the_same_url_reuses_the_downloaded_file(self, db, tmp_path):
        """Half a gigabyte, over a link that drops large transfers.

        The download is keyed by job id, so re-running a video with a
        different profile fetched the whole thing again: `job-8.mp4` and
        `job-9.mp4` sat in the media directory byte for byte identical.
        """
        source = tmp_path / "already-here.mp4"
        source.write_bytes(b"video")
        done = clip_jobs.create(db, source_ref="https://youtu.be/same", start_immediately=False)
        clip_jobs.record_source(
            db, done.id, original_path=str(source), duration_seconds=10.0,
            title=None, thumbnail_path=None, metadata={},
        )
        again = clip_jobs.create(db, source_ref="https://youtu.be/same", start_immediately=False)
        db.commit()

        found = clip_jobs.source_downloaded_elsewhere(db, again.id, "https://youtu.be/same")

        assert found == str(source)

    def test_a_source_that_was_deleted_is_not_offered(self, db, tmp_path):
        done = clip_jobs.create(db, source_ref="https://youtu.be/gone", start_immediately=False)
        clip_jobs.record_source(
            db, done.id, original_path=str(tmp_path / "swept-by-retention.mp4"),
            duration_seconds=10.0, title=None, thumbnail_path=None, metadata={},
        )
        again = clip_jobs.create(db, source_ref="https://youtu.be/gone", start_immediately=False)
        db.commit()

        assert clip_jobs.source_downloaded_elsewhere(db, again.id, "https://youtu.be/gone") is None

    def test_an_unmerged_download_is_never_offered(self, db, tmp_path):
        """`job-7.f399.mp4` is video with no audio, and it looks complete.

        A job row written before the downloader learned to refuse these still
        points at one. Handing it to the next job produced a silent source
        that failed 2000 seconds later inside the transcriber.
        """
        half = tmp_path / "job-7.f399.mp4"
        half.write_bytes(b"video only")
        done = clip_jobs.create(db, source_ref="https://youtu.be/half", start_immediately=False)
        clip_jobs.record_source(
            db, done.id, original_path=str(half), duration_seconds=10.0,
            title=None, thumbnail_path=None, metadata={},
        )
        again = clip_jobs.create(db, source_ref="https://youtu.be/half", start_immediately=False)
        db.commit()

        assert clip_jobs.source_downloaded_elsewhere(db, again.id, "https://youtu.be/half") is None

    def test_a_different_video_is_never_offered(self, db, tmp_path):
        source = tmp_path / "other.mp4"
        source.write_bytes(b"video")
        done = clip_jobs.create(db, source_ref="https://youtu.be/one", start_immediately=False)
        clip_jobs.record_source(
            db, done.id, original_path=str(source), duration_seconds=10.0,
            title=None, thumbnail_path=None, metadata={},
        )
        other = clip_jobs.create(db, source_ref="https://youtu.be/two", start_immediately=False)
        db.commit()

        assert clip_jobs.source_downloaded_elsewhere(db, other.id, "https://youtu.be/two") is None


class TestJobCancellation:
    def test_cancelling_stops_tasks_and_unfinished_clips(self, db, job):
        clip_jobs.start(db, job.id)
        make_clip(db, job.id, 1, status=ClipStatus.PLANNED)
        done = make_clip(db, job.id, 2, status=ClipStatus.READY)
        db.commit()

        clip_jobs.cancel(db, job.id)
        db.commit()

        assert clip_jobs.get(db, job.id).status == JobStatus.CANCELLED
        statuses = {c.index: c.status for c in clips.list_for_job(db, job.id)}
        assert statuses[1] == ClipStatus.CANCELLED
        # Work already finished is kept — cancelling is not deleting.
        assert statuses[2] == ClipStatus.READY
        assert all(t.status == TaskStatus.CANCELLED for t in db.query(Task).all())

    def test_a_cancelled_job_is_not_later_reported_as_failed(self, db, job):
        """Cancelling a running task still ends it through the failure hook.

        The queue reports every task that stops without succeeding to its
        handler's `on_failure`, cancelled ones included, and the download
        handler's hook fails the job. Job 8 was cancelled at 16:21 and read
        "failed: worker stopped responding" five minutes later.
        """
        clip_jobs.cancel(db, job.id)
        db.commit()

        clip_jobs.fail(db, job.id, "worker stopped responding; requeued")
        db.commit()

        refreshed = clip_jobs.get(db, job.id)
        assert refreshed.status == JobStatus.CANCELLED
        assert refreshed.error == "cancelled by user"

    def test_deleting_a_job_with_work_in_flight_is_refused(self, db, job):
        clip_jobs.start(db, job.id)
        db.commit()
        with pytest.raises(ConflictError, match="cancel the job"):
            clip_jobs.delete(db, job.id)


# ---- clips -----------------------------------------------------------------


class TestClipPlanning:
    def test_planning_creates_clips_and_queues_one_render_each(self, db, job):
        specs = [SliceSpec(i, (i - 1) * 60.0, i * 60.0, f"t{i}", "text", 3) for i in (1, 2, 3)]
        created = clips.plan(db, job.id, specs)
        db.commit()

        assert len(created) == 3
        renders = [t for t in db.query(Task).all() if t.kind == "render"]
        assert len(renders) == 3
        assert all(clip.task_id is not None for clip in created)

    def test_replanning_keeps_clips_that_already_rendered(self, db, job):
        rendered = make_clip(db, job.id, 1, status=ClipStatus.READY)
        make_clip(db, job.id, 2, status=ClipStatus.PLANNED)

        clips.plan(db, job.id, [SliceSpec(5, 0.0, 60.0, "new", "text", 1)])
        db.commit()

        remaining = {c.index for c in clips.list_for_job(db, job.id)}
        # The rendered file may already be referenced by a publication, so it
        # must survive; the unrendered plan is replaced.
        assert rendered.index in remaining
        assert 2 not in remaining
        assert 5 in remaining


class TestUnpublishedClips:
    def test_lists_only_ready_clips_with_no_active_publication(self, db, job, account):
        ready = make_clip(db, job.id, 1)
        make_clip(db, job.id, 2, status=ClipStatus.PLANNED)
        taken = make_clip(db, job.id, 3)
        publications.schedule_clip(
            db, clip_id=taken.id, username=account.username, scheduled_at=future()
        )
        db.commit()

        assert [c.id for c in clips.unpublished(db, job.id)] == [ready.id]

    def test_a_cancelled_publication_frees_its_clip(self, db, job, account):
        clip = make_clip(db, job.id, 1)
        publication = publications.schedule_clip(
            db, clip_id=clip.id, username=account.username, scheduled_at=future()
        )
        db.commit()
        assert clips.unpublished(db, job.id) == []

        publications.cancel(db, publication.id)
        db.commit()
        assert [c.id for c in clips.unpublished(db, job.id)] == [clip.id]


# ---- publications ----------------------------------------------------------


class TestScheduling:
    def test_scheduling_a_clip_generates_a_caption_and_queues_a_task(self, db, job, account):
        clip_jobs.set_custom_title(db, job.id, "Стрим недели")
        clip = make_clip(db, job.id, 2)
        row = publications.schedule_clip(
            db, clip_id=clip.id, username=account.username, scheduled_at=future()
        )
        db.commit()

        assert row.status == PublicationStatus.SCHEDULED
        assert row.caption.startswith("Стрим недели - часть 2")
        task = db.get(Task, row.task_id)
        assert task.kind == "publish"
        # The task must not run before its time.
        assert task.run_at == row.scheduled_at

    def test_an_explicit_caption_overrides_the_generated_one(self, db, job, account):
        clip = make_clip(db, job.id, 1)
        row = publications.schedule_clip(
            db,
            clip_id=clip.id,
            username=account.username,
            scheduled_at=future(),
            caption="Ровно то, что я написал",
        )
        db.commit()
        assert row.caption == "Ровно то, что я написал"

    def test_the_callers_own_mistake_is_reported_before_the_clip_conflict(
        self, db, account, job
    ):
        """"Already scheduled" is true and unhelpful when the request is wrong.

        Naming an account that does not exist, or a time in the past, used to
        come back as a conflict about the clip, because the clip's booking was
        checked first.
        """
        clip = make_clip(db, job.id, 1)
        db.commit()
        publications.schedule_clip(
            db, clip_id=clip.id, username="tester", scheduled_at=future(30)
        )
        db.commit()

        with pytest.raises(NotFoundError, match="nobody"):
            publications.schedule_clip(
                db, clip_id=clip.id, username="nobody", scheduled_at=future(30)
            )
        with pytest.raises(ValidationError, match="in the future"):
            publications.schedule_clip(
                db, clip_id=clip.id, username="tester", scheduled_at=future(-30)
            )
        with pytest.raises(ValidationError, match="private"):
            publications.schedule_clip(
                db, clip_id=clip.id, username="tester", scheduled_at=future(30),
                options={"visibility_type": 1},
            )

    def test_a_clip_cannot_be_scheduled_twice(self, db, job, account):
        clip = make_clip(db, job.id, 1)
        publications.schedule_clip(
            db, clip_id=clip.id, username=account.username, scheduled_at=future()
        )
        db.commit()
        with pytest.raises(ConflictError, match="already scheduled"):
            publications.schedule_clip(
                db, clip_id=clip.id, username=account.username, scheduled_at=future(60)
            )

    def test_an_unrendered_clip_cannot_be_scheduled(self, db, job, account):
        clip = make_clip(db, job.id, 1, status=ClipStatus.PLANNED)
        with pytest.raises(ConflictError, match="finish rendering"):
            publications.schedule_clip(
                db, clip_id=clip.id, username=account.username, scheduled_at=future()
            )

    def test_a_past_time_is_rejected(self, db, job, account):
        clip = make_clip(db, job.id, 1)
        with pytest.raises(ValidationError, match="future"):
            publications.schedule_clip(
                db,
                clip_id=clip.id,
                username=account.username,
                scheduled_at=aware_utc_now() - timedelta(minutes=1),
            )

    def test_a_naive_time_is_rejected(self, db, job, account):
        clip = make_clip(db, job.id, 1)
        with pytest.raises(ValidationError, match="timezone"):
            publications.schedule_clip(
                db, clip_id=clip.id, username=account.username, scheduled_at=utc_now()
            )

    def test_private_videos_cannot_be_scheduled(self, db, job, account):
        clip = make_clip(db, job.id, 1)
        with pytest.raises(ValidationError, match="private"):
            publications.schedule_clip(
                db,
                clip_id=clip.id,
                username=account.username,
                scheduled_at=future(),
                options={"visibility_type": 1},
            )

    def test_an_account_needing_login_cannot_be_scheduled_for(self, db, job, account):
        account.has_valid_session = False
        db.add(account)
        db.commit()
        clip = make_clip(db, job.id, 1)
        with pytest.raises(ConflictError, match="logged in again"):
            publications.schedule_clip(
                db, clip_id=clip.id, username=account.username, scheduled_at=future()
            )


class TestSchedulingAWholeJob:
    def test_clips_are_spaced_by_the_interval(self, db, job, account):
        for index in (1, 2, 3):
            make_clip(db, job.id, index, path=f"/tmp/clip{index}.mp4")
        start = future()

        rows = publications.schedule_job(
            db, job_id=job.id, username=account.username, first_at=start, interval_minutes=30
        )
        db.commit()

        gaps = [
            (b.scheduled_at - a.scheduled_at).total_seconds() / 60 for a, b in zip(rows, rows[1:])
        ]
        # Posting a wall of clips at once reads as spam and hits rate limits.
        assert gaps == [30.0, 30.0]

    def test_already_scheduled_clips_are_skipped(self, db, job, account):
        first = make_clip(db, job.id, 1, path="/tmp/a.mp4")
        make_clip(db, job.id, 2, path="/tmp/b.mp4")
        publications.schedule_clip(
            db, clip_id=first.id, username=account.username, scheduled_at=future()
        )
        db.commit()

        rows = publications.schedule_job(
            db, job_id=job.id, username=account.username, first_at=future(120)
        )
        db.commit()
        assert len(rows) == 1

    def test_a_job_with_nothing_to_schedule_is_a_conflict(self, db, job, account):
        with pytest.raises(ConflictError, match="no rendered"):
            publications.schedule_job(db, job_id=job.id, username=account.username, first_at=future())


class TestPublicationLifecycle:
    def test_rescheduling_moves_the_queued_task_too(self, db, job, account):
        clip = make_clip(db, job.id, 1)
        row = publications.schedule_clip(
            db, clip_id=clip.id, username=account.username, scheduled_at=future(10)
        )
        db.commit()

        moved_to = future(90)
        publications.reschedule(db, row.id, moved_to)
        db.commit()

        task = db.get(Task, row.task_id)
        # A publication moved without moving its task would fire at the old time.
        assert task.run_at == db.get(Publication, row.id).scheduled_at

    def test_cancelling_also_cancels_the_task(self, db, job, account):
        clip = make_clip(db, job.id, 1)
        row = publications.schedule_clip(
            db, clip_id=clip.id, username=account.username, scheduled_at=future()
        )
        db.commit()

        publications.cancel(db, row.id)
        db.commit()
        assert db.get(Publication, row.id).status == PublicationStatus.CANCELLED
        assert db.get(Task, row.task_id).status == TaskStatus.CANCELLED

    def test_a_publication_being_uploaded_cannot_be_cancelled(self, db, job, account):
        clip = make_clip(db, job.id, 1)
        row = publications.schedule_clip(
            db, clip_id=clip.id, username=account.username, scheduled_at=future()
        )
        publications.mark_publishing(db, row.id)
        db.commit()
        with pytest.raises(ConflictError, match="right now"):
            publications.cancel(db, row.id)

    def test_only_failed_publications_can_be_retried(self, db, job, account):
        clip = make_clip(db, job.id, 1)
        row = publications.schedule_clip(
            db, clip_id=clip.id, username=account.username, scheduled_at=future()
        )
        db.commit()
        with pytest.raises(ConflictError, match="only failed"):
            publications.retry(db, row.id)

    def test_a_bulk_retry_skips_anything_that_reached_tiktok(self, db, account, job):
        """The one-at-a-time rule exists for uploads that had already started.

        A publication whose task never became irreversible never sent a byte,
        so re-queueing it cannot duplicate a post. One that did might have.
        """
        untouched = make_clip(db, job.id, 1)
        started = make_clip(db, job.id, 2)
        db.commit()
        first = publications.schedule_clip(
            db, clip_id=untouched.id, username="tester", scheduled_at=future(30)
        )
        second = publications.schedule_clip(
            db, clip_id=started.id, username="tester", scheduled_at=future(60)
        )
        db.commit()
        queue.mark_irreversible(db, second.task_id)
        publications.mark_failed(db, first.id, "account needs to be logged in again")
        publications.mark_failed(db, second.id, "connection aborted mid-upload")
        db.commit()

        retried = publications.retry_untouched(db)
        db.commit()

        assert [p.id for p in retried] == [first.id]
        assert publications.get(db, first.id).status == PublicationStatus.SCHEDULED
        assert publications.get(db, second.id).status == PublicationStatus.FAILED

    def test_a_bulk_retry_keeps_the_gaps_between_posts(self, db, account, job):
        """Firing them all at once is what TikTok reads as spam."""
        made = []
        for index in range(3):
            clip = make_clip(db, job.id, index + 1)
            db.commit()
            made.append(
                publications.schedule_clip(
                    db, clip_id=clip.id, username="tester",
                    scheduled_at=future(30 + index * 30),
                )
            )
        db.commit()
        for row in made:
            publications.mark_failed(db, row.id, "account needs to be logged in again")
        db.commit()

        retried = publications.retry_untouched(db)
        db.commit()

        times = [p.scheduled_at for p in retried]
        assert len(times) == 3
        gaps = [round((b - a).total_seconds() / 60) for a, b in zip(times, times[1:])]
        assert gaps == [30, 30], "the original spacing must survive the retry"
        assert times[0] > aware_utc_now().replace(tzinfo=None)

    def test_retrying_queues_a_fresh_task(self, db, job, account):
        clip = make_clip(db, job.id, 1)
        row = publications.schedule_clip(
            db, clip_id=clip.id, username=account.username, scheduled_at=future()
        )
        publications.mark_failed(db, row.id, "TikTok said no")
        db.commit()

        retried = publications.retry(db, row.id)
        db.commit()
        assert retried.status == PublicationStatus.SCHEDULED
        assert retried.result_text is None
        # A new task, because the old dedupe key was released.
        assert len([t for t in db.query(Task).all() if t.kind == "publish"]) == 2

    def test_publishing_stamps_the_account_as_used(self, db, job, account):
        clip = make_clip(db, job.id, 1)
        row = publications.schedule_clip(
            db, clip_id=clip.id, username=account.username, scheduled_at=future()
        )
        publications.mark_published(db, row.id, video_id="7123456")
        db.commit()

        refreshed = db.get(Publication, row.id)
        assert refreshed.status == PublicationStatus.PUBLISHED
        assert "7123456" in refreshed.result_text
        assert refreshed.published_at is not None
        assert db.get(Account, account.id).last_used_at is not None


# ---- accounts --------------------------------------------------------------


class TestAccountSessionFlag:
    def test_an_account_stops_reading_as_signed_in_when_its_cookies_vanish(self, db, account):
        """The flag is a cache of the file, and it only went stale dangerously.

        A real account showed as signed in for weeks after its cookie file
        disappeared. Nothing noticed until a scheduled post failed at the
        minute it was due, with eleven more queued behind it.
        """
        from app.adapters.tiktok import cookies as cookie_store

        cookie_store.delete("tester")

        assert accounts.get_by_username(db, "tester").has_valid_session is False
        with pytest.raises(ConflictError, match="no cookie file"):
            accounts.get_ready(db, "tester")

    def test_a_present_file_never_raises_the_flag_back(self, db, account):
        """TikTok rejecting a session is knowledge the file cannot overturn."""
        accounts.invalidate_session(db, account.id)
        db.commit()

        assert accounts.get_by_username(db, "tester").has_valid_session is False

    def test_an_account_with_publications_cannot_be_deleted(self, db, account, job):
        """The foreign key refused anyway — as a bare 500 from the API."""
        clip = make_clip(db, job.id, 1)
        db.commit()
        publications.schedule_clip(
            db, clip_id=clip.id, username="tester", scheduled_at=future(30)
        )
        db.commit()

        with pytest.raises(ConflictError, match="publication"):
            accounts.delete(db, account.id)


class TestAccounts:
    def test_registering_without_cookies_is_refused(self, db):
        with pytest.raises(ConflictError, match="no cookie file"):
            accounts.register(db, "ghost")

    def test_invalidating_a_session_flags_the_account(self, db, account):
        accounts.invalidate_session(db, account.id)
        db.commit()
        assert db.get(Account, account.id).has_valid_session is False

    def test_a_flagged_account_is_not_ready(self, db, account):
        accounts.invalidate_session(db, account.id)
        db.commit()
        with pytest.raises(ConflictError):
            accounts.get_ready(db, account.username)


class TestStyleResolution:
    """Where a job's look is settled, and when.

    Once, at the moment the job starts — not per render. Otherwise the
    fiftieth clip of a long job comes out different from the first because
    somebody edited a preset while it was running.
    """

    def _job(self, db, **kwargs):
        return clip_jobs.create(
            db, source_ref="https://youtu.be/abc", start_immediately=False, **kwargs
        )

    def test_starting_a_job_freezes_its_style_into_the_payload(self, db):
        job = self._job(db)
        clip_jobs.start(db, job.id)
        db.commit()

        payload = queue.payload_of(
            db.exec(select(Task).where(Task.kind == "download")).one()
        )

        style = payload["render"]["style"]
        assert style["delivery"]["width"] == 1080
        assert style["pacing"]["remove_silence"] is True  # the talking profile

    def test_a_preset_beats_the_profile_in_the_frozen_style(self, db):
        preset = styles.create(
            db, name="Nothing removed", data={"pacing": {"remove_silence": False}}
        )
        job = self._job(db, style_id=preset.id)
        clip_jobs.start(db, job.id)
        db.commit()

        payload = queue.payload_of(
            db.exec(select(Task).where(Task.kind == "download")).one()
        )

        assert payload["render"]["style"]["pacing"]["remove_silence"] is False

    def test_editing_a_preset_does_not_reach_a_job_already_running(self, db):
        preset = styles.create(db, name="House", data={"subtitles": {"font_size": 100}})
        job = self._job(db, style_id=preset.id)
        clip_jobs.start(db, job.id)
        db.commit()

        styles.update(db, preset.id, data={"subtitles": {"font_size": 40}})
        db.commit()

        payload = queue.payload_of(
            db.exec(select(Task).where(Task.kind == "download")).one()
        )
        assert payload["render"]["style"]["subtitles"]["font_size"] == 100


class TestClipRerender:
    def _rendered_clip(self, db) -> Clip:
        job = clip_jobs.create(db, source_ref="https://youtu.be/abc", start_immediately=False)
        job.original_path = "/media/source.mp4"
        db.add(job)
        db.flush()
        created = clips.plan(
            db, job.id, [SliceSpec(1, 0.0, 60.0, "headline", "words", 3)],
            render_options={"source_path": "/media/source.mp4", "source_title": "Source"},
        )
        clip = created[0]
        clips.mark_ready(
            db, clip.id, video_path="/media/clip.mp4", cover_path=None,
            composition={"segments": [], "style": {}},
        )
        db.commit()
        return clip

    def test_a_rerender_keeps_the_boundaries_and_the_source(self, db):
        """Replanning the job would recut every clip in it; this repeats one."""
        clip = self._rendered_clip(db)

        clips.rerender(db, clip.id, style={"subtitles": {"font_size": 96}})
        db.commit()

        task = db.exec(
            select(Task).where(Task.kind == "render").order_by(Task.id.desc())
        ).first()
        payload = queue.payload_of(task)
        assert payload["render"]["source_path"] == "/media/source.mp4"
        assert payload["render"]["style"] == {"subtitles": {"font_size": 96}}
        assert clip.start_sec == 0.0 and clip.end_sec == 60.0

    def test_a_rerender_is_not_deduplicated_against_the_render_it_repeats(self, db):
        """The plan-time dedupe key would swallow it and nothing would happen."""
        clip = self._rendered_clip(db)
        before = len(db.exec(select(Task).where(Task.kind == "render")).all())

        clips.rerender(db, clip.id)
        db.commit()

        after = len(db.exec(select(Task).where(Task.kind == "render")).all())
        assert after == before + 1

    def test_a_clip_already_rendering_is_not_queued_twice(self, db):
        clip = self._rendered_clip(db)
        clips.mark_rendering(db, clip.id)
        db.commit()

        with pytest.raises(ConflictError):
            clips.rerender(db, clip.id)

    def test_a_finished_clip_hands_back_what_it_was_made_of(self, db):
        clip = self._rendered_clip(db)

        assert clips.composition_of(db, clip.id) == {"segments": [], "style": {}}
