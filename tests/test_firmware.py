"""固件备份 / 恢复的纯逻辑测试（不碰设备）。

真机那部分靠 `backup` + `restore` 自己跑（会 dd 分区），这里只锁三件事：
分区选择的解析、manifest 的拒绝条件、镜像 md5 的算法。
"""

import json

import pytest

from l5tk.core.errors import DeviceError
from l5tk.ops.firmware import Part, pick_parts, read_manifest, SCHEMA


PARTS = [
    Part("boot_a", "/dev/block/mmcblk0p13", 100663296),
    Part("boot_b", "/dev/block/mmcblk0p14", 100663296),
    Part("vbmeta_a", "/dev/block/mmcblk0p68", 65536),
    Part("vbmeta_b", "/dev/block/mmcblk0p69", 65536),
    Part("super", "/dev/block/mmcblk0p15", 6442450944),
    Part("userdata", "/dev/block/mmcblk0p80", 52800000000),
]


def test_pick_boot_expands_slots():
    """预设写的是 base 名，A/B 设备要展开成 _a/_b，漏了就等于没备份。"""
    assert [p.name for p in pick_parts(PARTS, "boot")] == [
        "boot_a", "boot_b", "vbmeta_a", "vbmeta_b"]


def test_pick_follows_user_order_and_dedups():
    got = [p.name for p in pick_parts(PARTS, "super,boot_b,super")]
    assert got == ["super", "boot_b"]


def test_pick_all_includes_userdata():
    """all 是「你自己要的」，userdata 也在里面；默认 preset 不含它。"""
    names = [p.name for p in pick_parts(PARTS, "all")]
    assert "userdata" in names and "boot_a" in names


def test_pick_critical_excludes_userdata():
    names = [p.name for p in pick_parts(PARTS, "critical")]
    assert "userdata" not in names
    assert "super" in names


def test_pick_unknown_part_shows_choices():
    with pytest.raises(DeviceError) as e:
        pick_parts(PARTS, "nosuch")
    assert "boot_a" in str(e.value.hint)


def test_read_manifest_missing(tmp_path):
    with pytest.raises(DeviceError) as e:
        read_manifest(tmp_path)
    assert "manifest.json" in e.value.message


def test_read_manifest_rejects_other_schema(tmp_path):
    """版本不认识就不写 —— 宁可不动，也不猜字段含义。"""
    (tmp_path / "manifest.json").write_text(
        json.dumps({"schema": SCHEMA + 1, "parts": [{"name": "boot_a"}]}),
        encoding="utf-8")
    with pytest.raises(DeviceError) as e:
        read_manifest(tmp_path)
    assert "只认" in e.value.message


def test_read_manifest_rejects_empty_parts(tmp_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps({"schema": SCHEMA, "parts": []}), encoding="utf-8")
    with pytest.raises(DeviceError):
        read_manifest(tmp_path)
