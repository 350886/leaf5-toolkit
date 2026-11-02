"""机型识别。重点测「不该认出来的别认出来」。"""

from __future__ import annotations

import pytest

from conftest import LEAF5_PROPS, TAB8C_PROPS
from l5tk.device import detect, ensure_known, by_key
from l5tk.core.errors import DeviceError


def test_leaf5():
    d = detect(LEAF5_PROPS)
    assert d.profile.key == "leaf5"
    assert not d.unknown
    assert any("model" in e for e in d.evidence)
    assert any("SoC" in e for e in d.evidence)


def test_tab8c():
    d = detect(TAB8C_PROPS)
    assert d.profile.key == "tab8c"
    assert d.score >= 60


def test_tab8_vs_tab8c_not_confused():
    """Tab8 的 model 不能匹配到 Tab8C 的正则（^tab\\s*8$ 不吃 'Tab8C'）。"""
    p = dict(TAB8C_PROPS)
    p["ro.product.model"] = "Tab8"
    p["ro.product.device"] = "tab8"
    p["ro.product.name"] = "BOOX_Tab8"
    assert detect(p).profile.key == "tab8"


def test_tab10_distinct():
    p = dict(TAB8C_PROPS)
    p["ro.product.model"] = "Tab10"
    p["ro.product.device"] = "tab10"
    d = detect(p)
    assert d.profile.key == "tab10"


def test_model_only_not_enough():
    """只改 model、SoC 对不上 —— 证据来源只有一处，应当判未知。"""
    p = {"ro.product.model": "Leaf5", "ro.build.version.sdk": "33"}
    d = detect(p)
    assert d.unknown, d.evidence


def test_soc_only_not_enough():
    p = {"ro.board.platform": "lito", "ro.build.version.sdk": "33"}
    assert detect(p).unknown


def test_model_says_leaf_but_soc_is_bengal():
    """model 与 SoC 矛盾时不给出高置信结论。

    两者各算一条证据，仍然会认成 leaf5（model 40 + soc 15 = 55），
    但分数必须明显低于完整匹配 —— 这就是要让 probe 报 warning 的信号。
    """
    p = dict(LEAF5_PROPS)
    p["ro.board.platform"] = "bengal"
    full = detect(LEAF5_PROPS)
    mixed = detect(p)
    assert mixed.score < full.score
    assert "lito" not in " ".join(mixed.evidence)


def test_tabboox_falls_back_to_family():
    """Tab 三台的 model 都报 TabBoox —— 只能认到家族，不能认成某台。"""
    p = dict(TAB8C_PROPS)
    p["ro.product.model"] = "TabBoox"
    p["ro.product.device"] = ""
    p["ro.product.name"] = ""
    d = detect(p)
    assert d.profile.key == "tab-unknown"
    assert d.ambiguous, "家族兜底必须标歧义"
    assert set(d.candidates) == {"tab8c", "tab8", "tab10"}


def test_tabboox_with_device_still_resolves():
    """model 是 TabBoox 但 device 报了具体型号时，以 device 为准。"""
    p = dict(TAB8C_PROPS)
    p["ro.product.model"] = "TabBoox"
    d = detect(p)
    assert d.profile.key == "tab8c"
    assert not d.ambiguous


def test_ambiguous_refuses_partition_write():
    """认到家族也不许刷 —— Tab10 与 Tab8 的 boot 差 60%。"""
    p = dict(TAB8C_PROPS)
    p["ro.product.model"] = "TabBoox"
    p["ro.product.device"] = ""
    p["ro.product.name"] = ""
    d = detect(p)
    with pytest.raises(DeviceError) as e:
        ensure_known(d)
    assert "家族" in e.value.message
    assert "tab10" in e.value.hint


def test_empty_props():
    d = detect({})
    assert d.unknown
    assert d.profile.key == "generic"


def test_ensure_known_raises_on_generic():
    d = detect({})
    with pytest.raises(DeviceError):
        ensure_known(d)
    assert ensure_known(d, allow_unknown=True).key == "generic"


def test_profiles_have_unique_keys():
    from l5tk.device.profiles import PROFILES
    keys = [p.key for p in PROFILES]
    assert len(keys) == len(set(keys))
    assert by_key("nope").key == "generic"


def test_boot_part_declared():
    for k in ("leaf5", "tab8c", "tab8", "tab10"):
        assert by_key(k).boot_part == "boot"
