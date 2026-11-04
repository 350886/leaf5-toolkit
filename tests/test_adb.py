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


def test_su_probe_tries_legacy_form(cfg):
    """只认 `su 0 id` 的老式 su 也要认出来 —— 只试 `su -c` 会误报没 root。"""
    fake = FakeAdb(su=True, su_forms={("su", "0")})
    ok_, why = Adb(cfg, runner=fake).su_probe()
    assert ok_ and why == ""
    # 第一条必须先试过，说明是「失败后回退」而不是碰巧
    assert fake.calls[0][-3:] == ("su", "-c", "id")


def test_su_probe_timeout_says_authorize(cfg):
    """Magisk 授权框没人点 → 超时。要给「去手机上点允许」，不能只说没有 root。"""
    ok_, why = Adb(cfg, runner=FakeAdb(su=True, su_timed_out=True)).su_probe()
    assert not ok_
    assert "授权框" in why
    assert "允许" in why


def test_su_probe_timeout_stops_early(cfg):
    """超时后不再试其它形式，否则白等三倍超时。"""
    fake = FakeAdb(su=True, su_timed_out=True)
    Adb(cfg, runner=fake).su_probe()
    assert len(fake.calls) == 1


def test_su_probe_denied_says_denied(cfg):
    ok_, why = Adb(cfg, runner=FakeAdb(su=False)).su_probe()
    assert not ok_
    assert "拒绝" in why


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


# ---------------- 大文件传输 ----------------

def _runner_devices_ok(argv, timeout, handler):
    from l5tk.core.result import ExecResult
    args = list(argv)
    if "devices" in args:
        return ExecResult(tuple(args), 0,
                          "List of devices attached\nA\tdevice\n", "", 1)
    return handler(args)


def test_pull_truncated_raises(cfg, tmp_path):
    """adb pull 会 rc=0 却只落一半 —— 96 MB 分区实测断在 33%。

    这是真机踩到的：残片 33,685,504 字节，md5 与设备侧不符。
    调用方只看 rc 就会把残片当备份拿去 dd。
    """
    from pathlib import Path

    def handler(args):
        from l5tk.core.result import ExecResult
        if "pull" in args:
            Path(args[3]).write_bytes(b"x" * 1024)      # 只写 1 KB 却报成功
            return ExecResult(tuple(args), 0, "1 file pulled", "", 1)
        return ExecResult(tuple(args), 0, "", "", 1)

    a = Adb(cfg, runner=lambda argv, t: _runner_devices_ok(argv, t, handler))
    with pytest.raises(AdbError) as e:
        a.pull("/dev/block/x", str(tmp_path / "out.img"), expect_size=4096)
    assert "字节" in str(e.value)


def test_pull_chunked_merges_in_order(cfg, tmp_path):
    """超过 chunk 自动分块：逐块校验大小，再按顺序合并。"""
    from pathlib import Path
    from l5tk.core.result import ExecResult

    MB2 = 2 << 20
    size = MB2 * 2 + 1234                       # 3 块，最后一块 1234 字节

    def handler(args):
        if "pull" in args:
            name = args[2].rsplit("/", 1)[-1]
            n = 1234 if name.endswith("ac") else MB2
            Path(args[3]).write_bytes(b"\1" * n)
            return ExecResult(tuple(args), 0, "ok", "", 1)
        if "ls" in args:
            return ExecResult(tuple(args), 0, "part_aa\npart_ab\npart_ac\n", "", 1)
        return ExecResult(tuple(args), 0, "", "", 1)

    a = Adb(cfg, runner=lambda argv, t: _runner_devices_ok(argv, t, handler))
    out = tmp_path / "out.img"
    a.pull("/dev/block/x", str(out), expect_size=size, chunk=MB2)
    assert out.stat().st_size == size
    assert out.read_bytes()[:4] == b"\1\1\1\1"


def test_pull_chunked_reports_bad_block(cfg, tmp_path):
    """某一块重试到底还是短，必须报出来，不能合并出个半成品。"""
    from pathlib import Path
    from l5tk.core.result import ExecResult

    MB2 = 2 << 20

    def handler(args):
        if "pull" in args:
            name = args[2].rsplit("/", 1)[-1]
            n = MB2 - 100 if name.endswith("ab") else MB2   # 第二块永远短
            Path(args[3]).write_bytes(b"\1" * n)
            return ExecResult(tuple(args), 0, "ok", "", 1)
        if "ls" in args:
            return ExecResult(tuple(args), 0, "part_aa\npart_ab\n", "", 1)
        if "devices" in args:
            return ExecResult(tuple(args), 0, "List of devices attached\nA\tdevice\n", "", 1)
        return ExecResult(tuple(args), 0, "", "", 1)

    a = Adb(cfg, runner=lambda argv, t: handler(list(argv)))
    import l5tk.core.adb as _m
    old = _m.time.sleep
    _m.time.sleep = lambda *_: None          # 别真等 12 次 × 3 秒
    try:
        with pytest.raises(AdbError) as e:
            a.pull("/dev/block/x", str(tmp_path / "o.img"),
                   expect_size=MB2 * 2, chunk=MB2)
        assert "分块拉取失败" in str(e.value)
    finally:
        _m.time.sleep = old



def test_pull_chunked_falls_back_to_execout(cfg, tmp_path):
    """pull 通道坏掉时，同一块改走 exec-out 必须能救回来。

    2026-11-04 真机：super 的 1.3 GB gz，第 567 块起 pull 每次都停在 1,245,184
    字节（rc=0，重连 12 次一模一样）；同一时刻 exec-out cat 一次拿满 2,097,152。
    设备侧 md5 秒算得出 —— 坏的是 sync 通道，不是文件、不是空间。
    """
    from pathlib import Path
    from l5tk.core.result import ExecResult

    MB2 = 2 << 20
    size = MB2 * 2

    def handler(args):
        if "pull" in args:
            Path(args[3]).write_bytes(b"x" * 1024)      # 永远只落 1 KB
            return ExecResult(tuple(args), 0, "[  3%] part", "", 1)
        if "ls" in args and "-l" in args:
            return ExecResult(tuple(args), 0,
                              "-rw-r--r-- 1 shell shell %d part_aa\n"
                              "-rw-r--r-- 1 shell shell %d part_ab\n"
                              % (MB2, MB2), "", 1)
        if "ls" in args:
            return ExecResult(tuple(args), 0, "part_aa\npart_ab\n", "", 1)
        return ExecResult(tuple(args), 0, "", "", 1)

    a = Adb(cfg, runner=lambda argv, t: handler(list(argv)),
            bin_runner=lambda argv, t: b"\2" * MB2)
    out = tmp_path / "out.img"
    a.pull("/dev/block/x", str(out), expect_size=size, chunk=MB2)

    assert out.stat().st_size == size
    assert out.read_bytes()[:4] == b"\2\2\2\2"          # 内容来自 exec-out
    assert any("exec-out" in c for c in a._last_pull_note()) if hasattr(
        a, "_last_pull_note") else True


def test_split_partial_detected(cfg, tmp_path):
    """split 在 /data 写满时会写一半就退出。

    表现是：前面几块完好、后面几块短或压根没有，只有拉到那一块才炸 ——
    时间全花在「以为快好了」上。必须在 split 之后当场认出来。
    """
    from l5tk.core.result import ExecResult

    MB2 = 2 << 20

    def handler(args):
        if "ls" in args and "-l" in args:
            return ExecResult(tuple(args), 0,
                              "-rw-r--r-- 1 shell shell %d part_aa\n" % (MB2 // 2), "", 1)
        if "ls" in args:
            return ExecResult(tuple(args), 0, "part_aa\npart_ab\n", "", 1)
        return ExecResult(tuple(args), 0, "", "", 1)

    a = Adb(cfg, runner=lambda argv, t: handler(list(argv)))
    with pytest.raises(AdbError) as e:
        a.pull("/dev/block/x", str(tmp_path / "o.img"),
               expect_size=MB2 * 2, chunk=MB2)
    assert "split 中途退出" in str(e.value)


def test_pull_chunked_free_space_guard(cfg, tmp_path):
    """空间不够要在 split 之前就拒绝，别先花十分钟 split 再报错。"""
    from l5tk.core.result import ExecResult

    MB2 = 2 << 20

    def handler(args):
        if "df" in args:
            return ExecResult(tuple(args), 0,
                              "Filesystem     1K-blocks     Used Available Use% Mounted on\n"
                              "/dev/block/dm-13 50000000 40000000  100000   1% /data\n",
                              "", 1)
        return ExecResult(tuple(args), 0, "", "", 1)

    a = Adb(cfg, runner=lambda argv, t: handler(list(argv)))
    with pytest.raises(AdbError) as e:
        a.pull("/dev/block/x", str(tmp_path / "o.img"),
               expect_size=MB2 * 100, chunk=MB2)
    assert "/data 可用" in str(e.value)


def test_exec_out_to_writes_bytes(cfg, tmp_path):
    """exec-out 是二进制通路：非 UTF-8 的字节必须原样落盘。"""
    a = Adb(cfg, runner=lambda argv, t: ExecResult(tuple(argv), 0, "", "", 1),
            bin_runner=lambda argv, t: bytes(range(256)))
    dst = tmp_path / "b.bin"
    n = a.exec_out_to("/data/local/tmp/x", str(dst))
    assert n == 256
    assert dst.read_bytes() == bytes(range(256))


def test_pull_chunked_falls_back_to_execout(cfg, tmp_path):
    """pull 通道坏掉时，同一块改走 exec-out 必须能救回来。

    2026-11-04 真机：super 的 1.3 GB gz，第 567 块起 pull 每次都停在 1,245,184
    字节（rc=0，重连 12 次一模一样）；同一时刻 exec-out cat 一次拿满 2,097,152。
    设备侧 md5 秒算得出 —— 坏的是 sync 通道，不是文件、不是空间。
    """
    from pathlib import Path
    from l5tk.core.result import ExecResult

    MB2 = 2 << 20
    size = MB2 * 2

    def handler(args):
        if "pull" in args:
            Path(args[3]).write_bytes(b"x" * 1024)      # 永远只落 1 KB
            return ExecResult(tuple(args), 0, "[  3%] part", "", 1)
        if "ls" in args and "-l" in args:
            return ExecResult(tuple(args), 0,
                              "-rw-r--r-- 1 shell shell %d part_aa\n"
                              "-rw-r--r-- 1 shell shell %d part_ab\n"
                              % (MB2, MB2), "", 1)
        if "ls" in args:
            return ExecResult(tuple(args), 0, "part_aa\npart_ab\n", "", 1)
        return ExecResult(tuple(args), 0, "", "", 1)

    a = Adb(cfg, runner=lambda argv, t: handler(list(argv)),
            bin_runner=lambda argv, t: b"\2" * MB2)
    out = tmp_path / "out.img"
    a.pull("/dev/block/x", str(out), expect_size=size, chunk=MB2)

    assert out.stat().st_size == size
    assert out.read_bytes()[:4] == b"\2\2\2\2"          # 内容来自 exec-out
    assert any("exec-out" in c for c in a._last_pull_note()) if hasattr(
        a, "_last_pull_note") else True


def test_split_partial_detected(cfg, tmp_path):
    """split 在 /data 写满时会写一半就退出。

    表现是：前面几块完好、后面几块短或压根没有，只有拉到那一块才炸 ——
    时间全花在「以为快好了」上。必须在 split 之后当场认出来。
    """
    from l5tk.core.result import ExecResult

    MB2 = 2 << 20

    def handler(args):
        if "ls" in args and "-l" in args:
            return ExecResult(tuple(args), 0,
                              "-rw-r--r-- 1 shell shell %d part_aa\n" % (MB2 // 2), "", 1)
        if "ls" in args:
            return ExecResult(tuple(args), 0, "part_aa\npart_ab\n", "", 1)
        return ExecResult(tuple(args), 0, "", "", 1)

    a = Adb(cfg, runner=lambda argv, t: handler(list(argv)))
    with pytest.raises(AdbError) as e:
        a.pull("/dev/block/x", str(tmp_path / "o.img"),
               expect_size=MB2 * 2, chunk=MB2)
    assert "split 中途退出" in str(e.value)


def test_pull_chunked_free_space_guard(cfg, tmp_path):
    """空间不够要在 split 之前就拒绝，别先花十分钟 split 再报错。"""
    from l5tk.core.result import ExecResult

    MB2 = 2 << 20

    def handler(args):
        if "df" in args:
            return ExecResult(tuple(args), 0,
                              "Filesystem     1K-blocks     Used Available Use% Mounted on\n"
                              "/dev/block/dm-13 50000000 40000000  100000   1% /data\n",
                              "", 1)
        return ExecResult(tuple(args), 0, "", "", 1)

    a = Adb(cfg, runner=lambda argv, t: handler(list(argv)))
    with pytest.raises(AdbError) as e:
        a.pull("/dev/block/x", str(tmp_path / "o.img"),
               expect_size=MB2 * 100, chunk=MB2)
    assert "/data 可用" in str(e.value)


def test_exec_out_to_writes_bytes(cfg, tmp_path):
    """exec-out 是二进制通路：非 UTF-8 的字节必须原样落盘。"""
    a = Adb(cfg, runner=lambda argv, t: ExecResult(tuple(argv), 0, "", "", 1),
            bin_runner=lambda argv, t: bytes(range(256)))
    dst = tmp_path / "b.bin"
    n = a.exec_out_to("/data/local/tmp/x", str(dst))
    assert n == 256
    assert dst.read_bytes() == bytes(range(256))
