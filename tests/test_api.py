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


class TestCapabilities:
    """§7.2 said for three stages that the editor does not offer what this
    build cannot do. There was no route to ask, so it offered everything."""

    def test_the_editor_can_ask_what_this_build_animates(self, client, monkeypatch):
        from app.api.routers import system

        monkeypatch.setattr(system.montage, "capabilities", lambda: {
            "ok": True, "build": "6.1.1", "ffmpeg": "/usr/bin/ffmpeg", "detail": "",
            "animatable": {"x": True, "y": True, "width": True,
                           "height": True, "rotate": False, "opacity": True},
            "capabilities": {},
        })

        body = client.get("/api/capabilities").json()

        assert body["animatable"]["rotate"] is False
        assert body["build"] == "6.1.1"

    def test_it_goes_through_the_client_so_it_answers_for_the_right_ffmpeg(
        self, client, monkeypatch
    ):
        """Pointed at a montage service, the build that matters is that
        container's, not the API's. Asking `montage.client` is what makes the
        answer follow the montage rather than the process serving the page."""
        from app.api.routers import system

        asked: list[str] = []

        def through():
            asked.append("client")
            return {"ok": True, "animatable": {}, "capabilities": {}}

        monkeypatch.setattr(system.montage, "capabilities", through)
        client.get("/api/capabilities")

        assert asked == ["client"]


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


class TestScenarios:
    """What a job can be rendered with. Read-only here: the editing half
    arrives with the editor (§12, этап 4)."""

    def test_the_four_built_ins_are_there_after_startup(self, client):
        """Seeded by the lifespan, so a fresh install has something to render
        with before anybody has opened an editor."""
        body = client.get("/api/scenarios").json()

        assert {row["name"] for row in body} == {"talking", "plain", "split", "film"}
        assert all(row["builtin"] for row in body)

    def test_one_comes_back_as_an_object_not_a_string(self, client):
        """The column holds text because that is what a column is. A client
        that has to parse a string out of the response has been handed the
        storage format instead of the value."""
        first = client.get("/api/scenarios").json()[0]

        body = client.get(f"/api/scenarios/{first['id']}").json()

        assert isinstance(body["data"], dict)
        assert body["data"]["tracks"], "a scenario has tracks"

    def test_an_unknown_one_is_404(self, client):
        assert client.get("/api/scenarios/999").status_code == 404

    def test_a_job_can_name_the_scenario_and_the_cutter(self, client):
        """The two halves a profile used to say at once, said separately."""
        scenario_id = next(
            row["id"] for row in client.get("/api/scenarios").json() if row["name"] == "film"
        )

        body = client.post(
            "/api/jobs",
            json={
                "source_ref": "https://youtu.be/abc",
                "scenario_id": scenario_id,
                "cutter": "scenes",
                "start_immediately": False,
            },
        ).json()

        assert body["scenario_id"] == scenario_id
        assert body["cutter"] == "scenes"

    def test_a_missing_scenario_is_a_404_rather_than_a_silent_default(self, client):
        response = client.post(
            "/api/jobs",
            json={
                "source_ref": "https://youtu.be/abc",
                "scenario_id": 999,
                "start_immediately": False,
            },
        )

        assert response.status_code == 404

    def test_an_unknown_cutter_is_refused(self, client):
        response = client.post(
            "/api/jobs",
            json={
                "source_ref": "https://youtu.be/abc",
                "cutter": "vibes",
                "start_immediately": False,
            },
        )

        assert response.status_code == 422

    def test_a_profile_is_still_accepted_and_still_means_both(self, client):
        """Deprecated for one release. A client that has not been updated yet
        gets exactly what it got before: the cutter that profile named, and
        the built-in montage of the same name at render time."""
        body = client.post(
            "/api/jobs",
            json={
                "source_ref": "https://youtu.be/abc",
                "profile": "film",
                "start_immediately": False,
            },
        ).json()

        assert body["profile"] == "film"
        assert body["cutter"] == "scenes"
        assert body["scenario_id"] is None

    def test_a_job_with_neither_still_has_a_cutter(self, client):
        """Nothing has to be chosen for a job to run — the point of the
        defaults, and the reason an unattended pipeline works at all."""
        body = client.post(
            "/api/jobs", json={"source_ref": "https://youtu.be/abc", "start_immediately": False},
        ).json()

        assert body["cutter"] == "speech"
        assert body["profile"] == "talking"


class TestScenarioEditing:
    """The endpoints the editor is built on: save one, take it apart, try it.

    `inspect` is the one that matters. It answers "what does this scenario do
    on a clip of that length" by compiling it, so what the editor draws is
    what the renderer will do rather than a second opinion about it.
    """

    def a_scenario(self, name: str = "Мой", **kwargs) -> dict:
        from montage.scenario import model, store
        from montage.style import StyleSpec

        spine = model.Track(id="spine", kind=model.TRACK_SPINE, elements=(
            model.Element(
                id="intro", slot=model.Slot(kind=model.SLOT_SOURCE),
                duration=model.Duration(mode=model.DurationMode.FIXED, value=20.0),
                label="интро",
            ),
            model.Element(
                id="source", slot=model.Slot(kind=model.SLOT_SOURCE),
                duration=model.Duration(mode=model.DurationMode.ELASTIC, grow=1.0),
            ),
        ))
        return store.to_dict(model.Scenario(
            name=name, tracks=(spine,), style=StyleSpec.from_settings(), **kwargs
        ))

    def test_saving_one_and_reading_it_back(self, client):
        created = client.post(
            "/api/scenarios",
            json={"name": "Мой", "description": "для тестов", "data": self.a_scenario()},
        )

        assert created.status_code == 201
        body = client.get(f"/api/scenarios/{created.json()['id']}").json()
        assert body["name"] == "Мой"
        assert body["builtin"] is False
        assert [track["id"] for track in body["data"]["tracks"]] == ["spine"]

    def test_a_scenario_that_cannot_be_read_back_is_refused(self, client):
        """Refusing here costs one request. Storing it costs every render that
        points at it afterwards."""
        response = client.post(
            "/api/scenarios", json={"name": "Битый", "data": {"tracks": ["nope"]}},
        )

        assert response.status_code == 422

    def test_editing_a_built_in_answers_with_the_copy(self, client):
        """Not a refusal: the operator asked to change something, and "no"
        would leave them without what they asked for. The response carries the
        id that was written, which is not the one in the URL."""
        talking = next(
            row for row in client.get("/api/scenarios").json() if row["name"] == "talking"
        )

        saved = client.put(
            f"/api/scenarios/{talking['id']}", json={"data": self.a_scenario("talking")},
        ).json()

        assert saved["id"] != talking["id"]
        assert saved["builtin"] is False
        assert client.get(f"/api/scenarios/{talking['id']}").json()["data"] != saved["data"]

    def test_editing_your_own_edits_it(self, client):
        mine = client.post(
            "/api/scenarios", json={"name": "Мой", "data": self.a_scenario()}
        ).json()

        saved = client.put(
            f"/api/scenarios/{mine['id']}",
            json={"data": self.a_scenario(), "name": "Переименован", "description": "x"},
        ).json()

        assert saved["id"] == mine["id"]
        assert saved["name"] == "Переименован"
        assert saved["description"] == "x"

    def test_a_stale_save_is_refused_rather_than_winning(self, client):
        """Two tabs on one montage: the second one to press save must be told
        that it is second, not quietly overwrite the first."""
        mine = client.post(
            "/api/scenarios", json={"name": "Мой", "data": self.a_scenario()}
        ).json()
        assert mine["version"] == 1

        first = client.put(
            f"/api/scenarios/{mine['id']}",
            json={"data": self.a_scenario("первая"), "version": 1},
        )
        second = client.put(
            f"/api/scenarios/{mine['id']}",
            json={"data": self.a_scenario("вторая"), "version": 1},
        )

        assert first.status_code == 200
        assert first.json()["version"] == 2
        assert second.status_code == 409
        assert "saved since you opened it" in second.json()["detail"]

    def test_a_built_in_cannot_be_deleted(self, client):
        talking = next(
            row for row in client.get("/api/scenarios").json() if row["name"] == "talking"
        )

        assert client.delete(f"/api/scenarios/{talking['id']}").status_code == 409

    def test_your_own_can(self, client):
        mine = client.post(
            "/api/scenarios", json={"name": "Мой", "data": self.a_scenario()}
        ).json()

        assert client.delete(f"/api/scenarios/{mine['id']}").status_code == 204
        assert client.get(f"/api/scenarios/{mine['id']}").status_code == 404

    def test_inspecting_one_at_two_lengths_shows_it_behaving_differently(self, client):
        """The layout switcher, end to end: the elastic middle takes what the
        fixed intro leaves, and that is a different number on a short clip."""
        mine = client.post(
            "/api/scenarios", json={"name": "Мой", "data": self.a_scenario()}
        ).json()

        short = client.get(f"/api/scenarios/{mine['id']}/inspect?duration_sec=30").json()
        long = client.get(f"/api/scenarios/{mine['id']}/inspect?duration_sec=180").json()

        def source(body):
            return next(b for b in body["blocks"] if b["element_id"] == "source")

        assert source(short)["duration_sec"] == 10.0
        assert source(long)["duration_sec"] == 160.0
        assert short["durations"] == [30.0, 60.0, 90.0, 120.0, 180.0]

    def test_inspect_reports_what_did_not_fit(self, client):
        """A fixed 20-second intro on a 10-second clip: the intro is cut in
        half and the material itself gets nothing. Invisible in a rendered
        file until somebody watches it; two numbers and a warning here."""
        mine = client.post(
            "/api/scenarios", json={"name": "Мой", "data": self.a_scenario()}
        ).json()

        body = client.get(f"/api/scenarios/{mine['id']}/inspect?duration_sec=10").json()

        blocks = {block["element_id"]: block for block in body["blocks"]}
        assert blocks["intro"]["duration_sec"] == 10.0   # asked for 20
        assert blocks["source"]["placed"] is False
        assert blocks["source"]["note"]
        assert any(w["code"] == "truncated" for w in body["warnings"])

    def test_inspect_falls_back_to_the_scenarios_own_mock_length(self, client):
        mine = client.post(
            "/api/scenarios", json={"name": "Мой", "data": self.a_scenario()}
        ).json()

        body = client.get(f"/api/scenarios/{mine['id']}/inspect").json()

        assert body["material_sec"] == 90.0

    def test_a_draft_is_inspected_without_being_saved(self, client):
        """What the editor asks on every edit. Saving first would make an
        editor a thing that writes to the database on every keystroke, and an
        unsaved draft is exactly the state somebody needs to see laid out
        before deciding whether to keep it."""
        response = client.post(
            "/api/scenarios/inspect",
            json={"data": self.a_scenario("Черновик"), "duration_sec": 60},
        )

        body = response.json()
        assert response.status_code == 200
        assert body["scenario_id"] == 0
        assert body["name"] == "Черновик"
        assert next(
            b for b in body["blocks"] if b["element_id"] == "source"
        )["duration_sec"] == 40.0
        assert client.get("/api/scenarios").json() == [
            row for row in client.get("/api/scenarios").json() if row["builtin"]
        ], "nothing was stored"

    def test_a_draft_that_cannot_be_read_is_refused(self, client):
        response = client.post(
            "/api/scenarios/inspect", json={"data": {"tracks": ["nope"]}},
        )

        assert response.status_code == 422

    def test_inspect_answers_for_the_moment_it_was_asked_about(self, client):
        """Once something moves, "where is this" has no answer without "when".
        The playhead is that when, and it comes back with the answer so a
        canvas cannot draw one moment while believing it is another."""
        data = self.a_scenario()
        data["tracks"].append({
            "id": "over",
            "kind": "overlay",
            "z": 2,
            "elements": [{
                "id": "mover",
                # The source itself rather than the library: this test is
                # about where a moving overlay is at a moment, and an empty
                # library would leave it out before the question is asked.
                "slot": {"kind": "source"},
                "start": {"mode": "start", "value": 0},
                "duration": {"mode": "fixed", "value": 10},
                "frame": {"x": {"static": 0, "keys": [
                    {"at": {"mode": "start", "value": 0}, "value": 0},
                    {"at": {"mode": "start", "value": 10}, "value": 100},
                ]}},
            }],
        })

        def where(at: float) -> dict:
            body = client.post(
                "/api/scenarios/inspect",
                json={"data": data, "duration_sec": 60, "at_sec": at},
            ).json()
            return next(b for b in body["blocks"] if b["element_id"] == "mover")

        assert where(0.0)["frame"]["x"] == 0.0
        assert where(5.0)["frame"]["x"] == 50.0
        assert where(5.0)["frame"]["moving"] is True

    def test_inspect_carries_the_fade_out_to_the_canvas(self, client):
        """Over the wire as well as in the compile: the schema strips a field
        it has not been told about, so a fade that reached the report and not
        the response would leave the canvas drawing solid blocks."""
        data = self.a_scenario()
        data["tracks"].append({
            "id": "over",
            "kind": "overlay",
            "z": 2,
            "elements": [{
                "id": "fader",
                "slot": {"kind": "source"},
                "start": {"mode": "start", "value": 0},
                "duration": {"mode": "fixed", "value": 10},
                "frame": {"opacity": {"static": 1, "keys": [
                    {"at": {"mode": "start", "value": 0}, "value": 0},
                    {"at": {"mode": "start", "value": 10}, "value": 1},
                ]}},
            }],
        })

        def opacity(at: float) -> float:
            body = client.post(
                "/api/scenarios/inspect",
                json={"data": data, "duration_sec": 60, "at_sec": at},
            ).json()
            block = next(b for b in body["blocks"] if b["element_id"] == "fader")
            return block["frame"]["opacity"]

        assert opacity(0.0) == 0.0
        assert opacity(5.0) == 0.5

    def test_a_key_on_any_property_is_answered_rather_than_500(self, client):
        """The editor writes width and rotate keys from its own presets, and
        the report used to look the property up in a dict of "x" and "y" —
        so a preset answered the screen that drew it with a 500 (trap 53)."""
        for name, value in (("width", 40), ("height", 25), ("rotate", 15),
                            ("opacity", 0.5), ("x", 20), ("y", 30)):
            data = self.a_scenario()
            data["tracks"].append({
                "id": "over",
                "kind": "overlay",
                "z": 2,
                "elements": [{
                    "id": "keyed",
                    "slot": {"kind": "source"},
                    "start": {"mode": "start", "value": 0},
                    "duration": {"mode": "fixed", "value": 10},
                    "frame": {name: {"static": value, "keys": [
                        {"at": {"mode": "start", "value": 0}, "value": value},
                        {"at": {"mode": "start", "value": 5}, "value": value / 2},
                    ]}},
                }],
            })

            response = client.post(
                "/api/scenarios/inspect",
                json={"data": data, "duration_sec": 60, "at_sec": 2.5},
            )

            assert response.status_code == 200, f"{name}: {response.text[:200]}"
            block = next(b for b in response.json()["blocks"]
                         if b["element_id"] == "keyed")
            assert [key["property"] for key in block["keys"]] == [name, name]

    def test_inspecting_an_unknown_scenario_is_404(self, client):
        assert client.get("/api/scenarios/999/inspect").status_code == 404

    def test_trying_a_scenario_on_a_real_clip(self, client, db, monkeypatch):
        """«Примерить»: composed by the code the real render uses, and the
        scenario that reaches it is the one that was asked for."""
        from app.api.routers import scenarios as scenarios_router
        from app.domain.cutting import SliceSpec
        from app.services import clip_jobs, clips
        import os

        source = os.path.join(os.environ["APP_VIDEOS_DIR"], "source.mp4")
        with open(source, "wb") as handle:
            handle.write(b"\x00" * 4096)
        job = clip_jobs.create(
            db, source_ref=source, source_platform="local", start_immediately=False
        )
        job.original_path = source
        db.add(job)
        db.flush()
        clip = clips.plan(db, job.id, [SliceSpec(1, 0.0, 60.0, "headline", "words", 3)])[0]
        db.commit()

        mine = client.post(
            "/api/scenarios", json={"name": "Мой", "data": self.a_scenario()}
        ).json()

        seen = {}

        def fake_compose(**kwargs):
            seen.update(kwargs)
            return _plan_stub(kwargs["style"], source)

        def fake_preview(composition, output_path, *, spec=None):
            seen["spec"] = spec
            with open(output_path, "wb") as handle:
                handle.write(b"\x00" * 32)

        monkeypatch.setattr(scenarios_router.rendering, "compose_clip", fake_compose)
        monkeypatch.setattr(scenarios_router.rendering.montage, "preview", fake_preview)

        response = client.post(
            f"/api/scenarios/{mine['id']}/preview",
            json={"clip_id": clip.id, "at_sec": 1.0, "duration_sec": 3.0},
        )

        assert response.status_code == 200
        assert response.headers["content-type"] == "video/mp4"
        assert seen["scenario"].name == "Мой"
        assert seen["spec"].duration_sec == 3.0

    def test_a_draft_is_previewed_without_being_saved(self, client, db, monkeypatch):
        """What is on screen rather than what was last written down, which is
        what somebody pressing the button means."""
        from app.api.routers import scenarios as scenarios_router
        from app.domain.cutting import SliceSpec
        from app.services import clip_jobs, clips
        import os

        source = os.path.join(os.environ["APP_VIDEOS_DIR"], "source.mp4")
        with open(source, "wb") as handle:
            handle.write(b"\x00" * 4096)
        job = clip_jobs.create(
            db, source_ref=source, source_platform="local", start_immediately=False
        )
        job.original_path = source
        db.add(job)
        db.flush()
        clip = clips.plan(db, job.id, [SliceSpec(1, 0.0, 60.0, "headline", "words", 3)])[0]
        db.commit()
        mine = client.post(
            "/api/scenarios", json={"name": "Сохранённый", "data": self.a_scenario()}
        ).json()

        seen = {}
        monkeypatch.setattr(
            scenarios_router.rendering, "compose_clip",
            lambda **kwargs: (seen.update(kwargs) or _plan_stub(kwargs["style"], source)),
        )
        monkeypatch.setattr(
            scenarios_router.rendering.montage, "preview",
            lambda composition, output_path, *, spec=None: open(output_path, "wb").write(b"\x00"),
        )

        client.post(
            f"/api/scenarios/{mine['id']}/preview",
            json={"clip_id": clip.id, "data": self.a_scenario("Черновик")},
        )

        assert seen["scenario"].name == "Черновик"
        assert client.get(f"/api/scenarios/{mine['id']}").json()["name"] == "Сохранённый"


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

        monkeypatch.setattr(clips_router.rendering.montage, "preview", fake_preview)
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

    def test_a_preview_uses_the_scenario_the_job_names(self, client, clip, db, monkeypatch):
        """A preview is worth having because it is composed by the code the
        real render uses — which includes reading the same scenario. One built
        from the profile's built-in while the job renders from something else
        is a preview of a video nobody is going to get.
        """
        from app.api.routers import clips as clips_router
        from app.db.models import ClipJob
        from app.services import scenarios
        from montage.scenario import builtin, store
        from montage.style import StyleSpec

        stored = scenarios.create(
            db, name="Мой", data=store.to_dict(builtin.plain(StyleSpec.from_settings())),
        )
        job = db.get(ClipJob, clip.job_id)
        job.scenario_id = stored.id
        db.add(job)
        db.commit()

        seen = {}

        def fake_compose(**kwargs):
            seen.update(kwargs)
            return _plan_stub(kwargs["style"], str(clip.video_path))

        def fake_preview(composition, output_path, *, spec=None):
            with open(output_path, "wb") as handle:
                handle.write(b"\x00" * 32)

        monkeypatch.setattr(clips_router.rendering.montage, "preview", fake_preview)
        monkeypatch.setattr(clips_router.rendering, "compose_clip", fake_compose)

        response = client.post(f"/api/clips/{clip.id}/preview", json={"at_sec": 1.0})

        assert response.status_code == 200
        assert seen["scenario"].name == "Мой"

    def test_the_preview_endpoint_calls_the_renderer_for_real(self, client, clip, monkeypatch):
        """Patched one level below the seam, so the call that crosses it is the
        real one. Every other preview test replaces `montage.preview` itself,
        and that is how a signature can be wrong for a fortnight: the endpoint
        would raise `TypeError` in production and pass here."""
        from montage.render import compiler as render_compiler

        seen = {}

        def fake_render_preview(composition, output_path, *, spec=None):
            seen.update(spec=spec, segments=len(composition.spine))
            with open(output_path, "wb") as handle:
                handle.write(b"\x00" * 32)
            return None

        monkeypatch.setattr(render_compiler, "render_preview", fake_render_preview)

        response = client.post(
            f"/api/clips/{clip.id}/preview", json={"at_sec": 0.0, "duration_sec": 2.0},
        )

        assert response.status_code == 200
        assert seen["spec"].duration_sec == 2.0
        assert seen["segments"] >= 1

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

        monkeypatch.setattr(clips_router.rendering.montage, "preview", fake_preview)
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
