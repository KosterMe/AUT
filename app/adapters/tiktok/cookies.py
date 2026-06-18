"""Where a TikTok session lives on disk.

One account = one pickled cookie jar plus the User-Agent captured at login,
named `tiktok_session-<username>.cookie` / `.ua` under the configured cookies
directory. This module owns that convention completely; nothing else builds
those paths by hand.

Two things here are load-bearing for staying signed in:

* the **whole** jar is kept, not just `sessionid` — a request carrying two
  cookies from a randomized device looks nothing like a browser session;
* the User-Agent is pinned to the one used at login, because rotating it on
  each upload is itself a strong automation signal.
"""
from __future__ import annotations

import logging
import os
import pickle
from pathlib import Path
from typing import Any, Iterable

from app.core.config import get_settings

log = logging.getLogger(__name__)

PREFIX = "tiktok_session-"
# Both are needed to publish: the session, and the datacenter it belongs to.
REQUIRED_COOKIES = ("sessionid", "tt-target-idc")


def cookies_dir() -> Path:
    path = get_settings().paths.cookies_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_name(username: str) -> str:
    return f"{PREFIX}{username}"


def cookie_path(username: str) -> Path:
    return cookies_dir() / f"{session_name(username)}.cookie"


def user_agent_path(username: str) -> Path:
    return cookies_dir() / f"{session_name(username)}.ua"


def exists(username: str) -> bool:
    return cookie_path(username).is_file()


def load(username: str) -> list[dict[str, Any]]:
    """Read a saved jar. Returns [] when there is nothing saved."""
    path = cookie_path(username)
    if not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            raw = pickle.load(handle)
    except (pickle.UnpicklingError, EOFError, OSError) as exc:
        log.warning("cookie file for '%s' is unreadable: %s", username, exc)
        return []

    cookies = []
    for cookie in raw:
        # Chrome refuses sameSite=None without Secure; normalize on read so an
        # old file still loads into a driver.
        if cookie.get("sameSite") == "None":
            cookie["sameSite"] = "Strict"
        cookies.append(cookie)
    return cookies


def save(username: str, cookies: Iterable[dict[str, Any]]) -> Path:
    path = cookie_path(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_pickle(path, list(cookies))
    log.info("saved TikTok session for '%s'", username)
    return path


def delete(username: str) -> None:
    for path in (cookie_path(username), user_agent_path(username)):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - filesystem edge case
            log.warning("could not delete %s: %s", path, exc)


def save_user_agent(username: str, user_agent: str) -> None:
    path = user_agent_path(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(user_agent or "", encoding="utf-8")


def load_user_agent(username: str) -> str | None:
    path = user_agent_path(username)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def has_valid_session(username: str) -> bool:
    """Whether the saved jar still carries a session id.

    A file-level check only: TikTok can invalidate a session server-side and
    the file will look fine until an upload is rejected.
    """
    return any(c.get("name") == "sessionid" and c.get("value") for c in load(username))


def find_value(cookies: list[dict[str, Any]], name: str) -> str | None:
    for cookie in cookies:
        if cookie.get("name") == name and cookie.get("value"):
            return str(cookie["value"])
    return None


def list_usernames() -> list[str]:
    """Accounts represented by a cookie file, whether or not the database
    knows about them. This is what makes an import-from-disk flow possible."""
    directory = cookies_dir()
    if not directory.is_dir():
        return []
    return sorted(
        name.stem[len(PREFIX):]
        for name in directory.glob(f"{PREFIX}*.cookie")
    )


def merge_from_jar(username: str, jar) -> int:
    """Fold a live requests jar back into the saved file.

    TikTok rotates short-lived antibot cookies (msToken, ttwid, tt_csrf_token)
    via Set-Cookie on most responses. Writing them back means the next upload
    starts from the freshest state rather than the one captured at login.

    Matching is by (name, domain, path): existing entries keep their other
    attributes and only take a new value. Returns how many changed.
    """
    saved = load(username)
    by_key = {(c.get("name"), c.get("domain", ""), c.get("path", "/")): c for c in saved}

    changed = 0
    for cookie in jar:
        key = (cookie.name, cookie.domain or "", cookie.path or "/")
        existing = by_key.get(key)
        if existing is not None:
            if existing.get("value") != cookie.value:
                existing["value"] = cookie.value
                if cookie.expires:
                    existing["expiry"] = cookie.expires
                changed += 1
            continue
        entry = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain or "",
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
        }
        if cookie.expires:
            entry["expiry"] = cookie.expires
        saved.append(entry)
        by_key[key] = entry
        changed += 1

    if changed:
        _atomic_pickle(cookie_path(username), saved)
    return changed


def _atomic_pickle(path: Path, payload: Any) -> None:
    """Write via a temp file so a crash mid-write cannot corrupt a session."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as handle:
        pickle.dump(payload, handle)
    os.replace(tmp, path)
