"""设备画像：脱敏与矛盾标记。"""

from __future__ import annotations

from conftest import LEAF5_PROPS, FakeAdb
from l5tk import logx
from l5tk.core.adb import Adb
from l5tk.logx import null_logger
from l5tk.ops.probe import Prober


def _report(cfg, fake):
    logx._secrets.clear()
    return Prober(Adb(cfg, runner=fake), null_logger()).run(deep=False)


def test_summary_hides_serial(cfg):
    """summary 是唯一直接输出 serial 的地方，必须打码。"""
    fake = FakeAdb()
    rep = _report(cfg, fake)
    assert rep.serial
    assert rep.serial not in rep.summary()


def test_serial_registered_for_masking(cfg):
    fake = FakeAdb()
    rep = _report(cfg, fake)
    assert rep.serial in logx.secrets()


def test_detection_from_real_props(cfg):
    rep = _report(cfg, FakeAdb())
    assert rep.detection.profile.key == "leaf5"
    assert rep.detection.score >= 60


def test_slot_and_lock_read(cfg):
    rep = _report(cfg, FakeAdb())
    assert rep.slot == "b"
    assert rep.locked == "0"
    assert rep.unlocked


def test_store_reported(cfg):
    fake = FakeAdb()
    fake.packages["com.android.vending"] = "/data/app/x/base.apk"
    rep = _report(cfg, fake)
    assert rep.store_installed
    assert rep.store_version == "42.0.18-31"


def test_locked_bootloader_warns(cfg):
    props = dict(LEAF5_PROPS)
    props["ro.boot.flash.locked"] = "1"
    rep = _report(cfg, FakeAdb(props=props))
    assert not rep.unlocked
    assert any("锁定" in w for w in rep.warnings)


def test_unknown_model_warns(cfg):
    props = {"ro.product.model": "SomethingElse", "ro.build.version.sdk": "33"}
    rep = _report(cfg, FakeAdb(props=props))
    assert rep.detection.unknown
    assert any("未识别" in w for w in rep.warnings)


def test_as_dict_is_json_safe(cfg):
    import json
    rep = _report(cfg, FakeAdb())
    json.dumps(rep.as_dict(), ensure_ascii=False)     # 不该抛
