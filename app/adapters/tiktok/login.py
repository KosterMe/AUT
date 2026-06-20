"""Capturing a TikTok session with a local browser.

The user signs in by hand — TikTok's login is behind a captcha and solving it
automatically is neither reliable nor something this project attempts. All
this does is open the page, wait for the session cookies to appear, and save
the jar together with the exact User-Agent the browser used.

Requires a desktop with Chrome. On a headless server, add accounts by
importing an exported cookie file instead (`app.services.login.import_cookies`).
"""
from __future__ import annotations

import logging
import threading
import time

from app.adapters.tiktok import cookies as cookie_store
from app.adapters.tiktok.browser import Browser
from app.core.config import get_settings

log = logging.getLogger(__name__)

# Chrome is a process-wide singleton that quits itself in a finally block, so
# two overlapping logins would tear down each other's browser.
_LOGIN_LOCK = threading.Lock()

DEFAULT_TIMEOUT_SECONDS = 300.0


def capture_session(username: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
    """Open TikTok's login page and save the session once the user signs in.

    Blocks until the cookies appear or `timeout` elapses. Always captures
    fresh cookies, even when a file already exists — an explicit re-login is
    usually the response to a session that expired server-side, and trusting
    the file on disk would defeat the point.
    """
    settings = get_settings()
    with _LOGIN_LOCK:
        browser = Browser.get()
        try:
            browser.driver.get(settings.tiktok.login_url)
            log.info("waiting for '%s' to sign in...", username)
            wait_for_session_cookies(browser.driver, timeout=timeout)

            # Persist the FULL jar (msToken, ttwid, s_v_web_id, csrf tokens…)
            # and the browser's own User-Agent, so uploads reuse this exact
            # identity instead of a bare sessionid from a random device.
            jar = browser.driver.get_cookies()
            try:
                user_agent = browser.driver.execute_script("return navigator.userAgent")
            except Exception:  # pragma: no cover - driver quirk
                user_agent = None

            cookie_store.save(username, jar)
            if user_agent:
                cookie_store.save_user_agent(username, user_agent)
        finally:
            browser.close()

    if not cookie_store.has_valid_session(username):
        raise RuntimeError("login finished but no sessionid cookie was saved")
    log.info("captured TikTok session for '%s'", username)


def wait_for_session_cookies(driver, *, poll_interval: float = 1.0, timeout: float | None = None) -> list[dict]:
    """Poll a Selenium driver until the session cookies exist.

    Both are required: `sessionid` authenticates, and `tt-target-idc` says
    which datacenter the session belongs to — publishing without it fails in
    ways that look like a random server error.
    """
    started = time.monotonic()
    collected: dict[str, dict] = {}

    while True:
        for cookie in driver.get_cookies():
            if cookie["name"] in cookie_store.REQUIRED_COOKIES:
                collected[cookie["name"]] = cookie
        if all(name in collected for name in cookie_store.REQUIRED_COOKIES):
            return [collected[name] for name in cookie_store.REQUIRED_COOKIES]
        if timeout is not None and (time.monotonic() - started) > timeout:
            raise TimeoutError(f"timed out after {timeout:.0f}s waiting for TikTok session cookies")
        time.sleep(poll_interval)
