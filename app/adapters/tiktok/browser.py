"""A local Chrome instance for capturing a TikTok login.

Desktop-only: it needs a real browser on the machine, so on a headless server
accounts are added by importing a cookie file instead (see
`app.services.login`).

Chrome's major version has to be detected up front because
undetected-chromedriver downloads a matching driver binary, and a mismatch
fails with an unhelpful error.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading

import undetected_chromedriver as uc

log = logging.getLogger(__name__)


def _get_chrome_major_version() -> int:
    """Detect the major version of the system-installed Chrome/Chromium so
    undetected-chromedriver downloads the matching ChromeDriver binary."""
    chrome_path = _get_chrome_binary_path()
    if chrome_path:
        major = _get_major_version_from_install_dir(chrome_path)
        if major:
            return major

        major = _get_major_version_from_windows_registry()
        if major:
            return major

    for binary in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        try:
            out = subprocess.check_output([binary, "--version"], stderr=subprocess.DEVNULL).decode()
            major = _major_from_text(out)
            if major:
                return major
        except (FileNotFoundError, ValueError, IndexError):
            continue
    log.warning("could not detect the installed Chrome version; using the latest driver")
    return 0


def _get_chrome_binary_path():
    for path in (
        os.environ.get("CHROME_PATH"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if path and os.path.exists(path):
            return path
    return None


def _major_from_text(text):
    match = re.search(r"\b(\d+)\.\d+\.\d+\.\d+\b", text or "")
    return int(match.group(1)) if match else 0


def _get_major_version_from_install_dir(chrome_path):
    app_dir = os.path.dirname(chrome_path)
    try:
        versions = [
            entry
            for entry in os.listdir(app_dir)
            if os.path.isdir(os.path.join(app_dir, entry)) and _major_from_text(entry)
        ]
    except OSError:
        return 0
    if not versions:
        return 0
    # Ignore non-numeric segments: updater leftovers like '120.0.6099.109.old'
    # pass the _major_from_text filter but would crash a plain int() sort.
    versions.sort(key=lambda value: tuple(int(part) for part in value.split(".") if part.isdigit()))
    return _major_from_text(versions[-1])


def _get_major_version_from_windows_registry():
    if os.name != "nt":
        return 0
    try:
        import winreg
    except ImportError:
        return 0

    keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Google\Chrome\BLBeacon"),
    )
    for root, subkey in keys:
        try:
            with winreg.OpenKey(root, subkey) as key:
                version, _ = winreg.QueryValueEx(key, "version")
                major = _major_from_text(version)
                if major:
                    return major
        except OSError:
            continue
    return 0


class ManagedChrome(uc.Chrome):
    def __init__(self, *args, **kwargs):
        self._closed = False
        super().__init__(*args, **kwargs)

    def quit(self):
        if self._closed:
            return
        try:
            return super().quit()
        finally:
            self._closed = True


class Browser:
    __instance = None
    __lock = threading.Lock()

    @staticmethod
    def get():
        # print("Browser.getBrowser() called")
        if Browser.__instance is None:
            with Browser.__lock:
                if Browser.__instance is None:
                    # print("Creating new browser instance due to no instance found")
                    Browser.__instance = Browser()
        return Browser.__instance

    def __init__(self):
        if Browser.__instance is not None:
            raise Exception("This class is a singleton!")
        self._driver = None
        options = uc.ChromeOptions()
        chrome_path = _get_chrome_binary_path()
        if chrome_path:
            options.binary_location = chrome_path
        # Proxies not supported on login.
        # if WITH_PROXIES:
        #     options.add_argument('--proxy-server={}'.format(PROXIES[0]))
        self._driver = ManagedChrome(options=options, version_main=_get_chrome_major_version())
        Browser.__instance = self

    @property
    def driver(self):
        return self._driver

    def close(self):
        if self._driver is not None:
            self._driver.quit()
            self._driver = None
        if Browser.__instance is self:
            Browser.__instance = None

    quit = close
