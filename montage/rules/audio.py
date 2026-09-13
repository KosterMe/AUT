"""Choosing what the clip sounds like, without a person.

Two things get decided here, and they are decided the same way b-roll is: from
the library, by tag, seeded by the clip so a re-render sounds identical.

**A music bed** tagged `music`, sitting under everything and ducking out of the
way whenever anybody speaks. Ducking rather than a fixed low level, because a
fixed level that is quiet enough under speech is inaudible in a pause, and one
loud enough in a pause competes with the speech.

**Effects** tagged `sfx`, at moments where the picture already changes — a cut
made by silence removal, an insert appearing. A whoosh over a continuous shot
is a sound with nothing to explain it; over a cut it is the cut. That is the
whole placement rule, and it is why effects are never placed on a beat.

Pure logic, as with every other planner: values in, values out.
"""
from __future__ import annotations

from montage.composition import Composition, MusicBed, SoundEffect
from montage.rules.inserts import AssetOption
from montage.style import MUSIC_TAG, SFX_TAG, AudioPolicy, tag_aliases


def choose_music(
    composition: Composition,
    *,
    assets: list[AssetOption],
    policy: AudioPolicy | None = None,
    seed: int = 0,
) -> MusicBed | None:
    """Pick a bed for this clip, or nothing if the library has no music."""
    rules = policy or AudioPolicy()
    tracks = _tagged(assets, MUSIC_TAG)
    if not tracks or composition.duration_sec <= 0:
        return None

    track = tracks[seed % len(tracks)]
    return MusicBed(
        source_path=track.path,
        gain_db=rules.music_gain_db,
        start_sec=_offset_into(track, composition.duration_sec, seed),
        fade_in_sec=rules.music_fade_in_sec,
        fade_out_sec=rules.music_fade_out_sec,
        duck_threshold=rules.duck_threshold,
        duck_ratio=rules.duck_ratio,
        duck_attack_ms=rules.duck_attack_ms,
        duck_release_ms=rules.duck_release_ms,
    )


def choose_effects(
    composition: Composition,
    *,
    assets: list[AssetOption],
    policy: AudioPolicy | None = None,
    seed: int = 0,
) -> tuple[SoundEffect, ...]:
    """Put a sound on each visible change, up to the policy's limit."""
    rules = policy or AudioPolicy()
    sounds = _tagged(assets, SFX_TAG)
    if not sounds:
        return ()

    effects: list[SoundEffect] = []
    previous = -rules.effect_min_gap_seconds
    for order, moment in enumerate(_moments(composition, rules)):
        if len(effects) >= rules.max_effects:
            break
        at = max(0.0, moment - rules.effect_lead_seconds)
        if at - previous < rules.effect_min_gap_seconds:
            continue
        if at >= composition.duration_sec:
            break
        sound = sounds[(seed + order) % len(sounds)]
        effects.append(
            SoundEffect(
                source_path=sound.path,
                at_sec=round(at, 3),
                duration_sec=_effect_length(sound, rules),
                gain_db=rules.effect_gain_db,
            )
        )
        previous = at
    return tuple(effects)


def _moments(composition: Composition, policy: AudioPolicy) -> list[float]:
    """Output times where something visibly changes.

    The first segment's start is not one of them: there is nothing before it
    for a transition sound to transition from.
    """
    moments: list[float] = []
    if policy.effects_on_cuts:
        moments.extend(
            segment.output_start_sec
            for segment in composition.timeline()[1:]
        )
    if policy.effects_on_inserts:
        moments.extend(insert.at_sec for insert in composition.inserts)
    return sorted({round(at, 3) for at in moments if at > 0})


def _tagged(assets: list[AssetOption], tag: str) -> list[AssetOption]:
    """Library items carrying a tag, least recently used first.

    Any spelling of the tag: a bed tagged "музыка" is a bed.
    """
    wanted = set(tag_aliases(tag))
    return [
        asset
        for asset in sorted(assets, key=lambda a: (a.last_used_rank, a.asset_id))
        if wanted.intersection(asset.tags) and not asset.still
    ]


def _offset_into(track: AssetOption, clip_duration: float, seed: int) -> float:
    """Where in the track to start.

    A long track always started from zero means every clip of a job opens on
    the same four bars. Stepping into it by a seeded amount costs nothing and
    stops that, while staying deterministic per clip.
    """
    room = track.duration_sec - clip_duration
    if room <= 1.0:
        return 0.0
    return round((seed * 17.0) % room, 3)


def _effect_length(sound: AssetOption, policy: AudioPolicy) -> float:
    natural = sound.duration_sec if sound.duration_sec > 0 else policy.effect_seconds
    return round(min(natural, policy.effect_seconds), 3)


__all__ = [
    "AudioPolicy",
    "MUSIC_TAG",
    "SFX_TAG",
    "choose_effects",
    "choose_music",
]
