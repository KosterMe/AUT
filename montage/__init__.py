"""The montage service: video in, a scenario, an edited clip out.

A package first and a service second, deliberately (§13.3). The boundary is
drawn here and called in-process; only once that holds does it become HTTP. The
other order — network first — is the usual way to end up with a distributed
monolith, because a seam drawn wrong is an hour's work while it is an import
and a contract renegotiation once it is a wire.

The rule that keeps it honest is one-directional and mechanically checkable:
**nothing under `montage/` imports from `app/`.** There is a test for it.

What lives here is everything that turns pixels into pixels — the scenario
model and its compiler, the EDL, the rules that place b-roll and sound, and the
ffmpeg renderer. What does not is everything about *why* a clip exists:
downloading, cutting, publishing, scheduling, accounts.
"""
