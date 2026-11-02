"""boot 镜像校验与刷入流程。用合成 header，不用真机镜像。"""

from __future__ import annotations

import struct

import pytest

from conftest import LEAF5_PROPS, FakeAdb
from l5tk.core.adb import Adb
from l5tk.core.errors import DeviceError, ImageError, Refused, Refused
from l5tk.core.fastboot import Fastboot
from l5tk.core.result import ExecResult
from l5tk.device import by_key
from l5tk.logx import null_logger
from l5tk.ops.root import RootOps, parse_boot

PAGE = 2048


def make_boot(path, kernel_mb=8, ramdisk_mb=4, magic=b"ANDROID!",
              page=PAGE, truncate=0):
    head = bytearray(1632)
    head[0:8] = magic
    struct.pack_into("<I", head, 8, kernel_mb * 1024 * 1024)
    struct.pack_into("<I", head, 16, ramdisk_mb * 1024 * 1024)
    struct.pack_into("<I", head, 36, page)
    struct.pack_into("<I", head, 40, 0)
    struct.pack_into("<I", head, 44, (13 << 14) | (0 << 7) | 0)
    head[48:52] = b"boot"

    def align(n):
        return (n + page - 1) // page * page

    total = page + align(kernel_mb * 1024 * 1024) + align(ramdisk_mb * 1024 * 1024)
    blob = bytes(head) + b"\x00" * (total - len(head) - truncate)
    path.write_bytes(blob)
    return path


def test_parse_ok(tmp_path):
    p = make_boot(tmp_path / "ok.img")
    img = parse_boot(p)
    assert img.kernel_size == 8 * 1024 * 1024
    assert img.page_size == PAGE
    assert img.os_version == "13.0.0"
    assert img.name == "boot"
    assert img.expect_size <= img.size


def test_bad_magic(tmp_path):
    p = make_boot(tmp_path / "bad.img", magic=b"NOTBOOT!")
    with pytest.raises(ImageError) as e:
        parse_boot(p)
    assert "ANDROID!" in e.value.message


def test_truncated(tmp_path):
    p = make_boot(tmp_path / "cut.img", truncate=6 * 1024 * 1024)
    with pytest.raises(ImageError) as e:
        parse_boot(p)
    assert "截断" in e.value.message


def test_bad_page_size(tmp_path):
    p = make_boot(tmp_path / "pg.img", page=1234)
    with pytest.raises(ImageError):
        parse_boot(p)


def test_missing_file(tmp_path):
    with pytest.raises(ImageError):
        parse_boot(tmp_path / "nope.img")


def test_zero_sizes(tmp_path):
    p = tmp_path / "z.img"
    head = bytearray(1632)
    head[0:8] = b"ANDROID!"
    struct.pack_into("<I", head, 36, PAGE)
    p.write_bytes(bytes(head) + b"\x00" * 4096)
    with pytest.raises(ImageError):
        parse_boot(p)


# ---------------- 刷入流程 ----------------
class FakeFb:
    def __init__(self, flash_ok=True, first_flash_fails=False):
        self.flashed: list[tuple[str, str]] = []
        self.flash_ok = flash_ok
        self.first_flash_fails = first_flash_fails
        self.rebooted = False

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
            return ExecResult(argv, 0, "XYZ789\tfastboot\n", "", 2)
        if head == "getvar":
            return ExecResult(argv, 0, "", "(bootloader) slot-count: 1\n", 2)
        if head == "flash":
            self.flashed.append((args[1], args[2]))
            fail = (not self.flash_ok) or (self.first_flash_fails and len(self.flashed) == 1)
            if fail:
                self.flash_ok = True      # 只失败一次，好让回滚那一刷成功
                return ExecResult(argv, 1, "", "FAILED (remote: not allowed)", 3)
            return ExecResult(argv, 0, "Sending 'boot' OKAY\nWriting OKAY\n", "", 4000)
        if head == "reboot":
            self.rebooted = True
            return ExecResult(argv, 0, "", "", 2)
        return ExecResult(argv, 0, "", "", 1)


def _ops(cfg, adb_runner, fb_runner):
    adb = Adb(cfg, runner=adb_runner)
    fb = Fastboot(cfg, runner=fb_runner)
    return RootOps(adb, fb, null_logger())


def test_flash_refused_for_unverified_profile(cfg, tmp_path):
    img = make_boot(tmp_path / "p.img")
    ops = _ops(cfg, FakeAdb(su=True), FakeFb())
    with pytest.raises(DeviceError) as e:
        ops.flash_boot(img, by_key("tab8c"))     # verified=False
    assert "没在真机验证" in e.value.message


def test_flash_refused_when_locked(cfg, tmp_path):
    from conftest import LEAF5_PROPS
    props = dict(LEAF5_PROPS)
    props["ro.boot.flash.locked"] = "1"
    img = make_boot(tmp_path / "p.img")
    ops = _ops(cfg, FakeAdb(props=props, su=True), FakeFb())
    # via=fastboot：锁定检查只属于 bootloader 那条路 —— dd 有 su 就能写，
    # 不看 flash.locked。测锁定就必须显式指名那条通路。
    with pytest.raises(DeviceError) as e:
        ops.flash_boot(img, by_key("leaf5"), via="fastboot")
    assert "锁定" in e.value.message


def test_flash_ok_with_backup(cfg, tmp_path):
    img = make_boot(tmp_path / "p.img")
    fb = FakeFb()
    ops = _ops(cfg, FakeAdb(su=True), fb)
    o = ops.flash_boot(img, by_key("leaf5"), backup=True, wait_seconds=1,
                       via="fastboot")
    # wait_for_adb 用 cfg.adb 起子进程，测试里会失败 → reboot_timeout 也算流程走完
    assert o.stage in ("flashed", "reboot_timeout"), o.stage
    # Leaf5 是 A/B，当前槽位 _b —— 必须刷 boot_b，不是 boot
    assert fb.flashed and fb.flashed[0][0] == "boot_b"
    assert fb.flashed[0][1].endswith("p.img")
    assert o.data.get("backup")


# ---------------- 分区判定 ----------------
def test_resolve_ab_partition(cfg):
    ops = _ops(cfg, FakeAdb(props=LEAF5_PROPS, su=True), FakeFb())
    part, why = ops.resolve_partition(by_key("leaf5"))
    assert part == "boot_b"
    assert "槽位 b" in why


def test_resolve_single_slot_when_no_suffix(cfg):
    props = dict(LEAF5_PROPS)
    props["ro.boot.slot_suffix"] = ""
    fake = FakeAdb(props=props, su=True, partitions=("boot", "vbmeta"))
    ops = _ops(cfg, fake, FakeFb())
    part, why = ops.resolve_partition(by_key("leaf5"))
    assert part == "boot"


def test_resolve_ab_without_slot_refuses(cfg):
    """有 boot_a/boot_b 但读不到当前槽位：不能瞎猜，必须停。"""
    props = dict(LEAF5_PROPS)
    props["ro.boot.slot_suffix"] = ""
    ops = _ops(cfg, FakeAdb(props=props, su=True), FakeFb())
    with pytest.raises(DeviceError) as e:
        ops.resolve_partition(by_key("leaf5"))
    assert "槽位" in e.value.message


def test_resolve_missing_partition_refuses(cfg):
    ops = _ops(cfg, FakeAdb(su=True, partitions=("system", "vendor")), FakeFb())
    with pytest.raises(DeviceError) as e:
        ops.resolve_partition(by_key("leaf5"))
    assert "没有" in e.value.message


def test_resolve_explicit_part_override(cfg):
    ops = _ops(cfg, FakeAdb(su=True), FakeFb())
    part, why = ops.resolve_partition(by_key("leaf5"), part="boot_a")
    assert part == "boot_a"


class NoRootAdb(FakeAdb):
    """user 版构建：su 不存在，adb root 也被拒。"""

    def __call__(self, argv, timeout):
        if "root" in argv:
            return ExecResult(tuple(argv), 0,
                              "adbd cannot run as root in production builds\n", "", 5)
        return super().__call__(argv, timeout)


def test_flash_failure_rolls_back(cfg, tmp_path):
    img = make_boot(tmp_path / "p.img")
    fb = FakeFb(first_flash_fails=True)
    ops = _ops(cfg, FakeAdb(su=True), fb)
    o = ops.flash_boot(img, by_key("leaf5"), backup=True, wait_seconds=1,
                       via="fastboot")
    assert o.stage == "flash_failed_rolled_back"
    assert len(fb.flashed) == 2, "第二次应当是回滚"
    assert "boot_b_backup" in fb.flashed[1][1]


def test_backup_requires_root(cfg):
    ops = _ops(cfg, NoRootAdb(su=False), FakeFb())
    with pytest.raises(DeviceError) as e:
        ops.backup_boot()
    assert "root" in e.value.message


def test_status_no_root(cfg):
    o = _ops(cfg, NoRootAdb(su=False), FakeFb()).status()
    assert not o.ok
    assert o.stage == "none"


# ---------------- 内置 Magisk ----------------

def _cfg_with_apk(cfg, tmp_path, name: str | None = "Magisk-v31.1.apk",
                  payload: bytes = b"PK\x03\x04fake"):
    """把 base_dir/bundle_dir 指到临时目录，控制内置件的有无。

    真实 assets/magisk 里现在确实有 APK —— 测试不能依赖这一点，
    否则打包机上一有文件，"缺资产"那条分支就永远测不到。
    """
    app = tmp_path / "app"
    d = app / "assets" / "magisk"
    d.mkdir(parents=True)
    if name:
        (d / name).write_bytes(payload)
    cfg.base_dir = app
    cfg.bundle_dir = app
    return cfg


def test_magisk_apk_found(cfg, tmp_path):
    _cfg_with_apk(cfg, tmp_path)
    ops = _ops(cfg, FakeAdb(), FakeFb())
    apk = ops.magisk_apk()
    assert apk is not None
    assert apk.name == "Magisk-v31.1.apk"


def test_magisk_apk_missing_is_reported(cfg, tmp_path):
    _cfg_with_apk(cfg, tmp_path, name=None)
    adb = FakeAdb()
    o = _ops(cfg, adb, FakeFb()).install_magisk()
    assert not o.ok
    assert o.stage == "no_asset"
    assert "assets" in o.reasons[0]
    assert not [c for c in adb.calls if "install" in c], "没资产就不该调 adb install"


def test_install_magisk_uses_bundled_apk(cfg, tmp_path):
    _cfg_with_apk(cfg, tmp_path)
    adb = FakeAdb()
    o = _ops(cfg, adb, FakeFb()).install_magisk()
    assert o.ok, o.detail
    assert o.stage == "installed"
    inst = [c for c in adb.calls if "install" in c]
    assert len(inst) == 1
    assert inst[0][-1].endswith("Magisk-v31.1.apk")
    # -r 必须带：重装时不带会返回 INSTALL_FAILED_ALREADY_EXISTS
    assert "-r" in inst[0]
    assert any("magsik" in r or "Magisk" in r for r in o.reasons)


def test_install_magisk_no_reinstall_flag(cfg, tmp_path):
    _cfg_with_apk(cfg, tmp_path)
    adb = FakeAdb()
    o = _ops(cfg, adb, FakeFb()).install_magisk(reinstall=False)
    assert o.ok
    inst = [c for c in adb.calls if "install" in c]
    assert "-r" not in inst[0]


def test_gitkeep_does_not_count_as_asset(tmp_path):
    """空目录只放 .gitkeep 时不能当成有货 —— 否则打出缺 adb 的 EXE。"""
    import build

    d = tmp_path / "bin"
    d.mkdir()
    (d / ".gitkeep").write_bytes(b"")
    assert build._has_real_file(d) is False
    (d / "adb.exe").write_bytes(b"MZ")
    assert build._has_real_file(d) is True


def test_install_magisk_refused_in_dry_run(cfg, tmp_path):
    """dry-run 不该往设备上装东西 —— 装 APK 会覆盖用户已装的同名包。"""
    _cfg_with_apk(cfg, tmp_path)
    cfg.dry_run = True
    adb = FakeAdb()
    with pytest.raises(Refused):
        _ops(cfg, adb, FakeFb()).install_magisk()
    assert not [c for c in adb.calls if "install" in c]


def test_install_magisk_refused_in_dry_run(cfg, tmp_path):
    """dry-run 不该往设备上装东西 —— 装 APK 会覆盖用户已装的同名包。"""
    _cfg_with_apk(cfg, tmp_path)
    cfg.dry_run = True
    adb = FakeAdb()
    with pytest.raises(Refused):
        _ops(cfg, adb, FakeFb()).install_magisk()
    assert not [c for c in adb.calls if "install" in c]
