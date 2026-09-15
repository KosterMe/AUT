"""Pure logic: no database, no HTTP, no subprocesses.

Everything here can be exercised with plain values, which is why the slicing
rules and caption formatting — the parts most likely to need tuning — are the
easiest things in the project to test.

What is left is AUT's own: where a clip starts and ends, what it is called,
where it came from. The EDL, the style, the subtitle timing and the rules that
place b-roll went to `montage/` — they turn pixels into pixels, and that is a
different service now (§2.2).
"""
