"""一键 root 的分支覆盖。

测的是「选了哪条路、为什么放弃上一条」，不测真机行为 ——
dd 写块设备、fastboot flash 都由各自模块的用例覆盖。
"""

from __future__ import annotations

import pytest

from conftest import LEAF5_PROPS, TAB8C_PROPS, FakeAdb
from l5tk.core.adb import Adb
from l5tk.core.fastboot import Fastboot
from l5tk.core.result import ExecResult
from l5tk.logx import null_logger
from l5tk.ops.autoroot import AutoRoot


class FakeFb:
    def __init__(self, online=False, flash_ok=True):
        self.online = online
        self.flash_ok = flash_ok
        self.flashed: list[tuple[str, str]] = []

    def __call__(self, argv, timeout):
        argv = tuple(str(a) for a in argv)
        args = list(argv[1:])
        if args[:1] == ["-s"]:
            args = args[2:]
        head = args[0] if args else ""
        if head == "devices" and not str(argv[0]).lower().endswith("fastboot.exe") \
                and "fastboot" not in str(argv[0]).lower():
            # wait_for_adb 会借同一个 runner 发 `adb devices`：不认出来就会
            # 一直匹配不到 `\tdevice`，干等满 180 秒
            return ExecResult(argv, 0, "List of devices attached\nABCD1234\tdevice\n", "", 2)
        if head == "devices":
            if not self.online:
                return ExecResult(argv, 0, "", "", 2)
            return ExecResult(argv, 0, "XYZ789\tfastboot\n", "", 2)
        if head == "getvar":
            return ExecResult(argv, 0, "", "(bootloader) slot-count: 2\n", 2)
        if head == "flash":
            self.flashed.append((args[1], args[2]))
            if not self.flash_ok:
                return ExecResult(argv, 1, "", "FAILED (remote: 'unknown command')", 3)
            return ExecResult(argv, 0, "Sending OKAY\nWriting OKAY\n", "", 3000)
        if head == "reboot":
            return ExecResult(argv, 0, "", "", 2)
        return ExecResult(argv, 0, "", "", 1)


def _auto(cfg, adb_runner, fb_runner=None):
    adb = Adb(cfg, runner=adb_runner)
    fb = Fastboot(cfg, runner=fb_runner or FakeFb())
    return AutoRoot(adb, fb, null_logger())


def _boot_image(path, kernel_mb=8, ramdisk_mb=4, page=2048):
    """合成一个 header 合法的 boot 镜像，够 parse_boot 过闸。"""
    import struct

    head = bytearray(1632)
    head[0:8] = b"ANDROID!"
    struct.pack_into("<I", head, 8, kernel_mb * 1024 * 1024)
    struct.pack_into("<I", head, 16, ramdisk_mb * 1024 * 1024)
    struct.pack_into("<I", head, 36, page)
    struct.pack_into("<I", head, 40, 0)
    struct.pack_into("<I", head, 44, (13 << 14))
    head[48:52] = b"boot"

    def align(n):
        return (n + page - 1) // page * page

    total = page + align(kernel_mb * 1024 * 1024) + align(ramdisk_mb * 1024 * 1024)
    path.write_bytes(bytes(head) + b"\x00" * (total - len(head)))
    return path


def _boot_image(path, kernel_mb=8, ramdisk_mb=4, page=2048):
    """合成一个 header 合法的 boot 镜像，够 parse_boot 过闸。"""
    import struct

    head = bytearray(1632)
    head[0:8] = b"ANDROID!"
    struct.pack_into("<I", head, 8, kernel_mb * 1024 * 1024)
    struct.pack_into("<I", head, 16, ramdisk_mb * 1024 * 1024)
    struct.pack_into("<I", head, 36, page)
    struct.pack_into("<I", head, 40, 0)
    struct.pack_into("<I", head, 44, (13 << 14))
    head[48:52] = b"boot"

    def align(n):
        return (n + page - 1) // page * page

    total = page + align(kernel_mb * 1024 * 1024) + align(ramdisk_mb * 1024 * 1024)
    path.write_bytes(bytes(head) + bytes(total - len(head)))
    return path


PROPS_USER = dict(LEAF5_PROPS, **{"ro.build.type": "user"})
PROPS_DEBUG = dict(LEAF5_PROPS, **{"ro.build.type": "userdebug",
                                   "ro.debuggable": "1"})


def test_already_rooted_stops_before_touching_anything(cfg):
    """已经 root 的机器再走一遍是全流程里唯一能把好机器搞砖的操作。"""
    adb = FakeAdb(props=PROPS_USER, su=True, magisk="31.1:MAGISK:R")
    r = _auto(cfg, adb).run()
    assert r.ok and r.stage == "already_rooted"
    assert not [c for c in adb.calls if "root" == (c[1] if len(c) > 1 else "")], \
        "已 root 就不该再发 adb root"
    assert not [c for c in adb.calls if "dd" in " ".join(c)]


def test_userdebug_takes_adb_root(cfg):
    adb = FakeAdb(props=PROPS_DEBUG)
    r = _auto(cfg, adb).run()
    assert r.ok and r.stage == "adb_root"
    assert any("root" in c for c in adb.calls)


def test_no_root_no_su_reports_why_and_next_step(cfg):
    """裸机：user 版、没 su、没给镜像 —— 必须报三条原因，不能假装成功。"""
    adb = FakeAdb(props=PROPS_USER, su=False)
    r = _auto(cfg, adb).run()
    assert not r.ok and r.stage == "no_entry"
    assert "没有 su" in r.detail
    assert "--image" in r.detail
    assert not [c for c in adb.calls if "dd" in " ".join(c)]


def test_locked_bootloader_is_called_out(cfg):
    props = dict(PROPS_USER, **{"ro.boot.flash.locked": "1"})
    r = _auto(cfg, FakeAdb(props=props, su=False)).run()
    assert not r.ok
    assert "flash.locked=1" in r.detail
    assert "flashing unlock" in r.detail


def test_su_alone_counts_as_rooted(cfg):
    """有 su 的机器 status() 就算 rooted —— 一键不能在已 root 的机器上重写 boot。"""
    adb = FakeAdb(props=PROPS_USER, su=True)
    r = _auto(cfg, adb).run()
    assert r.ok and r.stage == "already_rooted"
    assert not [c for c in adb.calls if "dd" in " ".join(c)]


def test_fastboot_fallback_when_image_given(cfg, tmp_path):
    """user 版 + 没 su + 给了镜像：只能靠 fastboot，且要真的刷。"""
    img = _boot_image(tmp_path / "patched.img")
    fb = FakeFb(online=True)
    r = _auto(cfg, FakeAdb(props=PROPS_USER, su=False), fb).run(image=str(img))
    assert r.ok and r.stage == "fastboot"
    assert fb.flashed and fb.flashed[0][1].endswith("patched.img")


def test_fastboot_failure_is_reported_not_swallowed(cfg, tmp_path):
    """abl 裁掉 flash 时返回 remote: unknown command —— 这句必须原样传出来。"""
    img = _boot_image(tmp_path / "patched.img")
    fb = FakeFb(online=True, flash_ok=False)
    r = _auto(cfg, FakeAdb(props=PROPS_USER, su=False), fb).run(image=str(img))
    assert not r.ok
    assert any("unknown command" in s.detail for s in r.steps), \
        [s.detail for s in r.steps]


def test_fastboot_skipped_when_disabled(cfg):
    fb = FakeFb(online=True)
    r = _auto(cfg, FakeAdb(props=PROPS_USER, su=False), fb).run(
        image="x.img", allow_fastboot=False)
    assert not r.ok
    assert not fb.flashed


def test_fastboot_not_attempted_without_image(cfg):
    fb = FakeFb(online=True)
    r = _auto(cfg, FakeAdb(props=PROPS_USER, su=False), fb).run()
    assert not r.ok
    assert not fb.flashed
    assert any("没给镜像" in s.detail for s in r.steps)


def test_dry_run_lists_plan_and_touches_nothing(cfg):
    cfg.dry_run = True
    adb = FakeAdb(props=PROPS_DEBUG)
    r = _auto(cfg, adb).run()
    assert r.stage == "dry_run" and r.ok
    assert not [c for c in adb.calls if len(c) > 1 and c[1] == "root"], \
        "dry-run 下 adb root 会重启 adbd，不能真发"
    assert any("adb root" in s.detail for s in r.steps)
