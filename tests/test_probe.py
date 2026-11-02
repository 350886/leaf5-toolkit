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


# ---- 真机 2026-11-02 抓到的两处误判，各留一条锁 ----

# 真实 dumpsys account 片段：两个账号挤在同一段，中间没有空行
DUMPSYS_ACCOUNT = """User UserInfo{0:擁有者:c13}:
  Accounts: 2
    Account {name=番茄免费小说, type=com.dragon.read}
    Account {name=theluck.blue@gmail.com, type=com.google}
  Active accounts: 2
  Registered authenticators:
    AuthenticatorDescription {type=com.google}, ComponentInfo{com.google.android.gms/x}
"""


def _deep(cfg, fake):
    logx._secrets.clear()
    return Prober(Adb(cfg, runner=fake), null_logger()).run(deep=True)


def test_google_account_not_swallowed_by_first_one(cfg):
    """按空行切块再 re.search 只会拿到「番茄免费小说」，Google 账号就丢了。"""
    rep = _deep(cfg, FakeAdb(su=True, accounts=DUMPSYS_ACCOUNT))
    assert rep.google_accounts == ["theluck.blue@gmail.com"]


def test_no_google_account_stays_empty(cfg):
    dump = DUMPSYS_ACCOUNT.replace("theluck.blue@gmail.com, type=com.google",
                                   "someone@example.com, type=com.other")
    rep = _deep(cfg, FakeAdb(su=True, accounts=dump))
    assert rep.google_accounts == []


def test_su_timeout_explains_instead_of_plain_none(cfg):
    """首次 su 会被 Magisk 授权框拦住。报「无」而不说原因，用户会以为没 root。"""
    rep = _deep(cfg, FakeAdb(su=True, su_timed_out=True))
    assert not rep.su
    assert "允许" in rep.su_detail
    assert any("允许" in w for w in rep.warnings)


def test_rooted_line_shows_magisk(cfg):
    rep = _deep(cfg, FakeAdb(su=True))
    assert rep.rooted
    assert rep._root_line().startswith("有（")


# ---- 真机 2026-11-02 抓到的两处误判，各留一条锁 ----

# 真实 dumpsys account 片段：两个账号挤在同一段，中间没有空行
DUMPSYS_ACCOUNT = """User UserInfo{0:擁有者:c13}:
  Accounts: 2
    Account {name=番茄免费小说, type=com.dragon.read}
    Account {name=theluck.blue@gmail.com, type=com.google}
  Active accounts: 2
  Registered authenticators:
    AuthenticatorDescription {type=com.google}, ComponentInfo{com.google.android.gms/x}
"""


def _deep(cfg, fake):
    logx._secrets.clear()
    return Prober(Adb(cfg, runner=fake), null_logger()).run(deep=True)


def test_google_account_not_swallowed_by_first_one(cfg):
    """按空行切块再 re.search 只会拿到「番茄免费小说」，Google 账号就丢了。"""
    rep = _deep(cfg, FakeAdb(su=True, accounts=DUMPSYS_ACCOUNT))
    assert rep.google_accounts == ["theluck.blue@gmail.com"]


def test_no_google_account_stays_empty(cfg):
    dump = DUMPSYS_ACCOUNT.replace("theluck.blue@gmail.com, type=com.google",
                                   "someone@example.com, type=com.other")
    rep = _deep(cfg, FakeAdb(su=True, accounts=dump))
    assert rep.google_accounts == []


def test_su_timeout_explains_instead_of_plain_none(cfg):
    """首次 su 会被 Magisk 授权框拦住。报「无」而不说原因，用户会以为没 root。"""
    rep = _deep(cfg, FakeAdb(su=True, su_timed_out=True))
    assert not rep.su
    assert "允许" in rep.su_detail
    assert any("允许" in w for w in rep.warnings)


def test_rooted_line_shows_magisk(cfg):
    rep = _deep(cfg, FakeAdb(su=True))
    assert rep.rooted
    assert rep._root_line().startswith("有（")
