"""adb 层的解析逻辑。全部走注入的 runner，不起进程。"""

from __future__ import annotations

import pytest

from conftest import FakeAdb
from l5tk.core.adb import Adb, _decode, _quote
from l5tk.core.errors import AdbError


def test_devices_parse(cfg, fake):
    a = Adb(cfg, runner=fake)
    devs = a.devices()
    assert len(devs) == 1
    assert devs[0].serial == "ABCD1234EFGH"
    assert devs[0].usable


def test_devices_offline_not_usable(cfg):
    a = Adb(cfg, runner=FakeAdb(state="offline"))
    assert a.devices()[0].state == "offline"
    with pytest.raises(AdbError) as e:
        a.pick()
    assert "offline" in str(e.value)


def test_unauthorized_hint(cfg):
    a = Adb(cfg, runner=FakeAdb(state="unauthorized"))
    with pytest.raises(AdbError) as e:
        a.pick()
    assert "USB 调试" in e.value.hint


def test_pick_multiple(cfg, tmp_path):
    class Two(FakeAdb):
        def __call__(self, argv, timeout):
            if "devices" in argv:
                from l5tk.core.result import ExecResult
                return ExecResult(tuple(argv), 0,
                                  "List of devices attached\nA1\tdevice\nB2\tdevice\n", "", 1)
            return super().__call__(argv, timeout)

    a = Adb(cfg, runner=Two())
    with pytest.raises(AdbError) as e:
        a.pick()
    assert "2 台" in e.value.message


def test_props_parsed_once(cfg, fake):
    a = Adb(cfg, runner=fake)
    p = a.props(["ro.product.model", "ro.build.type"])
    assert p == {"ro.product.model": "Leaf5", "ro.build.type": "user"}
    getprops = [c for c in fake.calls if c[1:] == ("shell", "getprop")]
    assert len(getprops) == 1, "两次调用只应发一次 getprop"


def test_pm_path_and_installed(cfg, fake):
    fake.packages["com.android.vending"] = "/data/app/x/base.apk"
    a = Adb(cfg, runner=fake)
    assert a.installed("com.android.vending")
    assert not a.installed("com.example.none")
    assert a.pm_path("com.android.vending") == "/data/app/x/base.apk"


def test_top_activity(cfg):
    a = Adb(cfg, runner=FakeAdb(top="com.android.vending/.AssetBrowserActivity"))
    assert a.top_activity() == "com.android.vending/.AssetBrowserActivity"
    assert a.foreground_pkg() == "com.android.vending"


def test_top_activity_fallback_to_resumed(cfg):
    class Old(FakeAdb):
        def _shell(self, argv, rest):
            if rest[:2] == ["dumpsys", "activity"]:
                from l5tk.core.result import ExecResult
                return ExecResult(tuple(argv), 0,
                                  "  mResumedActivity: ActivityRecord{1 u0 com.foo/.Bar}\n", "", 1)
            return super()._shell(argv, rest)

    a = Adb(cfg, runner=Old())
    assert a.top_activity() == "com.foo/.Bar"


def test_su_available(cfg):
    assert Adb(cfg, runner=FakeAdb(su=True)).su_available()
    assert not Adb(cfg, runner=FakeAdb(su=False)).su_available()


def test_install_has_downgrade_flag(cfg, fake):
    a = Adb(cfg, runner=fake)
    r = a.install("x.apk")
    assert r.ok
    argv = fake.calls[-1]
    assert "-r" in argv and "-d" in argv


def test_decode_gbk():
    # "货运" 的 GBK 字节
    assert _decode("货运".encode("gbk")) == "货运"
    assert _decode(b"plain ascii") == "plain ascii"
    assert _decode(b"") == ""


def test_quote_simple_and_space():
    assert _quote("id") == "id"
    assert _quote("a b") == "'a b'"
    assert _quote("it's") == "'it'\\''s'"


def test_missing_adb_raises(cfg):
    def boom(argv, timeout):
        raise AdbError("找不到 adb：adb")
    a = Adb(cfg, runner=boom)
    with pytest.raises(AdbError):
        a.devices()
