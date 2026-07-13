"""Encoder selection.

A listed encoder is not a working one — a Windows ffmpeg build advertises
`h264_nvenc` on machines with no NVIDIA card — so selection has to probe, and
every path has to end somewhere that actually encodes.
"""
from __future__ import annotations

import pytest

from app.adapters.media import encoders


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """Probe results are cached per process; tests must not inherit them.

    Guarded because a test may still have `is_usable` monkeypatched to a plain
    function when this tears down, and that has no cache to clear.
    """
    def clear():
        clearer = getattr(encoders.is_usable, "cache_clear", None)
        if clearer:
            clearer()

    clear()
    yield
    clear()


def fake_probe(working: set[str]):
    return lambda name: name in working


class TestSelection:
    def test_auto_picks_the_first_working_hardware_encoder(self, monkeypatch):
        monkeypatch.setattr(encoders, "is_usable", fake_probe({"h264_amf", "libx264"}))
        assert encoders.resolve("auto") == "h264_amf"

    def test_auto_prefers_nvidia_when_several_work(self, monkeypatch):
        monkeypatch.setattr(encoders, "is_usable", fake_probe({"h264_amf", "h264_nvenc"}))
        assert encoders.resolve("auto") == "h264_nvenc"

    def test_auto_falls_back_to_software(self, monkeypatch):
        monkeypatch.setattr(encoders, "is_usable", fake_probe({"libx264"}))
        assert encoders.resolve("auto") == "libx264"

    def test_an_explicit_working_encoder_is_honoured(self, monkeypatch):
        monkeypatch.setattr(encoders, "is_usable", fake_probe({"h264_qsv", "h264_nvenc"}))
        assert encoders.resolve("h264_qsv") == "h264_qsv"

    def test_an_explicit_broken_encoder_falls_back_instead_of_failing(self, monkeypatch):
        """Better a slow render than no render."""
        monkeypatch.setattr(encoders, "is_usable", fake_probe({"libx264"}))
        assert encoders.resolve("h264_nvenc") == "libx264"

    def test_software_is_never_probed(self, monkeypatch):
        def explode(name):
            raise AssertionError("libx264 should not need probing")

        monkeypatch.setattr(encoders, "is_usable", explode)
        assert encoders.resolve("libx264") == "libx264"


class TestQualityMapping:
    """Each encoder expresses quality its own way; callers only know CRF."""

    def test_software_uses_crf(self):
        args = encoders.encoder_args("libx264", crf=23)
        assert args[:2] == ["-c:v", "libx264"]
        assert "-crf" in args and args[args.index("-crf") + 1] == "23"

    def test_nvidia_uses_constant_quality(self):
        args = encoders.encoder_args("h264_nvenc", crf=23)
        assert args[args.index("-cq") + 1] == "23"
        # vbr with cq needs an explicit zero bitrate to stay quality-driven.
        assert args[args.index("-b:v") + 1] == "0"

    def test_amd_uses_constant_qp(self):
        args = encoders.encoder_args("h264_amf", crf=23)
        assert args[args.index("-qp_i") + 1] == "23"
        assert args[args.index("-qp_p") + 1] == "23"

    def test_intel_uses_global_quality(self):
        args = encoders.encoder_args("h264_qsv", crf=23)
        assert args[args.index("-global_quality") + 1] == "23"

    def test_apple_inverts_the_scale(self):
        """videotoolbox takes 1-100 where higher is better, the opposite of CRF."""
        low_crf = encoders.encoder_args("h264_videotoolbox", crf=18)
        high_crf = encoders.encoder_args("h264_videotoolbox", crf=32)
        assert int(low_crf[low_crf.index("-q:v") + 1]) > int(high_crf[high_crf.index("-q:v") + 1])

    def test_an_unknown_encoder_maps_to_software(self):
        assert encoders.encoder_args("something_invented", crf=23)[:2] == ["-c:v", "libx264"]

    @pytest.mark.parametrize("crf,expected", [(-5, "0"), (99, "51")])
    def test_quality_is_clamped_to_a_valid_range(self, crf, expected):
        args = encoders.encoder_args("libx264", crf=crf)
        assert args[args.index("-crf") + 1] == expected


class TestRealProbe:
    def test_software_encoding_works_on_this_machine(self):
        """A sanity check on the probe itself: if this fails, the probe is
        broken rather than the encoder."""
        assert encoders.is_usable("libx264") is True

    def test_a_nonexistent_encoder_is_not_usable(self):
        assert encoders.is_usable("h264_definitely_not_real") is False
