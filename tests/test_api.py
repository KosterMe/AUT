"""HTTP surface: status codes, error mapping, and the auth seam.

Routers hold no logic, so these tests check the translation layer — that a
domain refusal becomes the right status code, and that a configured token is
actually enforced.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.clock import aware_utc_now
from app.db.enums import ClipStatus
from app.db.models import Account, Clip


@pytest.fixture()
def client(db):
    from app.api.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture()
def account(db, settings):
    """An account with a cookie file, so it passes the readiness checks."""
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


def iso_future(minutes: int = 30) -> str:
    return (aware_utc_now() + timedelta(minutes=minutes)).isoformat()


class TestHealth:
    def test_reports_queue_depth(self, client):
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert "tasks" in body
        # A steadily rising number here means no worker is serving that kind.
        assert body["tasks_due_now"] == 0


class TestBrowserLoginAvailability:
    def test_health_says_whether_a_browser_login_could_work(self, client):
        """The UI needs this to stop offering a button that only 503s.

        The flow drives a real Chrome and waits for a person to get past
        TikTok's captcha, so it needs the dependency and a desktop. The image
        has neither on purpose, and a Chrome opened inside a container is one
        nobody can sign in to.
        """
        body = client.get("/api/health").json()

        assert "browser_login" in body
        assert isinstance(body["browser_login"], bool)

    def test_it_is_false_without_the_driver(self, client, monkeypatch):
        import importlib.util

        from app.api.routers import system

        monkeypatch.setattr(
            system.importlib.util, "find_spec",
            lambda name: None if name == "undetected_chromedriver" else importlib.util.find_spec(name),
        )

        assert client.get("/api/health").json()["browser_login"] is False


class TestJobs:
    def test_creating_a_job_returns_201(self, client):
        response = client.post(
            "/api/jobs",
            json={"source_ref": "https://www.youtube.com/watch?v=abc", "start_immediately": False},
        )
        assert response.status_code == 201
        assert response.json()["source_platform"] == "youtube"

    def test_a_non_youtube_url_declared_as_youtube_is_rejected(self, client):
        response = client.post(
            "/api/jobs",
            json={"source_ref": "https://vimeo.com/1", "source_platform": "youtube"},
        )
        assert response.status_code == 422

    def test_max_shorter_than_min_is_rejected(self, client):
        response = client.post(
            "/api/jobs",
            json={
                "source_ref": "https://youtu.be/abc",
                "min_clip_seconds": 120,
                "max_clip_seconds": 60,
            },
        )
        assert response.status_code == 422

    def test_an_unknown_job_is_404(self, client):
        assert client.get("/api/jobs/999").status_code == 404

    def test_the_detail_view_includes_clips(self, client, db):
        job_id = client.post(
            "/api/jobs", json={"source_ref": "https://youtu.be/abc", "start_immediately": False}
        ).json()["id"]
        db.add(
            Clip(
                job_id=job_id, index=1, start_sec=0, end_sec=60, duration_sec=60,
                status=ClipStatus.READY, video_path="/tmp/x.mp4", title="first",
            )
        )
        db.commit()

        body = client.get(f"/api/jobs/{job_id}").json()
        assert [clip["index"] for clip in body["clips"]] == [1]

    def test_renaming_a_job(self, client):
        job_id = client.post(
            "/api/jobs", json={"source_ref": "https://youtu.be/abc", "start_immediately": False}
        ).json()["id"]
        body = client.patch(f"/api/jobs/{job_id}/title", json={"custom_title": "Мой стрим"}).json()
        assert body["custom_title"] == "Мой стрим"


class TestPublications:
    def _ready_clip(self, client, db) -> int:
        job_id = client.post(
            "/api/jobs", json={"source_ref": "https://youtu.be/abc", "start_immediately": False}
        ).json()["id"]
        clip = Clip(
            job_id=job_id, index=1, start_sec=0, end_sec=60, duration_sec=60,
            status=ClipStatus.READY, video_path="/tmp/x.mp4", title="first", text="words",
        )
        db.add(clip)
        db.commit()
        db.refresh(clip)
        return clip.id

    def test_scheduling_a_clip_returns_201(self, client, db, account):
        clip_id = self._ready_clip(client, db)
        response = client.post(
            f"/api/publications/clip/{clip_id}",
            json={"username": account.username, "scheduled_at": iso_future()},
        )
        assert response.status_code == 201
        assert response.json()["status"] == "scheduled"

    def test_a_conflict_becomes_409(self, client, db, account):
        clip_id = self._ready_clip(client, db)
        payload = {"username": account.username, "scheduled_at": iso_future()}
        client.post(f"/api/publications/clip/{clip_id}", json=payload)
        second = client.post(f"/api/publications/clip/{clip_id}", json=payload)
        assert second.status_code == 409
        assert "already scheduled" in second.json()["detail"]

    def test_an_unknown_account_becomes_404(self, client, db):
        clip_id = self._ready_clip(client, db)
        response = client.post(
            f"/api/publications/clip/{clip_id}",
            json={"username": "nobody", "scheduled_at": iso_future()},
        )
        assert response.status_code == 404

    def test_a_past_time_becomes_422(self, client, db, account):
        clip_id = self._ready_clip(client, db)
        past = (aware_utc_now() - timedelta(hours=1)).isoformat()
        response = client.post(
            f"/api/publications/clip/{clip_id}",
            json={"username": account.username, "scheduled_at": past},
        )
        assert response.status_code == 422

    def test_timestamps_come_back_as_utc_with_a_z(self, client, db, account):
        clip_id = self._ready_clip(client, db)
        body = client.post(
            f"/api/publications/clip/{clip_id}",
            json={"username": account.username, "scheduled_at": iso_future()},
        ).json()
        # Without the Z the browser would read these as local time.
        assert body["scheduled_at"].endswith("Z")
        assert body["created_at"].endswith("Z")


class TestLoginImport:
    def test_importing_a_cookie_jar_creates_the_account(self, client, db):
        cookies = [
            {"name": "sessionid", "value": "s3cret", "domain": ".tiktok.com"},
            {"name": "tt-target-idc", "value": "useast2a", "domain": ".tiktok.com"},
        ]
        response = client.post(
            "/api/login/import", json={"username": "imported", "cookies": cookies}
        )
        assert response.status_code == 201
        assert response.json()["status"] == "completed"
        assert any(a["username"] == "imported" for a in client.get("/api/accounts").json())

    def test_cookies_without_a_session_are_rejected(self, client, db):
        response = client.post(
            "/api/login/import",
            json={"username": "broken", "cookies": [{"name": "ttwid", "value": "x"}]},
        )
        assert response.status_code == 422
        assert "sessionid" in response.json()["detail"]

    def test_a_netscape_cookies_file_is_accepted(self, client, db):
        text = "\n".join(
            [
                "# Netscape HTTP Cookie File",
                "\t".join([".tiktok.com", "TRUE", "/", "TRUE", "1999999999", "sessionid", "abc"]),
                "\t".join([".tiktok.com", "TRUE", "/", "TRUE", "1999999999", "tt-target-idc", "useast2a"]),
            ]
        )
        response = client.post("/api/login/import", json={"username": "netscape", "cookies": text})
        assert response.status_code == 201


    def test_browser_login_says_so_when_there_is_no_browser(self, client, monkeypatch):
        """A server has no Chrome, and the caller has to hear that now.

        The adapter was imported inside the worker thread, so the request
        answered 201 Created and the only way to learn it could never work was
        to poll a login session that had already failed.
        """
        import builtins

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name == "app.adapters.tiktok.login" or name.endswith("tiktok.login"):
                raise ImportError("no module named 'undetected_chromedriver'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)
        response = client.post("/api/login/browser", json={"username": "desktop"})

        assert response.status_code == 503
        assert "import" in response.json()["detail"].lower()


class TestAuth:
    def test_requests_pass_when_no_token_is_configured(self, client):
        assert client.get("/api/health").status_code == 200

    def test_a_configured_token_is_enforced(self, db, configure):
        from app.api.main import create_app

        configure(APP_AUTH_TOKEN="s3cret")
        with TestClient(create_app()) as guarded:
            assert guarded.get("/api/health").status_code == 401
            assert guarded.get("/api/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
            ok = guarded.get("/api/health", headers={"Authorization": "Bearer s3cret"})
            assert ok.status_code == 200


class TestTaskVisibility:
    def test_queued_tasks_are_listed_with_their_payload(self, client):
        client.post(
            "/api/jobs", json={"source_ref": "https://youtu.be/abc", "start_immediately": True}
        )
        tasks = client.get("/api/tasks", params={"kind": "download"}).json()
        assert len(tasks) == 1
        assert tasks[0]["kind"] == "download"
        assert "job_id" in tasks[0]["payload"]

    def test_the_recurring_media_sweep_is_on_the_queue(self, client):
        """Starting the API is what gets retention running.

        Nothing else ever enqueued a cleanup task, so the handler that keeps
        `data/media` from growing without bound had never run at all.
        """
        tasks = client.get("/api/tasks", params={"kind": "cleanup"}).json()
        assert [t["status"] for t in tasks] == ["queued"]

    def test_a_queued_task_can_be_cancelled(self, client):
        client.post(
            "/api/jobs", json={"source_ref": "https://youtu.be/abc", "start_immediately": True}
        )
        task_id = client.get("/api/tasks", params={"kind": "download"}).json()[0]["id"]
        body = client.post(f"/api/tasks/{task_id}/cancel").json()
        assert body["status"] == "cancelled"


class TestStyles:
    """Named looks over HTTP.

    The endpoint that carries the weight is /defaults: it is what lets a form
    show every value that will be used without anybody having filled anything
    in, and therefore what keeps "configurable" from turning into "required".
    """

    def test_defaults_describe_a_profile_completely(self, client):
        body = client.get("/api/styles/defaults", params={"profile": "talking"}).json()

        assert body["profile"] == "talking"
        assert body["style"]["delivery"]["width"] == 1080
        assert body["style"]["subtitles"]["font_size"] == 82
        # The profile's own opinions are already folded in.
        assert body["style"]["pacing"]["remove_silence"] is True
        assert "film" in body["profiles"]

    def test_a_preset_stores_only_what_it_overrides(self, client):
        created = client.post(
            "/api/styles",
            json={"name": "Big type", "data": {"subtitles": {"font_size": 110}}},
        )
        assert created.status_code == 201
        body = created.json()

        assert body["data"] == {"subtitles": {"font_size": 110}}
        # ...and reports what those overrides come out as, so the UI can show
        # the other thirty-nine controls without resolving anything itself.
        assert body["resolved"]["subtitles"]["font_size"] == 110
        assert body["resolved"]["framing"]["zoom"] == 1.2

    def test_an_unusable_style_is_refused_while_somebody_is_looking(self, client):
        response = client.post(
            "/api/styles",
            json={"name": "Broken", "data": {"framing": {"layout": "hexagon"}}},
        )

        assert response.status_code == 422

    def test_two_presets_cannot_share_a_name(self, client):
        client.post("/api/styles", json={"name": "House"})
        again = client.post("/api/styles", json={"name": "House"})

        assert again.status_code == 409

    def test_updating_data_replaces_it_rather_than_merging(self, client):
        """Merging would make it impossible to put a field back to the
        default, which is how somebody undoes an override."""
        style_id = client.post(
            "/api/styles",
            json={"name": "Loud", "data": {"subtitles": {"font_size": 110, "uppercase": True}}},
        ).json()["id"]

        body = client.patch(
            f"/api/styles/{style_id}", json={"data": {"subtitles": {"uppercase": True}}}
        ).json()

        assert body["data"] == {"subtitles": {"uppercase": True}}
        assert body["resolved"]["subtitles"]["font_size"] == 82

    def test_deleting_a_preset_leaves_its_jobs_renderable(self, client, db, tmp_path):
        """A job whose look was deleted falls back to its profile rather than
        failing: unattended means nothing stops because a row went away."""
        source = tmp_path / "source.mp4"
        source.write_bytes(b"\x00" * 2048)
        style_id = client.post("/api/styles", json={"name": "Temporary"}).json()["id"]
        job_id = client.post(
            "/api/jobs",
            json={
                "source_ref": str(source),
                "source_platform": "local",
                "style_id": style_id,
                "start_immediately": False,
            },
        ).json()["id"]

        assert client.delete(f"/api/styles/{style_id}").status_code == 204
        assert client.get(f"/api/jobs/{job_id}").json()["style_id"] is None

    def test_a_job_can_be_created_with_a_named_look(self, client, tmp_path):
        source = tmp_path / "source.mp4"
        source.write_bytes(b"\x00" * 2048)
        style_id = client.post(
            "/api/styles", json={"name": "Vertical", "data": {"framing": {"layout": "fill"}}}
        ).json()["id"]

        body = client.post(
            "/api/jobs",
            json={
                "source_ref": str(source),
                "source_platform": "local",
                "style_id": style_id,
                "start_immediately": False,
            },
        ).json()

        assert body["style_id"] == style_id

    def test_a_missing_style_is_a_404_rather_than_a_silent_default(self, client, tmp_path):
        source = tmp_path / "source.mp4"
        source.write_bytes(b"\x00" * 2048)

        response = client.post(
            "/api/jobs",
            json={
                "source_ref": str(source),
                "source_platform": "local",
                "style_id": 999,
                "start_immediately": False,
            },
        )

        assert response.status_code == 404


class TestClipEndpoints:
    """Acting on one clip: read what it is made of, render it again, preview it.

    The suite runs without ffmpeg, so the preview's encoder is stubbed — what
    is being checked here is the wiring around it, which is where the mistakes
    that matter live: which style was resolved, and which window was asked for.
    """

    @pytest.fixture()
    def clip(self, db, tmp_path):
        from app.domain.cutting import SliceSpec
        from app.services import clip_jobs, clips

        source = tmp_path / "source.mp4"
        source.write_bytes(b"\x00" * 4096)
        job = clip_jobs.create(
            db, source_ref=str(source), source_platform="local", start_immediately=False
        )
        job.original_path = str(source)
        job.title = "Source"
        db.add(job)
        db.flush()
        created = clips.plan(db, job.id, [SliceSpec(1, 0.0, 60.0, "headline", "words", 3)])
        clips.mark_ready(
            db, created[0].id, video_path=str(source), cover_path=None,
            composition={"segments": [{"source_path": str(source)}], "style": {}},
        )
        db.commit()
        return created[0]

    def test_a_clip_reports_what_it_was_rendered_from(self, client, clip):
        body = client.get(f"/api/clips/{clip.id}/composition").json()

        assert body["clip_id"] == clip.id
        assert body["composition"]["segments"]

    def test_a_clip_never_rendered_reports_nothing_rather_than_failing(self, client, clip, db):
        planned = Clip(
            job_id=clip.job_id, index=2, start_sec=60.0, end_sec=120.0, duration_sec=60.0
        )
        db.add(planned)
        db.commit()
        db.refresh(planned)

        body = client.get(f"/api/clips/{planned.id}/composition").json()

        assert body["composition"] == {}

    def test_rerendering_one_clip_queues_one_render(self, client, clip, db):
        from app.db.models import Task

        response = client.post(f"/api/clips/{clip.id}/render", json={})

        assert response.status_code == 202
        renders = [task for task in db.query(Task).all() if task.kind == "render"]
        assert len(renders) == 2  # the original plus this one

    def test_rerendering_with_a_style_resolves_it_before_queueing(self, client, clip, db):
        """Resolved here, not in the worker: what the clip renders with has to
        be settled at the moment somebody asked for it."""
        from app.db.models import Task
        from app.tasks import queue

        style_id = client.post(
            "/api/styles", json={"name": "Tall type", "data": {"subtitles": {"font_size": 120}}}
        ).json()["id"]

        client.post(f"/api/clips/{clip.id}/render", json={"style_id": style_id})

        task = sorted(
            (t for t in db.query(Task).all() if t.kind == "render"), key=lambda t: t.id
        )[-1]
        style = queue.payload_of(task)["render"]["style"]
        assert style["subtitles"]["font_size"] == 120
        assert style["delivery"]["width"] == 1080  # the rest is filled in

    def test_a_preview_renders_the_window_asked_for(self, client, clip, monkeypatch):
        from montage.render import compiler
        from app.api.routers import clips as clips_router

        seen = {}

        def fake_preview(composition, output_path, *, spec=None):
            seen["spec"] = spec
            seen["style"] = composition.style
            with open(output_path, "wb") as handle:
                handle.write(b"\x00" * 32)

        monkeypatch.setattr(clips_router.montage, "preview", fake_preview)
        monkeypatch.setattr(
            clips_router.rendering, "compose_clip",
            lambda **kwargs: _plan_stub(kwargs["style"], str(clip.video_path)),
        )

        response = client.post(
            f"/api/clips/{clip.id}/preview",
            json={"at_sec": 5.0, "duration_sec": 3.0, "style": {"framing": {"zoom": 1.5}}},
        )

        assert response.status_code == 200
        assert response.headers["content-type"] == "video/mp4"
        assert seen["spec"].at_sec == 5.0
        assert seen["spec"].duration_sec == 3.0
        assert seen["style"].framing.zoom == 1.5

    def test_a_preview_window_past_the_end_is_clamped_not_refused(
        self, client, clip, monkeypatch
    ):
        """Silence removal makes the montage shorter than the clip, so a
        slider placed against the source can legitimately overshoot."""
        from app.api.routers import clips as clips_router

        seen = {}

        def fake_preview(composition, output_path, *, spec=None):
            seen["spec"] = spec
            with open(output_path, "wb") as handle:
                handle.write(b"\x00" * 32)

        monkeypatch.setattr(clips_router.montage, "preview", fake_preview)
        monkeypatch.setattr(
            clips_router.rendering, "compose_clip",
            lambda **kwargs: _plan_stub(kwargs["style"], str(clip.video_path)),
        )

        response = client.post(
            f"/api/clips/{clip.id}/preview", json={"at_sec": 900.0, "duration_sec": 4.0}
        )

        assert response.status_code == 200
        assert seen["spec"].at_sec == 6.0  # a 10s composition minus the window


def _plan_stub(style, source_path: str):
    """A composed clip, without the transcript and library work behind it."""
    from montage import composition as comp
    from app.services import rendering

    return rendering.ClipPlan(
        composition=comp.Composition(
            spine=(comp.Segment(source_path, 0.0, 10.0),), style=style
        ),
        headline="headline",
        subtitle_source="stub",
        companion_path=None,
    )
