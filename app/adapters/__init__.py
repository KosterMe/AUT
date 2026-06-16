"""Everything that talks to the outside world.

Each subpackage wraps one external dependency behind a small interface, so the
services above never import `yt_dlp`, `subprocess`, `requests` or
`faster_whisper` directly — and tests can replace any of them with a fake.
"""
