"""The b-roll library: what goes in, and what the planner sees coming out."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ValidationError
from app.db.enums import AssetKind
from app.services import assets


@pytest.fixture()
def client(db):
    from app.api.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def upload(session, name="clip.mp4", data=b"pretend this is a video", tags="машина"):
    return assets.save_upload(session, filename=name, data=data, tags=tags)


# --- tags -------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("Машина, Дорога", "машина,дорога"),
    ("#машина #дорога", "машина,дорога"),          # a pasted hashtag list
    ("машина; дорога", "машина,дорога"),
    ("машина, машина", "машина"),                  # deduplicated
    ("  ", ""),
    (None, ""),
])
def test_tags_are_normalised(raw, expected):
    assert assets.normalize_tags(raw) == expected


# --- storage ----------------------------------------------------------------


def test_uploading_the_same_file_twice_merges_tags_instead_of_duplicating(db):
    """Two rows of identical footage would compete for the same keyword and
    halve the chance of either being picked."""
    first = upload(db, tags="машина")
    second = upload(db, name="renamed.mp4", tags="дорога")

    assert second.id == first.id
    assert set(second.tag_list) == {"машина", "дорога"}
    assert len(assets.list_all(db)) == 1
    assert len(os.listdir(assets.library_dir())) == 1


def test_each_file_type_is_recognised(db):
    still = upload(db, name="meme.png", data=b"png bytes here", tags="мем")
    video = upload(db, name="broll.mov", data=b"mov bytes here", tags="дорога")
    track = upload(db, name="bed.mp3", data=b"mp3 bytes here", tags="music")

    assert still.kind == AssetKind.IMAGE
    assert video.kind == AssetKind.VIDEO
    assert track.kind == AssetKind.AUDIO


def test_unsupported_file_types_are_refused(db):
    with pytest.raises(ValidationError, match="unsupported file type"):
        upload(db, name="notes.pdf", data=b"%PDF-1.4")


def test_an_empty_upload_is_refused(db):
    with pytest.raises(ValidationError, match="empty"):
        upload(db, data=b"")


def test_uploads_are_capped(db, configure):
    configure(AUTOCLIPS_INSERTS_MAX_UPLOAD_MB=1)

    with pytest.raises(ValidationError, match="over the"):
        upload(db, data=b"0" * (2 * 1024 * 1024))


def test_deleting_an_asset_takes_its_file_with_it(db):
    asset = upload(db)
    path = asset.path
    db.commit()

    assets.delete(db, asset.id)
    db.commit()

    assert not os.path.exists(path)
    assert assets.list_all(db) == []


# --- what the planner sees --------------------------------------------------


def test_the_library_is_offered_least_recently_used_first(db):
    old = upload(db, name="old.mp4", data=b"old video", tags="один")
    fresh = upload(db, name="fresh.mp4", data=b"fresh video", tags="два")
    never = upload(db, name="never.mp4", data=b"never used", tags="три")
    db.commit()

    assets.mark_used(db, [fresh.path])
    assets.mark_used(db, [old.path])
    db.commit()

    ranks = {option.asset_id: option.last_used_rank for option in assets.options_for_planner(db)}

    assert ranks[never.id] < ranks[fresh.id] < ranks[old.id]


def test_marking_use_counts_it(db):
    asset = upload(db)
    db.commit()

    assets.mark_used(db, [asset.path])
    assets.mark_used(db, [asset.path])
    db.commit()
    db.refresh(asset)

    assert asset.use_count == 2
    assert asset.last_used_at is not None


def test_an_asset_whose_file_vanished_is_skipped_not_fatal(db):
    """One deleted file should not stop a job from rendering."""
    present = upload(db, name="present.mp4", data=b"present", tags="есть")
    missing = upload(db, name="missing.mp4", data=b"missing", tags="нет")
    db.commit()
    os.remove(missing.path)

    options = assets.options_for_planner(db)

    assert [option.asset_id for option in options] == [present.id]


def test_stills_are_flagged_for_the_compiler(db):
    upload(db, name="meme.png", data=b"png bytes", tags="мем")
    db.commit()

    assert assets.options_for_planner(db)[0].still is True


def test_a_music_file_is_flagged_so_it_can_never_become_b_roll(db):
    """Nothing in the library is typed by tag alone: an mp3 tagged the same way
    as a video would otherwise be handed to the insert planner as a picture."""
    upload(db, name="bed.mp3", data=b"mp3 bytes", tags="music")
    db.commit()

    option = assets.options_for_planner(db)[0]
    assert option.audio is True
    assert option.still is False


def test_a_music_file_is_never_offered_as_a_split_screen_background(db):
    upload(db, name="bed.mp3", data=b"mp3 bytes", tags="background")
    db.commit()

    assert assets.companion_for(db, tag="background") is None


# --- http -------------------------------------------------------------------


class TestAssetsApi:
    def test_upload_list_retag_and_delete(self, client):
        created = client.post(
            "/api/assets",
            files={"file": ("broll.mp4", b"video bytes", "video/mp4")},
            data={"tags": "#Машина #дорога"},
        )
        assert created.status_code == 201
        body = created.json()
        assert body["tags"] == ["машина", "дорога"]
        assert body["kind"] == "video"

        listed = client.get("/api/assets").json()
        assert [item["id"] for item in listed] == [body["id"]]

        retagged = client.patch(f"/api/assets/{body['id']}", json={"tags": "самолёт"})
        assert retagged.json()["tags"] == ["самолёт"]

        assert client.get(f"/api/assets/{body['id']}/file").status_code == 200
        assert client.delete(f"/api/assets/{body['id']}").status_code == 204
        assert client.get("/api/assets").json() == []

    def test_an_unsupported_upload_is_refused_with_a_reason(self, client):
        response = client.post(
            "/api/assets",
            files={"file": ("notes.pdf", b"%PDF-1.4", "application/pdf")},
            data={"tags": ""},
        )

        # ValidationError maps to 422 everywhere in this API.
        assert response.status_code == 422
        assert "unsupported file type" in response.json()["detail"]

    def test_a_missing_asset_is_a_404(self, client):
        assert client.get("/api/assets/999/file").status_code == 404
        assert client.delete("/api/assets/999").status_code == 404
