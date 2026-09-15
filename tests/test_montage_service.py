"""The montage service's own door (§1в).

What is worth testing here is not that FastAPI routes work. It is the two
things the wire adds that an import never had — a path is now a request to
open a file on somebody else's disk, and the caller is now somebody — plus the
property the whole split rests on: **the same call, in-process and over the
wire, gives the same answer**, because there is one implementation underneath
both.
"""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from montage import client
from montage import composition as comp
from montage.service import wire
from montage.service.app import create_app
from montage.style import StyleSpec


@pytest.fixture()
def service(configure, tmp_path):
    configure(AUTOCLIPS_DIR=str(tmp_path / "media"))
    (tmp_path / "media").mkdir(parents=True, exist_ok=True)
    return TestClient(create_app())


def a_request(source_path: str) -> client.ClipRequest:
    return client.ClipRequest(
        source_path=source_path,
        start_sec=0.0,
        end_sec=30.0,
        style=StyleSpec.from_settings(),
        speech=(),
        fallback_text="подпись",
        title_text="Заголовок",
        library=client.Library(),
        seed=7,
        index=1,
    )


class TestThePathIsARequestToOpenAFile:
    """§2.4: files do not travel, paths inside a shared volume do — which
    makes an unchecked path "read me anything you can reach" with extra
    steps."""

    def test_a_path_outside_the_media_root_is_refused(self, service):
        response = service.post("/analyse", json={"source_path": "/etc/passwd"})

        assert response.status_code == 400
        assert "outside the media root" in response.json()["detail"]

    def test_a_path_that_climbs_out_with_dots_is_refused_too(self, service, tmp_path):
        response = service.post(
            "/analyse", json={"source_path": "../../etc/passwd"},
        )

        assert response.status_code == 400

    def test_a_relative_path_is_read_against_the_root(self, service, tmp_path):
        """Which is what lets one request mean the same file in two containers
        that mount the volume at different places."""
        media = tmp_path / "media"
        (media / "clip.mp4").write_bytes(b"\x00" * 64)

        response = service.post("/analyse", json={"source_path": "clip.mp4"})

        assert response.status_code == 200

    def test_a_composition_is_checked_by_every_path_in_it(self, service, tmp_path):
        """A composition is a list of paths with instructions attached, so it
        is the same request repeated — and the same refusal."""
        composition = comp.Composition(
            spine=(comp.Segment("/etc/shadow", 0.0, 10.0),),
        )

        response = service.post("/render", json={
            "composition": comp.to_dict(composition),
            "output_path": "out.mp4",
        })

        assert response.status_code == 400


class TestWhoMayAsk:
    """§2.6: the service is not meant to be reachable from outside at all, and
    this is the belt for the day somebody publishes its port by accident."""

    def test_with_no_token_configured_the_door_is_open(self, service):
        assert service.get("/health").status_code == 200

    def test_a_configured_token_is_required(self, configure, tmp_path):
        configure(AUTOCLIPS_DIR=str(tmp_path / "media"), MONTAGE_SERVICE_TOKEN="s3cret")
        door = TestClient(create_app())

        assert door.get("/health").status_code == 401
        assert door.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert door.get(
            "/health", headers={"Authorization": "Bearer s3cret"}
        ).status_code == 200


class TestTheAnswersAreTheSameOnesAUTWouldHaveGot:
    """The property the split rests on. Not "the endpoint returns 200" — that
    the value on the wire is the value the function returns."""

    def test_health_says_whether_it_can_do_its_job(self, service):
        from montage.render import probe

        body = service.get("/health").json()

        assert body["ffmpeg"] == client.renderer_available()
        assert body["media_root"] == probe.media_root()

    def test_capabilities_is_the_probe_verbatim(self, service, monkeypatch):
        from montage.service import app as service_app

        # The door calls the `_here` half — the one that cannot forward the
        # request back to itself — so that is the one to stand in for.
        monkeypatch.setattr(
            service_app.client, "capabilities_here", lambda: {"probe": "recorded"},
        )

        assert service.get("/capabilities").json() == {"probe": "recorded"}

    def test_compose_returns_the_plan_the_client_would_have_built(
        self, service, tmp_path, monkeypatch
    ):
        from montage.service import app as service_app

        source = tmp_path / "media" / "clip.mp4"
        source.write_bytes(b"\x00" * 64)
        plan = client.ClipPlan(
            composition=comp.Composition(spine=(comp.Segment(str(source), 0.0, 10.0),)),
            companion_path=None,
            notes=("нечего было положить",),
        )
        monkeypatch.setattr(service_app.client, "compose_here", lambda request: plan)

        body = service.post("/compose", json={
            "request": wire.clip_request_to_dict(a_request(str(source))),
        }).json()

        assert wire.clip_plan_from_dict(body).composition == plan.composition
        assert body["notes"] == ["нечего было положить"]

    def test_a_request_survives_the_round_trip_whole(self, tmp_path):
        """Every field, not a sample: a field that is written and not read
        comes back as its default, the render succeeds, and the clip is simply
        not what was asked for — the same failure `store` is guarded against."""
        from montage.scenario import builtin

        original = dataclasses.replace(
            a_request("/media/clip.mp4"),
            scenario=builtin.talking(StyleSpec.from_settings()),
            library=client.Library(
                options=[
                    wire.asset_from_dict({
                        "asset_id": 3, "path": "/media/b.mp4", "tags": ["broll"],
                        "duration_sec": 4.0, "still": False, "audio": False,
                        "last_used_rank": 2,
                    }),
                ],
                companion_path="/media/bg.mp4",
            ),
            speech=({"start": 1.0, "end": 2.0, "text": "раз"},),
        )

        back = wire.clip_request_from_dict(wire.clip_request_to_dict(original))

        assert back == original

    def test_a_render_result_survives_it_too(self):
        from montage.render.compiler import ClipRenderResult

        original = ClipRenderResult(
            output_path="/media/out.mp4", output_duration=61.5, segment_count=3,
            subtitles_path="/media/out.ass", subtitle_count=12,
            silence_removed_seconds=4.25, strategy="one_pass", fragments_reused=2,
            qa={"loudness": -14.0},
        )

        assert wire.render_result_from_dict(wire.render_result_to_dict(original)) == original

    def test_the_fragment_cache_answers_with_its_own_volume(self, service):
        directory, suffix = client.fragment_cache()

        body = service.get("/fragment-cache").json()

        assert body == {"directory": str(directory), "suffix": suffix}


class TestTheSwitch:
    """§1в's whole user interface: one setting. Nothing else changes — AUT
    imports the same client and calls the same functions."""

    def test_by_default_the_montage_runs_in_this_process(self):
        from montage.service import remote

        assert remote.configured() == ""
        assert client._elsewhere() is None

    def test_setting_a_url_sends_the_work_over_the_wire(self, configure, monkeypatch):
        configure(MONTAGE_SERVICE_URL="http://montage:8080/")
        from montage.service import remote

        seen = {}
        monkeypatch.setattr(
            remote, "_call",
            lambda method, path, **kwargs: seen.update(
                method=method, path=path, **kwargs
            ) or {"has_audio": True},
        )

        assert client.has_audio("/media/clip.mp4") is True
        assert (seen["method"], seen["path"]) == ("POST", "/has-audio")
        assert seen["json"] == {"source_path": "/media/clip.mp4"}

    def test_the_trailing_slash_does_not_double_up(self, configure):
        configure(MONTAGE_SERVICE_URL="http://montage:8080/")
        from montage.service import remote

        assert remote.configured() == "http://montage:8080"

    def test_every_call_that_can_travel_has_the_same_signature_on_both_sides(self):
        """Trap 45 across the wire, where it would be dearer: a signature that
        is wrong here is wrong only when somebody turns the switch on, which
        is a fortnight after the code was written and in production."""
        import inspect

        from montage.service import remote

        travelling = [
            name for name in dir(remote)
            if not name.startswith("_") and callable(getattr(remote, name))
            and hasattr(client, name)
        ]
        assert set(travelling) >= {
            "analyse", "capabilities", "compose", "cover", "fragment_cache",
            "has_audio", "preview", "render", "renderer_available",
        }

        differences = {
            name: (str(inspect.signature(getattr(client, name))),
                   str(inspect.signature(getattr(remote, name))))
            for name in travelling
            if _shape(inspect.signature(getattr(client, name)))
            != _shape(inspect.signature(getattr(remote, name)))
        }

        assert differences == {}


    def test_the_dispatcher_and_the_local_body_take_the_same_arguments(self):
        """The other pair that can drift. `render` grew `on_progress` once;
        if `render_here` had not, the callback would have been dropped in one
        of the two ways of calling and nobody would have noticed which."""
        import inspect

        # A callback cannot go through a request, so the wire's half does not
        # take one. That is the only difference allowed, and it is spelled out
        # here so a second one cannot arrive quietly.
        allowed = {"render": {"on_progress"}}

        differences = {}
        for name in dir(client):
            local = getattr(client, f"{name}_here", None)
            if name.startswith("_") or local is None or not callable(local):
                continue
            here = [n for n, _, _ in _shape(inspect.signature(local))]
            there = [
                n for n, _, _ in _shape(inspect.signature(getattr(client, name)))
                if n not in allowed.get(name, set())
            ]
            if here != there:
                differences[name] = (there, here)

        assert differences == {}
        assert any(hasattr(client, f"{n}_here") for n in ("compose", "render", "cover"))


def _shape(signature) -> list[tuple[str, str, bool]]:
    """A signature as names, kinds and whether each has a default.

    Annotations are deliberately ignored: the two sides write them against
    different imports — `comp.Composition` here, `Composition` there — and a
    test that failed on that would be a test about spelling.
    """
    return [
        (name, str(parameter.kind), parameter.default is not parameter.empty)
        for name, parameter in signature.parameters.items()
    ]


class TestBothWaysOfCallingAgree:
    """The acceptance criterion of §1в, and the only one that matters: the
    same request, answered in-process and through the door, gives the same
    value. Not "both return 200" — the same composition, segment for segment.

    The transport here is a `TestClient` rather than a socket, which keeps the
    test hermetic and still exercises every layer that can lose something: the
    encoder, the request model, the decoder, and the encoder again on the way
    back.
    """

    @pytest.fixture()
    def both(self, configure, tmp_path, monkeypatch):
        """A client that can be asked either way, and a source it can read."""
        media = tmp_path / "media"
        media.mkdir(parents=True, exist_ok=True)
        source = media / "clip.mp4"
        source.write_bytes(b"\x00" * 2048)
        configure(AUTOCLIPS_DIR=str(media))

        from montage.render import probe

        # ffprobe is not the thing under test, and a real one would make this
        # a test about the machine it runs on.
        monkeypatch.setattr(probe, "probe_media", lambda path: {
            "width": 1920, "height": 1080, "duration": 90.0,
        })
        monkeypatch.setattr(probe, "ffprobe_has_audio", lambda path: True)
        monkeypatch.setattr(
            probe, "montage_keep_segments",
            lambda path, **kwargs: [(0.0, 12.0), (14.0, 30.0)],
        )
        return str(source)

    def through_the_door(self, configure, monkeypatch):
        """Point the remote client at an app running in this process."""
        from montage.service import remote

        configure(MONTAGE_SERVICE_URL="http://montage")
        door = TestClient(create_app())

        def call(method, path, *, json=None, timeout=None):
            response = door.request(method, path, json=json)
            if response.status_code >= 400:
                raise remote.MontageServiceError(response.text)
            return response.json()

        monkeypatch.setattr(remote, "_call", call)

    def test_compose_gives_the_same_clip_both_ways(self, both, configure, monkeypatch):
        request = a_request(both)

        here = client.compose(request)
        self.through_the_door(configure, monkeypatch)
        there = client.compose(request)

        assert there.composition == here.composition
        assert there.notes == here.notes
        assert there.companion_path == here.companion_path
        assert here.composition.spine, "and it is not the same because both are empty"

    def test_render_gives_the_same_result_both_ways(self, both, configure, monkeypatch):
        from montage.render import compiler as render_compiler

        result = render_compiler.ClipRenderResult(
            output_path="/media/out.mp4", output_duration=26.0, segment_count=2,
            subtitles_path=None, subtitle_count=0, silence_removed_seconds=2.0,
            strategy="one_pass", fragments_reused=0, qa={"loudness": -14.0},
        )
        monkeypatch.setattr(render_compiler, "render", lambda *a, **k: result)
        composition = client.compose(a_request(both)).composition
        out = both.replace("clip.mp4", "out.mp4")

        here = client.render(composition, out, source_duration_sec=30.0)
        self.through_the_door(configure, monkeypatch)
        there = client.render(composition, out, source_duration_sec=30.0)

        assert there == here == result

    def test_a_refusal_on_the_far_side_arrives_as_one_exception(
        self, both, configure, monkeypatch
    ):
        """"It said no" and "it did not answer" need the same handling on this
        side — the clip is not rendered either way — so they are one type with
        the difference in the message."""
        from montage.service import remote

        self.through_the_door(configure, monkeypatch)

        with pytest.raises(remote.MontageServiceError):
            client.analyse("/etc/passwd")
