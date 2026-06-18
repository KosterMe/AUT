"""Publishing to TikTok through its private web API.

This is a reverse-engineered flow and the most fragile part of the system, so
the call sequence below is deliberately preserved as-is:

    project/create -> upload/auth -> ApplyUploadInner -> chunk transfer
    -> phase=finish -> CommitUploadInner -> sign -> project/post

What did change is everything around it: configuration comes from settings,
progress goes to the logger and to the task, and a rejected session raises
`SessionExpiredError` so the caller can flag the account for re-login instead
of retrying an upload that can never succeed.

Scheduling is ours, not TikTok's: there is no `schedule_time` here because our
queue decides when a video goes out, which proved far more reliable than
TikTok's server-side scheduling.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Callable

import requests
from requests_auth_aws_sigv4 import AWSSigV4

from app.adapters.tiktok import cookies as cookie_store
from app.adapters.tiktok import signing
from app.core.config import get_settings
from app.core.errors import PermanentError, SessionExpiredError

log = logging.getLogger(__name__)

MAX_CAPTION_CHARS = 2200
ProgressCallback = Callable[[str, float], None]


def upload_video(
    username: str,
    video_path: str,
    caption: str,
    *,
    allow_comment: int = 1,
    allow_duet: int = 0,
    allow_stitch: int = 0,
    visibility_type: int = 0,
    brand_organic_type: int = 0,
    branded_content_type: int = 0,
    ai_label: int = 0,
    proxy: str = "",
    on_progress: ProgressCallback | None = None,
) -> str:
    """Publish one video. Returns the TikTok video id.

    Raises `SessionExpiredError` when the account needs a new login, and
    `PermanentError` for input that can never succeed. Anything else (network,
    rate limits) propagates so the queue can retry it.
    """
    if len(caption) > MAX_CAPTION_CHARS:
        raise PermanentError(f"caption is longer than {MAX_CAPTION_CHARS} characters")

    video_file = _resolve_video_path(video_path)
    session, identity = _build_session(username, proxy=proxy)
    report = on_progress or (lambda stage, value: None)

    try:
        report("creating_project", 0.05)
        creation_id = signing.random_string(21)
        _create_project(session, creation_id)

        report("uploading_video", 0.15)
        upload = _upload_file(session, video_file, on_progress=report)

        report("finalizing_upload", 0.75)
        _finish_upload(upload, proxy=proxy)
        _commit_upload(session, upload)

        report("publishing", 0.9)
        video_id = _publish(
            session,
            identity=identity,
            upload=upload,
            creation_id=creation_id,
            caption=caption,
            options={
                "visibility_type": visibility_type,
                "allow_comment": allow_comment,
                "allow_duet": allow_duet,
                "allow_stitch": allow_stitch,
                "brand_organic_type": brand_organic_type,
                "branded_content_type": branded_content_type,
                "ai_label": ai_label,
            },
        )
        report("published", 1.0)
        return video_id
    finally:
        # TikTok rotates msToken/ttwid/csrf on most responses; persist the live
        # jar so the next upload starts from the freshest antibot state.
        try:
            updated = cookie_store.merge_from_jar(username, session.cookies)
            if updated:
                log.debug("refreshed %d session cookie(s) for '%s'", updated, username)
        except Exception as exc:  # pragma: no cover - never fail an upload on this
            log.warning("could not persist refreshed cookies: %s", exc)


class _Identity:
    """The browser fingerprint an account is pinned to."""

    __slots__ = ("username", "user_agent", "verify_fp", "ms_token", "datacenter")

    def __init__(
        self,
        username: str,
        user_agent: str,
        verify_fp: str | None,
        ms_token: str | None,
        datacenter: str,
    ):
        self.username = username
        self.user_agent = user_agent
        self.verify_fp = verify_fp
        self.ms_token = ms_token
        self.datacenter = datacenter


class _Upload:
    """Handles returned by TikTok's storage service for one in-flight file."""

    __slots__ = (
        "video_id",
        "session_key",
        "upload_id",
        "crcs",
        "host",
        "store_uri",
        "auth",
        "aws_auth",
    )

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


def _build_session(username: str, *, proxy: str = "") -> tuple[requests.Session, _Identity]:
    settings = get_settings()
    cookies = cookie_store.load(username)
    session_id = cookie_store.find_value(cookies, "sessionid")
    if not session_id:
        raise SessionExpiredError(f"no session saved for '{username}' — log the account in first")

    datacenter = cookie_store.find_value(cookies, "tt-target-idc")
    if not datacenter:
        log.warning("no datacenter cookie for '%s'; falling back to useast2a", username)
        datacenter = "useast2a"

    # Reuse the exact User-Agent captured at login: a per-upload random UA on a
    # single session is itself a strong automation signal.
    user_agent = cookie_store.load_user_agent(username) or settings.tiktok.default_user_agent

    session = requests.Session()
    # Load the whole saved jar — a request carrying dozens of cookies looks
    # like a browser session; one carrying two looks like a script.
    for cookie in cookies:
        try:
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ".tiktok.com"),
                path=cookie.get("path", "/"),
            )
        except Exception:
            continue
    session.cookies.set("sessionid", session_id, domain=".tiktok.com")
    session.cookies.set("tt-target-idc", datacenter, domain=".tiktok.com")
    session.headers.update(
        {"User-Agent": user_agent, "Accept": "application/json, text/plain, */*"}
    )
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    identity = _Identity(
        username=username,
        user_agent=user_agent,
        verify_fp=cookie_store.find_value(cookies, "s_v_web_id"),
        ms_token=cookie_store.find_value(cookies, "msToken"),
        datacenter=datacenter,
    )
    log.info("publishing as '%s' via datacenter %s", username, datacenter)
    return session, identity


def _create_project(session: requests.Session, creation_id: str) -> str:
    url = (
        "https://www.tiktok.com/api/v1/web/project/create/"
        f"?creation_id={creation_id}&type=1&aid=1988"
    )
    response = session.post(url)
    if not signing.check_response(url, response):
        raise RuntimeError(f"project/create failed with HTTP {response.status_code}")

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"project/create returned a non-JSON body: {response.text[:300]!r}"
        ) from exc

    if "project" not in payload:
        # TikTok answers HTTP 200 with an error body when the session is logged
        # out server-side. Reporting that as a session problem is what lets the
        # account be flagged instead of the upload being retried forever.
        raise SessionExpiredError(f"TikTok rejected the session: {str(payload)[:300]}")
    return str(payload["project"]["project_id"])


def _upload_file(
    session: requests.Session, video_path: str, *, on_progress: ProgressCallback
) -> _Upload:
    settings = get_settings()

    auth_url = "https://www.tiktok.com/api/v1/video/upload/auth/?aid=1988"
    response = session.get(auth_url)
    if not signing.check_response(auth_url, response):
        raise RuntimeError("could not get upload authorization")
    token = response.json()["video_token_v5"]
    aws_auth = AWSSigV4(
        "vod",
        region="ap-singapore-1",
        aws_access_key_id=token["access_key_id"],
        aws_secret_access_key=token["secret_acess_key"],  # TikTok's own spelling
        aws_session_token=token["session_token"],
    )

    file_size = os.path.getsize(video_path)
    if file_size == 0:
        raise PermanentError(f"video file is empty: {video_path}")

    apply_url = (
        "https://www.tiktok.com/top/v1?Action=ApplyUploadInner&Version=2020-11-19"
        f"&SpaceName=tiktok&FileType=video&IsInner=1&FileSize={file_size}&s=g158iqx8434"
    )
    response = session.get(apply_url, auth=aws_auth)
    if not signing.check_response(apply_url, response):
        raise RuntimeError("could not reserve an upload slot")

    node = response.json()["Result"]["InnerUploadAddress"]["UploadNodes"][0]
    store = node["StoreInfos"][0]
    upload = _Upload(
        video_id=node["Vid"],
        session_key=node["SessionKey"],
        upload_id=str(uuid.uuid4()),
        crcs=[],
        host=node["UploadHost"],
        store_uri=store["StoreUri"],
        auth=store["Auth"],
        aws_auth=aws_auth,
    )

    # Stream in chunks: reading a long source whole would hold roughly twice
    # its size in memory, and a worker container has a modest limit.
    chunk_size = settings.tiktok.upload_chunk_bytes
    total_chunks = max(1, (file_size + chunk_size - 1) // chunk_size)
    log.info("uploading %.1f MB in %d chunk(s)", file_size / 1024 / 1024, total_chunks)

    uploaded = 0
    with open(video_path, "rb") as handle:
        for part in range(1, total_chunks + 1):
            chunk = handle.read(chunk_size)
            checksum = signing.crc32(chunk)
            upload.crcs.append(checksum)
            chunk_url = (
                f"https://{upload.host}/{upload.store_uri}"
                f"?partNumber={part}&uploadID={upload.upload_id}&phase=transfer"
            )
            response = session.post(
                chunk_url,
                headers={
                    "Authorization": upload.auth,
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": 'attachment; filename="undefined"',
                    "Content-Crc32": checksum,
                },
                data=chunk,
            )
            if not signing.check_response(chunk_url, response):
                raise RuntimeError(f"chunk {part}/{total_chunks} failed")
            uploaded += len(chunk)
            # The transfer occupies 0.15..0.75 of the overall publish.
            on_progress("uploading_video", 0.15 + 0.6 * (uploaded / file_size))

    return upload


def _finish_upload(upload: _Upload, *, proxy: str) -> None:
    url = (
        f"https://{upload.host}/{upload.store_uri}"
        f"?uploadID={upload.upload_id}&phase=finish&uploadmode=part"
    )
    body = ",".join(f"{index + 1}:{crc}" for index, crc in enumerate(upload.crcs))
    # Deliberately not the cookie session: this endpoint authenticates with the
    # upload token alone.
    proxies = {"http": proxy, "https": proxy} if proxy else None
    response = requests.post(
        url,
        headers={"Authorization": upload.auth, "Content-Type": "text/plain;charset=UTF-8"},
        data=body,
        proxies=proxies,
        timeout=120,
    )
    if not signing.check_response(url, response):
        raise RuntimeError("finalizing the upload failed")


def _commit_upload(session: requests.Session, upload: _Upload) -> None:
    url = (
        "https://www.tiktok.com/top/v1?Action=CommitUploadInner"
        "&Version=2020-11-19&SpaceName=tiktok"
    )
    body = json.dumps({"SessionKey": upload.session_key, "Functions": [{"name": "GetMeta"}]})
    response = session.post(url, auth=upload.aws_auth, data=body)
    if not signing.check_response(url, response):
        raise RuntimeError("committing upload metadata failed")


def _publish(
    session: requests.Session,
    *,
    identity: _Identity,
    upload: _Upload,
    creation_id: str,
    caption: str,
    options: dict[str, int],
) -> str:
    # Touch the site once so the jar picks up a fresh csrf/ttwid pair.
    session.head("https://www.tiktok.com", headers={"user-agent": identity.user_agent})

    # Only the ranges are used: the plain caption is sent as markup_text, which
    # TikTok accepts, but the offsets must be computed over the same walk.
    _, text_extra = signing.convert_tags(caption, session, identity.user_agent)

    ms_token = _cookie_from_jar(session, "msToken") or identity.ms_token or ""
    if not ms_token:
        log.warning("no msToken available; TikTok will most likely reject the publish")

    sign_target = (
        "https://www.tiktok.com/api/v1/web/project/post/"
        f"?app_name=tiktok_web&channel=tiktok_web&device_platform=web&aid=1988&msToken={ms_token}"
    )
    raw = signing.sign_url(sign_target, identity.user_agent, identity.verify_fp)
    if raw is None:
        raise RuntimeError("could not generate the publish signature")
    try:
        signed = json.loads(raw)["data"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise RuntimeError(f"signature output was unusable: {raw[:200]!r}") from exc

    # Adopt the signer's fingerprint when the account had none, and keep the
    # cookie and the signed query agreeing about it.
    verify_fp = identity.verify_fp or signed.get("verify_fp")
    if verify_fp:
        session.cookies.set("s_v_web_id", verify_fp, domain=".tiktok.com")

    # Only the query string is reused. The signed URL's *path* is the signing
    # endpoint, not the publish endpoint — posting to it returns an empty body.
    # X-Bogus is already appended by the signer, so the query goes as-is.
    signed_query = signed["signed_url"].split("?", 1)[1]
    url = f"https://www.tiktok.com/tiktok/web/project/post/v1/?{signed_query}"

    payload = {
        "post_common_info": {
            "creation_id": creation_id,
            "enter_post_page_from": 1,
            "post_type": 3,
        },
        "feature_common_info_list": [
            {
                "geofencing_regions": [],
                "playlist_name": "",
                "playlist_id": "",
                "tcm_params": '{"commerce_toggle_info":{}}',
                "sound_exemption": 0,
                "anchors": [],
                "vedit_common_info": {"draft": "", "video_id": upload.video_id},
                "privacy_setting_info": {
                    "visibility_type": options["visibility_type"],
                    "allow_duet": options["allow_duet"],
                    "allow_stitch": options["allow_stitch"],
                    "allow_comment": options["allow_comment"],
                },
                "aigc_info": {"aigc_label_type": options["ai_label"]},
            }
        ],
        "single_post_req_list": [
            {
                "batch_index": 0,
                "video_id": upload.video_id,
                "is_long_video": 0,
                "single_post_feature_info": {
                    "text": caption,
                    "text_extra": text_extra,
                    "markup_text": caption,
                    "music_info": {},
                    "poster_delay": 0,
                },
            }
        ],
    }

    response = session.post(
        url,
        data=json.dumps(payload),
        headers={"content-type": "application/json", "user-agent": identity.user_agent},
    )
    if not signing.check_response(url, response):
        raise RuntimeError("publish request failed")

    try:
        result = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"publish returned a non-JSON body: {response.text[:300]!r}"
        ) from exc

    if result.get("status_code") == 0:
        log.info("published video %s", upload.video_id)
        return str(upload.video_id)

    # Surface TikTok's own wording ("You are posting too fast", region blocks)
    # rather than a generic failure.
    reason = result.get("status_msg") or f"status_code={result.get('status_code')}"
    raise RuntimeError(f"TikTok rejected the publish: {reason}")


def _resolve_video_path(video_path: str) -> str:
    """Accept an absolute path, or a filename inside the configured videos dir."""
    if os.path.isabs(video_path) and os.path.exists(video_path):
        return video_path
    candidate = os.path.join(str(get_settings().paths.videos_dir), video_path)
    if os.path.exists(candidate):
        return candidate
    if os.path.exists(video_path):
        return os.path.abspath(video_path)
    raise PermanentError(f"video file not found: {video_path}")


def _cookie_from_jar(session: requests.Session, name: str) -> str | None:
    """Last matching cookie wins.

    `jar.get()` raises CookieConflictError when one name exists on several
    domains, which TikTok does for msToken (`.tiktok.com` and `www.tiktok.com`).
    """
    value = None
    for cookie in session.cookies:
        if cookie.name == name:
            value = cookie.value
    return value
