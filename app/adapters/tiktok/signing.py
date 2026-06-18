"""Request signing and payload helpers for TikTok's private web API.

TikTok signs publish requests in browser JavaScript (X-Bogus / _signature).
There is no Python equivalent, so `sign_url` shells out to Node running the
bundled signer — that subprocess is the reason Node is installed in the image.

The previous version of this module carried each helper twice, under both
`snake_case` and `camelCase` names, plus a `getTagsExtra` that nothing called.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import string
import subprocess
import zlib
from pathlib import Path

import requests

log = logging.getLogger(__name__)

SIGNER_JS = Path(__file__).parent / "signature" / "browser.js"


def sign_url(url: str, user_agent: str, verify_fp: str | None = None) -> str | None:
    """Run the bundled Node signer. Returns its raw JSON output, or None.

    `verify_fp` must be the account's stable `s_v_web_id` cookie: the signature
    is computed over it, so a mismatch between the signed query and the cookie
    sent with the request gets the publish rejected.
    """
    node = _node_executable()
    command = [node, str(SIGNER_JS), url, user_agent]
    if verify_fp:
        command.append(verify_fp)

    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        log.error("Node.js not found (%s); install Node or put it on PATH", node)
        return None
    except subprocess.TimeoutExpired:
        log.error("signature generation timed out")
        return None

    if proc.returncode != 0:
        log.error("signature generation failed: %s", (proc.stderr or "").strip()[:500])
        return None
    return proc.stdout


def _node_executable() -> str:
    found = shutil.which("node")
    if found:
        return found
    windows_default = r"C:\Program Files\nodejs\node.exe"
    return windows_default if os.path.exists(windows_default) else "node"


def random_string(length: int, allow_underscore: bool = True) -> str:
    alphabet = string.ascii_letters + string.digits + ("_" if allow_underscore else "")
    return "".join(secrets.choice(alphabet) for _ in range(length))


def crc32(content: bytes) -> str:
    """Lowercase 8-digit CRC32, the form TikTok's chunk upload expects."""
    return ("%X" % (zlib.crc32(content, 0) & 0xFFFFFFFF)).lower().zfill(8)


def check_response(url: str, response) -> bool:
    """Log and report whether a call succeeded."""
    if response.status_code != 200:
        log.error(
            "TikTok rejected %s: HTTP %s %s",
            url,
            response.status_code,
            (response.text or "")[:300],
        )
        return False
    return True


def convert_tags(text: str, session, user_agent: str) -> tuple[str, list[dict]]:
    """Build TikTok's `text_extra` ranges for the #tags and @mentions in a caption.

    Returns the caption with markup and the ranges themselves. Only the ranges
    are actually sent — the plain caption goes out as `markup_text`, which
    TikTok accepts — but the offsets have to be computed over the same walk.
    """
    text_extra: list[dict] = []
    offset = 0
    index = -1

    def block(start: int, end: int, kind: int, hashtag: str, user_id: str, tag_id: str) -> dict:
        return {
            "start": start,
            "end": end,
            "type": kind,
            "hashtag_name": hashtag,
            "user_id": user_id,
            "tag_id": tag_id,
        }

    def convert(match: re.Match) -> str:
        nonlocal index, offset
        index += 1
        hashtag, mention, plain = match.group(1), match.group(2), match.group(3)

        if hashtag:
            text_extra.append(block(offset, offset + len(hashtag) + 1, 1, hashtag, "", str(index)))
            offset += len(hashtag) + 1
            return f'<h id="{index}">#{hashtag}</h>'

        if mention:
            text_extra.append(
                block(offset, offset + len(mention) + 1, 0, "", _lookup_user_id(session, mention, user_agent), str(index))
            )
            offset += len(mention) + 1
            return f'<m id="{index}">@{mention}</m>'

        offset += len(plain)
        return plain

    markup = re.sub(r"#(\w+)|@([\w.-]+)|([^#@]+)", convert, text)
    return markup, text_extra


def _lookup_user_id(session, username: str, user_agent: str) -> str:
    """Resolve a @mention to a numeric user id, scraped from their page.

    An unknown or private account, or a change to TikTok's page layout, yields
    an empty id: the mention then posts as plain text instead of failing the
    whole upload.
    """
    try:
        response = session.get(
            f"https://www.tiktok.com/@{username}",
            headers={"accept": "*/*", "user-agent": user_agent},
            timeout=15,
        )
        marker = 'webapp.user-detail":{"userInfo":{"user":{"id":"'
        return response.text.split(marker)[1].split('"')[0]
    except (requests.RequestException, IndexError):
        log.debug("could not resolve user id for @%s", username)
        return ""
