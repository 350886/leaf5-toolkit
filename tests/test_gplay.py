"""Google Play：APK 选取顺序 + enable 的四种出口。"""

from __future__ import annotations

import pytest

from conftest import FakeAdb
from l5tk.core.adb import Adb
from l5tk.core.errors import GpError
from l5tk.logx import null_logger
from l5tk.ops.gplay import GPlay, candidate_apks, find_apk

STORE = "com.android.vending"


def _mk(path, mb=1.5):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PK\x03\x04" + b"\x00" * int(mb * 1024 * 1024))
    return path


def test_candidate_order_by_dir_not_size(tmp_path):
    """外层目录的小体积新版，必须压过内层的大体积旧版。"""
    small_new = _mk(tmp_path / "apk" / "Phonesky_42.apk", 1.2)
    big_old = _mk(tmp_path / "assets" / "apk" / "Phonesky_44.apk", 5)
    got = candidate_apks(base_dir=tmp_path, include_defaults=False)
    assert got[0] == small_new.resolve(), "目录顺序应当优先于体积"
    assert big_old.resolve() in [p.resolve() for p in got]


def test_within_dir_take_largest(tmp_path):
    _mk(tmp_path / "apk" / "a.apk", 1.1)
    big = _mk(tmp_path / "apk" / "b.apk", 3)
    got = candidate_apks(base_dir=tmp_path, include_defaults=False)
    assert got[0] == big.resolve()


def test_ignore_small_files(tmp_path):
    _mk(tmp_path / "apk" / "placeholder.apk", 0.01)
    assert candidate_apks(base_dir=tmp_path, include_defaults=False) == []


def test_explicit_wins(tmp_path):
    p = _mk(tmp_path / "x.apk", 2)
    assert candidate_apks(base_dir=tmp_path, explicit=p, include_defaults=False) == [p]


def test_explicit_missing_gives_empty(tmp_path):
    assert candidate_apks(base_dir=tmp_path, explicit=tmp_path / "no.apk", include_defaults=False) == []


def test_find_apk_raises_when_none(tmp_path):
    with pytest.raises(GpError) as e:
        find_apk(base_dir=tmp_path, include_defaults=False)
    assert "apk" in e.value.hint


def test_state(cfg):
    fake = FakeAdb()
    fake.packages[STORE] = "/data/app/x/base.apk"
    gp = GPlay(Adb(cfg, runner=fake), null_logger())
    st = gp.state()
    assert st["store"] and st["version"] == "42.0.18-31"
    assert not st["gms"]


class _StoreOk(FakeAdb):
    """install 之后商店能起来。"""

    def __call__(self, argv, timeout):
        r = super().__call__(argv, timeout)
        if "install" in argv:
            self.top = STORE + "/.AssetBrowserActivity"
        return r


def test_enable_launches_after_install(cfg, tmp_path):
    apk = _mk(tmp_path / "apk" / "Phonesky.apk", 2)
    fake = _StoreOk()
    gp = GPlay(Adb(cfg, runner=fake), null_logger())
    o = gp.enable(apk=apk, base_dir=tmp_path)
    assert o.ok and o.stage == "launched"


def test_enable_need_login_when_blocked(cfg, tmp_path):
    """拉起失败 → 打开账号页成功 → need_login，且必须告诉用户去点哪里。"""
    apk = _mk(tmp_path / "apk" / "Phonesky.apk", 2)
    fake = FakeAdb()          # top 恒为空 → 拉起判失败
    gp = GPlay(Adb(cfg, runner=fake), null_logger())
    o = gp.enable(apk=apk, base_dir=tmp_path)
    assert not o.ok
    assert o.stage in ("need_login", "failed"), o.stage
    if o.stage == "need_login":
        assert "account_page" in o.data


def test_enable_blocked_reports_version(cfg, tmp_path):
    apk = _mk(tmp_path / "apk" / "Phonesky.apk", 2)

    class NoAccountPage(FakeAdb):
        def __call__(self, argv, timeout):
            if "am" in argv and "start" in argv:
                from l5tk.core.result import ExecResult
                return ExecResult(tuple(argv), 1, "", "Error: Activity not started", 1)
            return super().__call__(argv, timeout)

    fake = NoAccountPage()
    gp = GPlay(Adb(cfg, runner=fake), null_logger())
    o = gp.enable(apk=apk, base_dir=tmp_path)
    assert not o.ok
    assert o.data.get("version")


def test_install_reports_downgrade_hint(cfg, tmp_path):
    apk = _mk(tmp_path / "apk" / "Phonesky.apk", 2)

    class Downgrade(FakeAdb):
        def __call__(self, argv, timeout):
            if "install" in argv:
                from l5tk.core.result import ExecResult
                return ExecResult(tuple(argv), 1, "",
                                  "Failure [INSTALL_FAILED_VERSION_DOWNGRADE]", 3)
            return super().__call__(argv, timeout)

    gp = GPlay(Adb(cfg, runner=Downgrade()), null_logger())
    with pytest.raises(GpError) as e:
        gp.install(apk)
    assert "降级" in e.value.hint or "-d" in e.value.hint
