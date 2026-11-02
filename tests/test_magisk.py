"""Magisk 全自动装机的纯本地逻辑：APK 取工具、services.jar 单字节补丁。

这两块不碰设备，却能一错就砖（补丁字节错了开机卡 logo，工具取错了 patch 直接失败），
所以要单独锁住。
"""

from __future__ import annotations

import hashlib
import struct
import zipfile
import zlib

import pytest

from l5tk.core.errors import DeviceError, ImageError
from l5tk.ops.magisk import (APK_TOOLS, DEX_NEW, DEX_OFF, DEX_OLD,
                             assert_kernel_unchanged, extract_tools,
                             patch_services_jar)


def make_apk(path, drop: str | None = None, names=None):
    """造一个最小 Magisk APK：只需要那 9 个条目存在。"""
    entries = dict(names) if names else {arc: b"ELF" + arc.encode() for arc in APK_TOOLS}
    if drop:
        entries.pop(drop, None)
    with zipfile.ZipFile(path, "w") as z:
        for arc, data in entries.items():
            z.writestr(arc, data)
    return path


def make_dex(code_off: int = DEX_OFF) -> bytes:
    """造一个带合法 dex 头的假 classes.dex，补丁点周围可写。"""
    size = code_off + 0x100
    buf = bytearray(size)
    buf[0:8] = b"dex\n035\x00"
    buf[0x20:] = b"\x00" * (size - 0x20)
    buf[code_off:code_off + 4] = DEX_OLD

    def fix(buf):
        buf[0x0C:0x20] = hashlib.sha1(bytes(buf[32:])).digest()
        buf[0x08:0x0C] = struct.pack("<I", zlib.adler32(bytes(buf[12:])) & 0xFFFFFFFF)

    fix(buf)
    return bytes(buf)


def make_jar(path, dex: bytes):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("classes.dex", dex)
        z.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n")
    return path


# ---------------- APK 取工具 ----------------

def test_extract_all_tools(tmp_path):
    apk = make_apk(tmp_path / "Magisk.apk")
    out = extract_tools(apk, tmp_path / "kits")
    assert set(out) == set(APK_TOOLS.values())
    assert out["boot_patch.sh"].read_bytes().startswith(b"ELF")
    assert out["magiskboot"].is_file()


def test_extract_reports_missing_entry(tmp_path):
    apk = make_apk(tmp_path / "Magisk.apk", drop="assets/boot_patch.sh")
    with pytest.raises(DeviceError) as e:
        extract_tools(apk, tmp_path / "kits")
    assert "boot_patch.sh" in e.value.message


def test_extract_not_a_magisk_apk(tmp_path):
    apk = make_apk(tmp_path / "x.apk", names={"foo.txt": b"hi"})
    with pytest.raises(DeviceError) as e:
        extract_tools(apk, tmp_path / "kits")
    assert "9" in e.value.message or "缺" in e.value.message


# ---------------- services.jar 补丁 ----------------

def _dex_payload(jar_path) -> bytes:
    with zipfile.ZipFile(jar_path) as z:
        return z.read("classes.dex")


def test_patch_services_jar_rewrites_branch(tmp_path):
    src = make_jar(tmp_path / "services.jar", make_dex())
    dst = tmp_path / "out.jar"
    got, changed = patch_services_jar(src, dst)
    assert changed is True
    dex = _dex_payload(got)
    assert dex[DEX_OFF:DEX_OFF + 4] == DEX_NEW


def test_patch_services_jar_fixes_both_checksums(tmp_path):
    """DEX 头两个校验不动的话 ART 直接拒绝加载 —— 表现是开机卡 logo。"""
    src = make_jar(tmp_path / "services.jar", make_dex())
    dst = tmp_path / "out.jar"
    patch_services_jar(src, dst)
    dex = _dex_payload(dst)
    assert dex[0x0C:0x20] == hashlib.sha1(dex[32:]).digest(), "signature 没重算"
    want = struct.pack("<I", zlib.adler32(dex[12:]) & 0xFFFFFFFF)
    assert dex[0x08:0x0C] == want, "checksum 没重算"


def test_patch_services_jar_keeps_other_entries(tmp_path):
    src = make_jar(tmp_path / "services.jar", make_dex())
    dst = tmp_path / "out.jar"
    patch_services_jar(src, dst)
    with zipfile.ZipFile(dst) as z:
        assert "META-INF/MANIFEST.MF" in z.namelist()


def test_patch_services_jar_idempotent(tmp_path):
    src = make_jar(tmp_path / "services.jar", make_dex())
    first = tmp_path / "a.jar"
    patch_services_jar(src, first)
    second = tmp_path / "b.jar"
    _got, changed = patch_services_jar(first, second)
    assert changed is False, "已经是补丁后的状态不该重复改"
    assert _dex_payload(second)[DEX_OFF:DEX_OFF + 4] == DEX_NEW


def test_patch_services_jar_rejects_unknown_firmware(tmp_path):
    """偏移只对 4.2.3-rel_0928 成立，别的固件必须拦住而不是硬改。"""
    dex = bytearray(make_dex())
    dex[DEX_OFF:DEX_OFF + 4] = b"\x11\x22\x33\x44"
    src = make_jar(tmp_path / "services.jar", bytes(dex))
    with pytest.raises(ImageError) as e:
        patch_services_jar(src, tmp_path / "out.jar")
    assert "重算" in str(e.value.hint) or "固件" in e.value.message


def test_patch_services_jar_needs_classes_dex(tmp_path):
    src = tmp_path / "services.jar"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("other.txt", b"x")
    with pytest.raises(ImageError) as e:
        patch_services_jar(src, tmp_path / "out.jar")
    assert "classes.dex" in e.value.message


# ---------------- 写回前的闸 ----------------

class _Img:
    def __init__(self, kernel_size, ramdisk_size=4 << 20):
        self.kernel_size = kernel_size
        self.ramdisk_size = ramdisk_size


def test_kernel_same_passes():
    assert_kernel_unchanged(_Img(20 << 20), _Img(20 << 20, 6 << 20))


def test_kernel_changed_blocks_writeback():
    """kernel 段长度变了就是拿错分区或补丁工具不对，写进去必砖。"""
    with pytest.raises(DeviceError) as e:
        assert_kernel_unchanged(_Img(20 << 20), _Img(21 << 20))
    assert "禁止写入" in e.value.message
    assert "分区未改动" in e.value.message


# ---------------- AMS 模块：失败不能拖垮已写好的 boot ----------------

def _ops_without_device():
    """不走 __init__（那要真 adb），只拿到一个能调 _module_or_skip 的壳。"""
    from l5tk.logx import null_logger
    from l5tk.ops.magisk import MagiskOps

    ops = MagiskOps.__new__(MagiskOps)
    ops.log = null_logger()
    return ops


def test_module_patch_mismatch_is_skipped_not_fatal():
    """补丁点字节不符时降级成「跳过」。

    Tab 三台是 Android 11 framework，0x2b2fee 上不可能是 DEX_OLD。走到这一步
    boot 已经写回并回读校验过，把整条链判失败等于告诉用户「刷机失败」——
    实际 root 马上就会生效，只是 Magisk App 还会崩。
    """
    ops = _ops_without_device()

    def boom():
        raise ImageError("0x%x 处不是预期字节（读到 38 01 3f 00，期望 %s）"
                         % (DEX_OFF, DEX_OLD.hex(" ")),
                         hint="这个偏移只对固件 4.2.3-rel_0928 成立")

    ops._prepare_module = boom
    msg = ops._module_or_skip()
    assert "跳过" in msg
    assert "0x%x" % DEX_OFF in msg


def test_module_ok_passes_message_through():
    ops = _ops_without_device()
    ops._prepare_module = lambda: "AMS 修复模块已铺 boox-ams-fix"
    assert ops._module_or_skip() == "AMS 修复模块已铺 boox-ams-fix"


# ---------------- AMS 模块：失败不能拖垮已写好的 boot ----------------

def _ops_without_device():
    """不走 __init__（那要真 adb），只拿到一个能调 _module_or_skip 的壳。"""
    from l5tk.logx import null_logger
    from l5tk.ops.magisk import MagiskOps

    ops = MagiskOps.__new__(MagiskOps)
    ops.log = null_logger()
    return ops


def test_module_patch_mismatch_is_skipped_not_fatal():
    """补丁点字节不符时降级成「跳过」。

    Tab 三台是 Android 11 framework，0x2b2fee 上不可能是 DEX_OLD。走到这一步
    boot 已经写回并回读校验过，把整条链判失败等于告诉用户「刷机失败」——
    实际 root 马上就会生效，只是 Magisk App 还会崩。
    """
    ops = _ops_without_device()

    def boom():
        raise ImageError("0x%x 处不是预期字节（读到 38 01 3f 00，期望 %s）"
                         % (DEX_OFF, DEX_OLD.hex(" ")),
                         hint="这个偏移只对固件 4.2.3-rel_0928 成立")

    ops._prepare_module = boom
    msg = ops._module_or_skip()
    assert "跳过" in msg
    assert "0x%x" % DEX_OFF in msg


def test_module_ok_passes_message_through():
    ops = _ops_without_device()
    ops._prepare_module = lambda: "AMS 修复模块已铺 boox-ams-fix"
    assert ops._module_or_skip() == "AMS 修复模块已铺 boox-ams-fix"
