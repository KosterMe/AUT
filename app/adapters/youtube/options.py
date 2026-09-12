"""yt-dlp options: auth, transport, and readable diagnostics when they fail.

YouTube's bot checks are the single most common reason a job fails, and the
raw yt-dlp errors say almost nothing useful. Most of this module exists to
turn "HTTP Error 403" into a sentence that names the actual fix.

The working setup at time of writing: an exported cookies file, the `mweb`
player client, and a bgutil PO-token provider — see .env.example.
"""
from __future__ import annotations

import logging
import os
import shutil
import socket
import threading
from pathlib import Path
from typing import Iterable

from app.core.config import get_settings

log = logging.getLogger(__name__)


def apply_ytdlp_auth_options(options: dict) -> dict:
    """Fill in auth, proxy, JS runtime and retry options from settings."""
    settings = get_settings().youtube

    _apply_proxy(options, settings)
    _apply_js_runtime(options, settings)
    _apply_ffmpeg_location(options, settings)
    _apply_youtube_extractor_args(options, settings)

    # googlevideo CDN edges intermittently time out; without explicit retries a
    # single connect timeout aborts an otherwise fine download.
    options.setdefault("retries", settings.retries)
    options.setdefault("fragment_retries", settings.retries)
    options.setdefault("socket_timeout", settings.socket_timeout)

    cookies_file = _clean(settings.cookies_file)
    if cookies_file:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(cookies_file)))
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"YTDLP_COOKIES_FILE points at a missing file: {path}. Export "
                "YouTube cookies in Netscape cookies.txt format, or unset the variable."
            )
        options["cookiefile"] = _writable_cookie_jar(path)
        return options

    browser_spec = _clean(settings.cookies_from_browser)
    if browser_spec:
        options["cookiesfrombrowser"] = _parse_browser_spec(browser_spec)

    return options


def _writable_cookie_jar(source: str) -> str:
    """Return a cookie file yt-dlp may write, kept in step with `source`.

    yt-dlp saves its jar back to `cookiefile` from `YoutubeDL.close()`, so
    handing it the configured export directly makes every download raise from
    the `with` block's *exit* — after the work itself finished, and replacing
    whatever the real error was with `OSError: Read-only file system`. Compose
    mounts the export read-only exactly so nothing rewrites it, which is what
    turned every YouTube download into that one unreadable failure.

    The copy is per worker thread. yt-dlp's save is a plain truncate-and-write,
    so two concurrent downloads sharing one jar could interleave into a corrupt
    file. Cookies YouTube refreshes still carry from one download to the next
    within a thread, and the user's export is never touched.
    """
    directory = get_settings().paths.cookies_dir
    jar = directory / f"ytdlp-jar.{os.getpid()}.{threading.get_ident()}.txt"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if not jar.exists() or jar.stat().st_mtime < os.path.getmtime(source):
            shutil.copyfile(source, jar)
    except OSError as exc:
        raise OSError(
            f"cannot prepare a writable copy of the cookie file at {jar}: {exc}. "
            "yt-dlp rewrites its cookie jar after every download, so this "
            "directory (APP_COOKIES_DIR) must be writable."
        ) from exc
    return str(jar)


def explain_ytdlp_auth_error(exc: BaseException) -> str:
    """Translate a yt-dlp failure into the action that actually fixes it.

    Matched against the whole exception chain rather than the outermost error
    alone. Anything raised while yt-dlp is unwinding — a cookie jar it cannot
    save, a temp file it cannot delete — becomes the exception the caller sees,
    and the sentence that says what actually stopped the download is several
    `__context__` links underneath it.
    """
    chain = _exception_chain(exc)
    text = "\n".join(str(item) for item in chain)

    if "Failed to decrypt with DPAPI" in text or "Could not copy Chrome cookie database" in text:
        return (
            "yt-dlp cannot decrypt Chrome/Edge cookies on this Windows profile. "
            "Use an exported cookies.txt instead: set YTDLP_COOKIES_FILE to its "
            "path and unset YTDLP_COOKIES_FROM_BROWSER."
        )
    if any(marker in text for marker in _UNRESOLVABLE_MARKERS):
        return (
            "YouTube could not be reached at all: the DNS lookup for its hostname "
            "failed, so no request was ever made. Cookies and PO tokens have "
            "nothing to do with it. Either this network blocks or poisons "
            "YouTube's DNS, or the container has no working resolver — compare "
            "`getent hosts www.youtube.com` with a hostname that is not blocked. "
            "Set YTDLP_PROXY to a proxy that can reach YouTube if the network is "
            "the cause."
        )
    if "HTTP Error 403" in text:
        provider = _clean(get_settings().youtube.pot_base_url) or "http://127.0.0.1:4416"
        return (
            "YouTube rejected the video data request (HTTP 403), which normally "
            "means the player client needs a PO token. Check that the bgutil "
            f"provider is running (it must answer on {provider}/ping) "
            "and that YTDLP_PLAYER_CLIENTS=mweb is set."
        )
    if "Requested format is not available" in text:
        return (
            "yt-dlp found no downloadable video/audio format. Usually the cookies "
            "file is missing, expired, or was exported from the wrong browser profile."
        )
    return _primary_message(chain)


# Every phrase the platforms use for "this hostname does not resolve".
_UNRESOLVABLE_MARKERS = (
    "No address associated with hostname",
    "Temporary failure in name resolution",
    "Name or service not known",
    "Failed to resolve",
    "getaddrinfo failed",
    "nodename nor servname provided",
)

# yt-dlp's own exception types, by name so this module need not import yt_dlp.
_YTDLP_ERROR_NAMES = frozenset(
    {"DownloadError", "ExtractorError", "UnsupportedError", "GeoRestrictedError",
     "PostProcessingError"}
)


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """The exception and everything it was raised from, outermost first."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _primary_message(chain: list[BaseException]) -> str:
    """The most explanatory message in a chain we have no specific advice for.

    yt-dlp's own errors carry the useful text; an OSError from unwinding does
    not. Prefer the first yt-dlp error, and fall back to what was raised.
    """
    for item in chain:
        if type(item).__name__ in _YTDLP_ERROR_NAMES:
            return str(item)
    return str(chain[0]) if chain else ""


def assert_downloadable_formats(info: dict, source_ref: str) -> None:
    """Fail early, and loudly, when YouTube returned only storyboards.

    This is the LOGIN_REQUIRED case in disguise: the extraction "succeeds" but
    every format is a thumbnail strip, and the download then fails much later
    with something unrelated-looking.
    """
    formats = info.get("formats") or []
    real = [f for f in formats if f.get("vcodec") != "none" or f.get("acodec") != "none"]
    if real:
        return

    title = info.get("title") or source_ref
    format_ids = ", ".join(str(f.get("format_id")) for f in formats if f.get("format_id")) or "none"
    raise RuntimeError(
        f"YouTube returned no downloadable video/audio formats for '{title}'. "
        f"Available formats: {format_ids} — sb0/sb1/sb2/sb3 are storyboard "
        "thumbnails, not video.\n"
        "Three different causes look identical here, so check them in this order:\n"
        f"1. Name resolution — {_resolution_diagnostics()}\n"
        f"2. JavaScript runtime — {_js_runtime_diagnostics()} Without one, "
        "YouTube's n-challenge cannot be solved and only storyboards come back, "
        "even with perfectly good cookies.\n"
        f"3. Cookies — {_cookie_diagnostics()}"
    )


def _resolution_diagnostics() -> str:
    """Whether the hosts a format list depends on resolve at all.

    Ranked first because it is the cause that hides best. A watch page can come
    back complete — title, duration, thumbnails — while the player API calls
    and the challenge-solver fetch that actually produce the format list never
    leave the machine, and what is left is a list of storyboards. That reads
    exactly like an expired cookie file, and the cookie report below will then
    supply a plausible-sounding reason for a file that is perfectly good.
    """
    hosts = ["www.youtube.com"]
    if "github" in _clean(get_settings().youtube.remote_components):
        # Where the n-challenge solver itself is fetched from.
        hosts.append("github.com")
    unresolved = [host for host in hosts if not _resolves(host)]
    if not unresolved:
        return "every host yt-dlp needs resolves, so this is probably not the cause."
    return (
        f"{_names(unresolved)} did NOT resolve. This is the most likely cause. "
        "Some networks answer nothing for exactly these names on port 53 while "
        "leaving every other lookup alone, so a general is-the-internet-up check "
        "will not show it: compare these against a hostname you know is not "
        "blocked. The fix is to resolve names somewhere the interception cannot "
        "read the query — docker-compose.yml runs a `resolver` service that "
        "forwards over TLS for exactly this reason."
    )


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
    except OSError:
        return False
    return True


def _js_runtime_diagnostics() -> str:
    node = _clean(get_settings().youtube.node_path) or _find_node()
    if node:
        return f"found at {node}, so this is probably not the cause."
    return (
        "NOT FOUND. Install Node.js, or set YTDLP_NODE_PATH to its executable. "
        "This is the most likely cause."
    )


def _apply_proxy(options: dict, settings) -> None:
    # An empty proxy setting means "explicitly none". Left unset, yt-dlp
    # silently inherits HTTP_PROXY/HTTPS_PROXY from the shell, which is a
    # reliable way to trip YouTube's bot checks from an unexpected exit IP.
    options["proxy"] = _clean(settings.proxy)


def _apply_js_runtime(options: dict, settings) -> None:
    """Point yt-dlp at Node for solving YouTube's n-challenge.

    Without a JavaScript runtime the challenge fails and YouTube returns only
    storyboard images — which looks exactly like an authentication problem and
    sends you hunting through cookie files for hours. yt-dlp's own default
    prefers Deno, which is not installed here, while Node always is: the
    TikTok request signer depends on it.
    """
    node_path = _clean(settings.node_path) or _find_node()
    if node_path and os.path.isfile(node_path):
        options["js_runtimes"] = {"node": {"path": node_path}}
    else:
        log.warning(
            "no JavaScript runtime found for yt-dlp; YouTube will return only "
            "storyboard images. Install Node.js or set YTDLP_NODE_PATH."
        )

    components = _clean(settings.remote_components)
    if components:
        # The challenge solver script itself, fetched at runtime. It needs the
        # JS runtime above to actually run.
        options["remote_components"] = {item.strip() for item in components.split(",") if item.strip()}


def _find_node() -> str:
    """Locate Node without configuration. Cached: this runs per download."""
    global _NODE_PATH
    if _NODE_PATH is None:
        found = shutil.which("node") or ""
        if not found:
            windows_default = r"C:\Program Files\nodejs\node.exe"
            found = windows_default if os.path.isfile(windows_default) else ""
        _NODE_PATH = found
        if found:
            log.debug("using %s as the yt-dlp JavaScript runtime", found)
    return _NODE_PATH


_NODE_PATH: str | None = None


def _apply_youtube_extractor_args(options: dict, settings) -> None:
    extractor_args = options.setdefault("extractor_args", {})
    youtube_args = extractor_args.setdefault("youtube", {})

    if settings.player_clients:
        youtube_args["player_client"] = list(settings.player_clients)
    if settings.po_token:
        youtube_args["po_token"] = list(settings.po_token)
    if _clean(settings.visitor_data):
        youtube_args["visitor_data"] = [_clean(settings.visitor_data)]
    if _clean(settings.data_sync_id):
        youtube_args["data_sync_id"] = [_clean(settings.data_sync_id)]

    # The bgutil plugin reads its own extractor-args namespace rather than
    # youtube's, and left alone it asks 127.0.0.1:4416 — which resolves to the
    # asking container, not the provider. The key is yt-dlp's, derived from the
    # provider class: youtubepot-<PROVIDER_KEY lowercased>.
    pot_base_url = _clean(settings.pot_base_url)
    if pot_base_url:
        extractor_args.setdefault("youtubepot-bgutilhttp", {})["base_url"] = [pot_base_url]


def _apply_ffmpeg_location(options: dict, settings) -> None:
    configured = _clean(settings.ffmpeg_location)
    if configured:
        options["ffmpeg_location"] = configured
        return
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return
    if bundled and os.path.isfile(bundled):
        options["ffmpeg_location"] = bundled


def _parse_browser_spec(value: str) -> tuple:
    """Parse `chrome`, `edge:Default`, etc. into yt-dlp's tuple form."""
    parts = [part.strip() or None for part in value.split(":", 3)]
    if not parts or not parts[0]:
        raise ValueError("YTDLP_COOKIES_FROM_BROWSER must start with a browser name")
    return tuple(parts)


def _cookie_diagnostics() -> str:
    """Report which auth cookies the configured file carries. Never logs values."""
    cookies_file = _clean(get_settings().youtube.cookies_file)
    if not cookies_file:
        return "No YTDLP_COOKIES_FILE is configured."

    path = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(cookies_file))))
    if not path.is_file():
        return f"Configured cookie file is missing: {path}."

    cookies, httponly_count = _read_cookie_names(path)
    youtube_names = {name for domain, name in cookies if "youtube.com" in domain}
    google_names = {name for domain, name in cookies if "google.com" in domain}
    missing = sorted(AUTH_MARKERS - youtube_names)

    report = (
        f"Cookie diagnostics: youtube.com={len(youtube_names)}, "
        f"google.com={len(google_names)}, "
        f"youtube auth markers={_names(sorted(youtube_names & AUTH_MARKERS))}, "
        f"missing={_names(missing)}. Cookie values were not logged."
    )

    # A lead, not a verdict. Nearly every YouTube auth cookie is httpOnly, so
    # an export carrying none of them is a common reason for LOGIN_REQUIRED —
    # but the #HttpOnly_ prefix is optional, and this sentence has already
    # been believed once about a cookie file that turned out to be fine.
    # Report what was observed and leave the conclusion to whoever reads it.
    if httponly_count == 0 and missing:
        report += (
            " No line carries the #HttpOnly_ prefix and the missing names are "
            "all httpOnly cookies, which would fit an exporter that skipped "
            "them — but an export that omits only the prefix looks identical "
            "here and works fine, so rule out the causes above first. If this "
            "is the cause, re-export with a tool that keeps httpOnly cookies "
            '(for example "Get cookies.txt LOCALLY") from a youtube.com tab '
            "that is signed in."
        )
    return report


AUTH_MARKERS = frozenset(
    {"LOGIN_INFO", "SID", "HSID", "SSID", "APISID", "SAPISID",
     "__Secure-1PSID", "__Secure-3PSID"}
)


def _read_cookie_names(path: Path) -> tuple[list[tuple[str, str]], int]:
    """Cookie (domain, name) pairs, plus how many were marked httpOnly."""
    rows: list[tuple[str, str]] = []
    httponly = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows, 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("# Netscape"):
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
            httponly += 1
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            rows.append((parts[0].lower(), parts[5]))
    return rows, httponly


def _names(values: Iterable[str]) -> str:
    listed = list(values)
    return ", ".join(listed) if listed else "none"


def _clean(value: str) -> str:
    return (value or "").strip().strip('"').strip("'").strip()
