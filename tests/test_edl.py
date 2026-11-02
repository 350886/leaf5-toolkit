"""EDL 写分区的离线测试：不连 9008、不碰真设备，只验会话层的判断逻辑。

要覆盖的几条，都是「错一点就刷砖」的地方：
  1. 分区名写错 → 报 no_partition 并给相近名字，一条写命令都不发
  2. 镜像比分区的容量大 → too_large，不写
  3. 备份长度不足 → backup_short，在写入之前中止
  4. 读回 sha256 不符 → 用备份回写，stage=readback_mismatch_rolled_back
  5. 正常 → stage=flashed，verified=True

跑法：python -m pytest tests/test_edl.py
"""

from __future__ import annotations

import hashlib
import os

import pytest

from l5tk.core import edllink
from l5tk.logx import null_logger
from l5tk.ops.edl import EdlOps, sha256_of

GPT = """\
Parsing Lun 0 GPT
  boot_a:              Offset 0x0000000000001000, Length 0x0000000006000000, Active False
  boot_b:              Offset 0x0000000006002000, Length 0x0000000006000000, Active True
  super:               Offset 0x0000000010004000, Length 0x0000000060000000, Active False
"""

BOOT_LEN = 0x6000000          # 96 MB


class _Cfg:
    timeout_short = 10

    def __init__(self, work):
        self.work_dir = work


class FakeAdb:
    def __init__(self, work):
        self.cfg = _Cfg(work)

    def getprop(self, key):
        return "_b" if key == "ro.boot.slot_suffix" else ""

    def sh(self, *a, **kw):
        raise AssertionError("测试里不该真跑 adb：%s" % (a,))


class _Firehose:
    class cfg:
        SECTOR_SIZE_IN_BYTES = 512


class FakeSession:
    """顶替 edllink.Session：read 吐脚本里的内容，write 记账。"""

    script: list[bytes] = []
    writes: list[tuple[str, str]] = []

    def __init__(self, loader=None):
        self.fh = type("FH", (), {"firehose": _Firehose})()
        self.n = 0

    def open(self, on_line=None, on_progress=None, timeout=90):
        return True

    def printgpt(self, on_line=None, on_progress=None, timeout=120):
        for line in GPT.splitlines():
            if on_line:
                on_line(line)
        return True

    def read(self, part, out_path, on_line=None, on_progress=None,
             timeout=900, place=None, expect=None, chunk=0, attempts=0):
        data = FakeSession.script[self.n] if self.n < len(FakeSession.script) else b""
        self.n += 1
        with open(out_path, "wb") as f:
            f.write(data)
        return out_path

    def write(self, part, image, on_line=None, on_progress=None, timeout=1800):
        FakeSession.writes.append((part, os.path.abspath(image)))
        return dict(partition=part, size=os.path.getsize(image),
                    sectors=1, seconds=0.1)

    def reset(self, on_line=None, timeout=60):
        return True

    def close(self):
        pass


@pytest.fixture
def ops(tmp_path, monkeypatch):
    monkeypatch.setattr(edllink, "Session", FakeSession)
    monkeypatch.setattr(edllink, "find_9008", lambda: [(0x9008, "9008 EDL", "")])
    # _ensure_edl 里会问驱动状态，测试里直接当就绪
    from l5tk.core import drivers
    monkeypatch.setattr(drivers, "state", lambda: {"usable": True, "present": True,
                                                   "problem": 0, "desc": "", "inst": ""})
    monkeypatch.setattr(drivers, "summary", lambda st=None: "9008 已就绪")
    o = EdlOps(FakeAdb(tmp_path), null_logger())
    FakeSession.script = []
    FakeSession.writes = []
    yield o
    FakeSession.script = []
    FakeSession.writes = []


def _img(tmp_path, n=8192, byte=b"\xA5"):
    p = tmp_path / "boot.img"
    p.write_bytes(byte * n)
    return p


# ---------------------------------------------------------------- 基础
def test_sha256_prefix_ignores_tail(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x01" * 3000 + b"\x02" * 500)
    assert sha256_of(p, limit=3000) == hashlib.sha256(b"\x01" * 3000).hexdigest()


def test_status_without_loader_is_explicit(monkeypatch):
    """缺 programmer 时要说缺什么，不能让用户以为 9008 坏了。"""
    from l5tk.core import edllink as el
    monkeypatch.setattr(el, "loader_path", lambda: None)
    monkeypatch.setattr(el, "find_9008", lambda: [(0x9008, "9008 EDL", "")])
    o = EdlOps(FakeAdb("/tmp"), null_logger())
    r = o.status()
    assert r.ok is False and r.stage == "no_loader"
    assert any("BENGAL" in x for x in r.reasons)


def test_status_not_in_edl(tmp_path, monkeypatch):
    from l5tk.core import edllink as el
    monkeypatch.setattr(el, "loader_path", lambda: __file__)
    monkeypatch.setattr(el, "find_9008", lambda: [])
    o = EdlOps(FakeAdb(tmp_path), null_logger())
    r = o.status()
    assert r.stage == "not_in_edl"


# ---------------------------------------------------------------- 前置
def test_flash_missing_image(ops, tmp_path):
    from l5tk.core.errors import ImageError
    with pytest.raises(ImageError):
        ops.flash(tmp_path / "nope.img")


def test_flash_empty_image(ops, tmp_path):
    from l5tk.core.errors import ImageError
    p = tmp_path / "empty.img"
    p.write_bytes(b"")
    with pytest.raises(ImageError):
        ops.flash(p)


# ---------------------------------------------------------------- 分区
def test_partition_name_typo(ops, tmp_path):
    src = _img(tmp_path)
    o = ops.flash(src, part="bootb")
    assert o.stage == "no_partition"
    assert any("boot_b" in x or "boot_a" in x for x in o.reasons)
    assert FakeSession.writes == []


def test_image_too_large(ops, tmp_path):
    p = tmp_path / "big.img"
    with open(p, "wb") as f:
        f.truncate(BOOT_LEN + 512)
    o = ops.flash(p, part="boot_b")
    assert o.stage == "too_large"
    assert any("未写入" in x for x in o.reasons)
    assert FakeSession.writes == []


# ---------------------------------------------------------------- 全流程
def test_flash_ok(ops, tmp_path):
    src = _img(tmp_path)
    body = src.read_bytes()
    padded = body + b"\x00" * (512 - len(body) % 512)
    FakeSession.script = [b"\x00" * BOOT_LEN, padded]

    o = ops.flash(src, part="boot_b")
    assert o.ok and o.stage == "flashed"
    assert o.data["verified"] is True
    assert len(FakeSession.writes) == 1
    assert FakeSession.writes[0][0] == "boot_b"
    assert os.path.isfile(o.data["backup"])


def test_flash_readback_mismatch_rolls_back(ops, tmp_path):
    src = _img(tmp_path)
    FakeSession.script = [b"\x11" * BOOT_LEN, b"\x22" * 8192]

    o = ops.flash(src, part="boot_b")
    assert o.ok is False
    assert o.stage == "readback_mismatch_rolled_back"
    # 第二次写必须是备份，不是源镜像
    assert len(FakeSession.writes) == 2
    assert "stock_boot_b_" in os.path.basename(FakeSession.writes[1][1])


def test_backup_short_aborts_before_write(ops, tmp_path):
    src = _img(tmp_path, 4096)
    FakeSession.script = [b"\x00" * 4096]        # 备份只有 4KB

    o = ops.flash(src, part="boot_b")
    assert o.stage == "backup_short"
    assert FakeSession.writes == []


def test_part_from_slot_falls_back(monkeypatch, tmp_path):
    """读不到 slot_suffix 时按 _b，并留下 warn 痕迹（不是静默）。"""
    class NoSlot(FakeAdb):
        def getprop(self, key):
            return ""
    o = EdlOps(NoSlot(tmp_path), null_logger())
    assert o._part_from_slot() == "boot_b"


def test_slot_parsed(ops):
    assert ops._part_from_slot() == "boot_b"
