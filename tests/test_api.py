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
        tasks = client.get("/api/tasks").json()
        assert len(tasks) == 1
        assert tasks[0]["kind"] == "download"
        assert "job_id" in tasks[0]["payload"]

    def test_a_queued_task_can_be_cancelled(self, client):
        client.post(
            "/api/jobs", json={"source_ref": "https://youtu.be/abc", "start_immediately": True}
        )
        task_id = client.get("/api/tasks").json()[0]["id"]
        body = client.post(f"/api/tasks/{task_id}/cancel").json()
        assert body["status"] == "cancelled"
