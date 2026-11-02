"""fastbootd 通路的离线测试。

覆盖的是「判断对不对」，不是「能不能真刷」：真刷要一台设备，而且这条
通路存在的理由就是 ONYX 的 abl 裁了 bootloader 的 flash handler，
测试机不是 ONYX 就复现不出来。

假的是 fastboot **命令行**（FakeFbD 只模拟 fastboot.exe 的输出），
真跑的是 core/fastboot.py 的解析 —— getvar 的结果写在 stderr 且带
`(bootloader)` 前缀，只有走真解析才测得到「is-userspace 读得对不对」。

最要紧的几条：
  1. is-userspace 才是 fastbootd 的判据，不是 product 名
  2. bootloader 模式下调 flash 直接拒，不在那儿发无效命令
  3. 进不去时要说清「还有哪几条路」
  4. plan_via 的顺序：有 su 先 dd（不重启），没 su 才进 fastbootd
"""

from __future__ import annotations

import pytest
from conftest import FakeAdb
from test_root import FakeFb, NoRootAdb, _ops

from l5tk.core.result import ExecResult
from l5tk.device import by_key


class FakeFbD:
    """假 fastboot.exe：能在 none / bootloader / fastbootd 之间切。"""

    def __init__(self, mode="bootloader", can_enter=True,
                 unknown_partition=False, part_size="0x6000000"):
        self.mode = mode
        self.can_enter = can_enter
        self.unknown_partition = unknown_partition
        self.part_size = part_size
        self.flashed: list[tuple[str, str]] = []
        self.rebooted = False
        self.cmds: list[tuple[str, ...]] = []

    def __call__(self, argv, timeout):
        argv = tuple(str(a) for a in argv)
        args = list(argv[1:])
        if args[:1] == ["-s"]:
            args = args[2:]
        head = args[0] if args else ""
        self.cmds.append(tuple(args))
        is_fb = "fastboot" in str(argv[0]).lower()

        if head == "devices":
            if not is_fb:
                # wait_for_adb 借同一个 runner 发 `adb devices`
                return ExecResult(argv, 0,
                                  "List of devices attached\nABCD1234\tdevice\n", "", 2)
            out = "" if self.mode == "none" else "XYZ789\tfastboot\n"
            return ExecResult(argv, 0, out, "", 2)
        if head == "getvar":
            name = args[1] if len(args) > 1 else ""
            if name == "is-userspace":
                val = "yes" if self.mode == "fastbootd" else "no"
            elif name.startswith("partition-size:"):
                val = self.part_size                    # 默认 96MB，够装 boot
            elif name == "slot-count":
                val = "2"
            else:
                val = ""
            # fastboot 把 getvar 结果写 stderr，且带 (bootloader) 前缀 —— 照抄
            return ExecResult(argv, 0, "",
                              "(bootloader) %s: %s\n" % (name, val), 2)
        if args[:2] == ["reboot", "fastboot"]:
            if not self.can_enter:
                return ExecResult(argv, 1, "",
                                  "FAILED (remote: 'unknown command')", 3)
            self.mode = "fastbootd"
            return ExecResult(argv, 0, "", "", 3)
        if head == "reboot":
            self.rebooted = True
            return ExecResult(argv, 0, "", "", 2)
        if head == "flash":
            part = args[1]
            self.flashed.append((part, args[2]))
            if self.unknown_partition and "_" in part:
                return ExecResult(argv, 1, "",
                                  "FAILED (remote: 'unknown partition')", 3)
            return ExecResult(argv, 0, "Sending 'boot' OKAY\nWriting OKAY\n",
                              "", 4000)
        return ExecResult(argv, 0, "", "", 1)


def _fd(cfg, fb_runner, adb_runner=None):
    """取 RootOps 上那个配好 FakeFbD 的 Fastbootd。"""
    ops = _ops(cfg, adb_runner or NoRootAdb(su=False), fb_runner)
    return ops.fbd


# ---------------------------------------------------------------- 判定 ------
def test_mode_bootloader(cfg):
    fd = _fd(cfg, FakeFbD(mode="bootloader"))
    assert fd.mode() == "bootloader"
    assert fd.is_userspace() is False


def test_mode_fastbootd(cfg):
    fd = _fd(cfg, FakeFbD(mode="fastbootd"))
    assert fd.mode() == "fastbootd"
    assert fd.is_userspace() is True


def test_mode_none(cfg):
    fd = _fd(cfg, FakeFbD(mode="none"))
    assert fd.mode() == "none"


def test_partition_size_parsed_hex(cfg):
    fd = _fd(cfg, FakeFbD(mode="fastbootd"))
    assert fd.partition_size("boot_b") == 0x6000000


# ---------------------------------------------------------------- 进入 ------
def test_enter_already_there(cfg):
    fd = _fd(cfg, FakeFbD(mode="fastbootd"))
    ok, how = fd.enter()
    assert ok and "已经在 fastbootd" in how


def test_enter_from_bootloader(cfg):
    fb = FakeFbD(mode="bootloader")
    ok, how = _fd(cfg, fb).enter(seconds=1)
    assert ok, how
    assert "reboot fastboot" in how
    assert fb.mode == "fastbootd"


def test_enter_reboot_rejected(cfg):
    """abl 若连 reboot fastboot 都裁了，必须把 remote 报错原样带出来 ——
    只说「进不去」，用户没法判断是驱动问题还是被裁。"""
    fd = _fd(cfg, FakeFbD(mode="bootloader", can_enter=False))
    ok, how = fd.enter(seconds=1)
    assert not ok
    assert "unknown command" in how


def test_enter_no_device(cfg):
    """两边都没设备时要说清楚，不能干等 90 秒再报个超时。"""
    fb = FakeFbD(mode="none")
    fd = _fd(cfg, fb, adb_runner=FakeAdb(state=""))
    ok, how = fd.enter(seconds=1)
    assert not ok
    assert "adb 与 fastboot 都没有设备" in how


def test_enter_via_adb(cfg):
    """没有 fastboot 设备、但 adb 在线 —— 主路。直接发 reboot，
    省掉「让用户手按音量键」这一步。"""
    fb = FakeFbD(mode="none")
    fd = _fd(cfg, fb)
    real_wait = fd._wait
    fd._wait = lambda seconds, poll=1.0: (setattr(fb, "mode", "fastbootd") or True)
    try:
        ok, how = fd.enter(seconds=1)
    finally:
        fd._wait = real_wait
    assert ok, how
    assert "adb reboot fastboot" in how


# ---------------------------------------------------------------- 刷入 ------
def test_flash_refused_in_bootloader(cfg):
    """在 bootloader 里 flash 必然被 abl 拒 —— 与其发出去拿一句 unknown
    command，不如就地拦住，把原因说清楚。"""
    fd = _fd(cfg, FakeFbD(mode="bootloader"))
    with pytest.raises(Exception) as e:
        fd.flash("boot_b", "x.img")
    assert "拒绝 flash" in str(getattr(e.value, "message", e.value))


def test_flash_ok_in_fastbootd(cfg):
    fb = FakeFbD(mode="fastbootd")
    r, used = _fd(cfg, fb).flash("boot_b", "x.img")
    assert r.ok and used == "boot_b"
    assert fb.flashed == [("boot_b", "x.img")]


def test_flash_falls_back_to_no_suffix(cfg):
    """fastbootd 报 unknown partition 时去掉 _a/_b 再试一次 ——
    不同引擎实现分区命名不一致，比让整条通路在这里断掉强。"""
    fb = FakeFbD(mode="fastbootd", unknown_partition=True)
    r, used = _fd(cfg, fb).flash("boot_b", "x.img")
    assert r.ok
    assert used == "boot"
    assert [p for p, _ in fb.flashed] == ["boot_b", "boot"]


# -------------------------------------------------------- plan_via 选择 -----
def test_plan_dd_when_su(cfg):
    """有 su 首选 dd：不重启、写完能立刻回读校验，代价最低。"""
    ops = _ops(cfg, FakeAdb(su=True), FakeFb())
    assert ops.plan_via("auto") == "dd"


def test_plan_fastbootd_when_no_su(cfg):
    ops = _ops(cfg, NoRootAdb(su=False), FakeFb())
    assert ops.plan_via("auto") == "fastbootd"


def test_plan_explicit_passthrough(cfg):
    ops = _ops(cfg, FakeAdb(su=True), FakeFb())
    for v in ("dd", "fastbootd", "fastboot"):
        assert ops.plan_via(v) == v


def test_plan_rejects_unknown(cfg):
    ops = _ops(cfg, FakeAdb(su=True), FakeFb())
    with pytest.raises(Exception) as e:
        ops.plan_via("usb")
    assert "不认识的写入通路" in str(getattr(e.value, "message", e.value))


# ------------------------------------------------------ 端到端（假设备）----
def test_flash_boot_via_fastbootd_end_to_end(cfg, tmp_path):
    from test_root import make_boot
    img = make_boot(tmp_path / "p.img")
    fb = FakeFbD(mode="bootloader")
    ops = _ops(cfg, NoRootAdb(su=False), fb)
    o = ops.flash_boot(img, by_key("leaf5"), backup=False, force=False,
                       via="fastbootd", wait_seconds=1)
    assert o.ok, o.detail
    assert o.data["via"] == "fastbootd"
    assert o.data["partition"] == "boot_b"
    assert fb.flashed and fb.flashed[0][0] == "boot_b"
    assert fb.rebooted


def test_flash_boot_via_fastbootd_cannot_enter(cfg, tmp_path):
    """进不去 fastbootd 时不能只报「失败」—— 必须把剩下的三条路写出来，
    否则用户手上只有一个「刷不进去」。"""
    from test_root import make_boot
    img = make_boot(tmp_path / "p.img")
    fb = FakeFbD(mode="bootloader", can_enter=False)
    ops = _ops(cfg, NoRootAdb(su=False), fb)
    o = ops.flash_boot(img, by_key("leaf5"), backup=False, force=False,
                       via="fastbootd", wait_seconds=1)
    assert not o.ok
    assert o.stage == "no_fastbootd"
    joined = "\n".join(o.reasons)
    assert "root exploit" in joined and "edl" in joined


def test_flash_boot_via_fastbootd_image_too_big(cfg, tmp_path):
    """镜像比分区的容量大时中止 —— fastbootd 刷进去会截断。"""
    from test_root import make_boot
    img = make_boot(tmp_path / "p.img")
    fb = FakeFbD(mode="fastbootd", part_size="0x1000")    # 4KB，装不下 boot
    ops = _ops(cfg, NoRootAdb(su=False), fb)
    o = ops.flash_boot(img, by_key("leaf5"), backup=False, force=False,
                       via="fastbootd", wait_seconds=1)
    assert not o.ok and o.stage == "image_too_big"
    assert not fb.flashed, "容量不够时一条写命令都不该发"
