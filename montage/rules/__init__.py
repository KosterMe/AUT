"""The automation inside a scenario: what places b-roll, music and sound.

These were AUT's `domain/inserts.py` and `domain/audio.py` — the "auto
planners". They keep every limit they earned: never over the hook, no more than
one insert every six seconds, never more than a third of the clip, one fragment
once per clip, rotation by last use.
"""
from montage.rules.audio import choose_effects, choose_music
from montage.rules.inserts import AssetOption, choose_inserts

__all__ = ["AssetOption", "choose_effects", "choose_inserts", "choose_music"]
