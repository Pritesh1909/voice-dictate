"""Device choice for the speech engine (no model or GPU needed)."""
from types import SimpleNamespace as D

import pytest

from parakeet_dictate import config
from parakeet_dictate.engine import device_order

# The order transcribe.cpp lists them on this laptop.
DEVS = [D(device_type="igpu", name="Vulkan0"), D(device_type="gpu", name="Vulkan1"), D(device_type="cpu", name="CPU")]


def names(want, devs=DEVS):
    return [d.name for d in device_order(devs, want)]


def test_auto_prefers_discrete_gpu_then_integrated_then_cpu():
    assert names("auto") == ["Vulkan1", "Vulkan0", "CPU"]


def test_explicit_choices_always_end_with_cpu():
    assert names("gpu") == ["Vulkan1", "CPU"]
    assert names("igpu") == ["Vulkan0", "CPU"]
    assert names("cpu") == ["CPU"]


def test_no_gpu_falls_back_to_cpu():
    assert names("gpu", [D(device_type="cpu", name="CPU")]) == ["CPU"]


def test_device_setting():
    assert config.parse("").device == "auto"
    assert config.parse('[model]\ndevice = "cpu"').device == "cpu"
    with pytest.raises(config.ConfigError):
        config.parse('[model]\ndevice = "tpu"')


def test_old_settings_file_with_hotwords_score_still_loads():
    assert config.parse("[accuracy]\nhotwords_score = 2.0").hotwords
