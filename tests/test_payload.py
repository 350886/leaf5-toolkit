"""payload.bin 解析的离线测试：合成小包，不读真固件。

为什么不直接测 firmware/ 里的包：那是 1.8GB 的固件，不会跟着仓库走。
合成包能精确控制「每段操作该解出什么字节」，比「抽出来能打开」更能
证明解析对 —— 尤其是 data_offset 相对 blobs 起点、extents 定位这两处，
错了在真包上只会表现成「镜像打不开」，看不出是哪一步偏了。

真固件那几条用 skipif 挂在最后：有包就跑，没包就跳，不阻塞别的机器。
"""

from __future__ import annotations

import bz2
import lzma
import struct
import zipfile
from pathlib import Path

import pytest

from l5tk.core.errors import ImageError
from l5tk.core.payload import (
    OP_DISCARD, OP_MOVE, OP_REPLACE, OP_REPLACE_BZ, OP_REPLACE_XZ, OP_ZERO,
    Payload, find_ota,
)

BLK = 4096


# ---------------------------------------------------------------- 编码器 --
def vint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def fvar(no: int, val: int) -> bytes:
    return vint(no << 3) + vint(val)


def flen(no: int, data: bytes) -> bytes:
    return vint((no << 3) | 2) + vint(len(data)) + data


def mk_op(type_: int, offset: int = 0, length: int = 0,
          extents: tuple[tuple[int, int], ...] = ()) -> bytes:
    b = fvar(1, type_)
    if offset or length:
        b += fvar(2, offset) + fvar(3, length)
    for start, n in extents:
        b += flen(6, fvar(1, start) + fvar(2, n))
    return b


def mk_part(name: str, ops: list[bytes], size: int = 0) -> bytes:
    b = flen(1, name.encode())
    if size:
        b += flen(7, fvar(1, size))
    for o in ops:
        b += flen(8, o)
    return b


def build(parts: list[bytes], blobs: list[bytes], version: int = 2,
          block: int = BLK) -> bytes:
    man = fvar(3, block) + b"".join(flen(13, p) for p in parts)
    head = b"CrAU" + struct.pack(">Q", version) + struct.pack(">Q", len(man))
    if version >= 2:
        head += struct.pack(">I", 0)       # metadata_signature_size = 0
    return head + man + b"".join(blobs)


def write(path: Path, data: bytes, as_zip: bool = False) -> Path:
    if as_zip:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("payload.bin", data)
            z.writestr("payload_properties.txt", "FILE_SIZE=%d\n" % len(data))
    else:
        path.write_bytes(data)
    return path


def page(ch: bytes, n: int = BLK) -> bytes:
    return (ch * n)[:n]


# ---------------------------------------------------------------- 正常路径 --
def test_single_replace_raw(tmp_path):
    body = page(b"A")
    data = build([mk_part("boot", [mk_op(OP_REPLACE, 0, len(body))])], [body])
    p = Payload(write(tmp_path / "p.bin", data))
    out = tmp_path / "boot.img"
    info = p.extract("boot", out)
    assert info["bytes"] == BLK
    assert out.read_bytes() == body
    p.close()


def test_replace_bz(tmp_path):
    raw = page(b"B") * 2
    packed = bz2.compress(raw)
    data = build([mk_part("boot", [mk_op(OP_REPLACE_BZ, 0, len(packed))])], [packed])
    p = Payload(write(tmp_path / "p.bin", data))
    out = tmp_path / "boot.img"
    p.extract("boot", out)
    assert out.read_bytes() == raw
    p.close()


def test_replace_xz(tmp_path):
    raw = page(b"C") * 3
    packed = lzma.compress(raw)
    data = build([mk_part("boot", [mk_op(OP_REPLACE_XZ, 0, len(packed))])], [packed])
    p = Payload(write(tmp_path / "p.bin", data))
    out = tmp_path / "boot.img"
    p.extract("boot", out)
    assert out.read_bytes() == raw
    p.close()


def test_mixed_ops_in_order(tmp_path):
    """三段不同压缩方式顺序拼接 —— data_offset 是相对 blobs 起点的，
    第二段的 offset 必须等于第一段的数据长度（写成文件绝对偏移就会错位）。"""
    a, b, c = page(b"1"), page(b"2"), page(b"3")
    pa, pb, pc = a, bz2.compress(b), lzma.compress(c)
    ops = [mk_op(OP_REPLACE, 0, len(pa)),
           mk_op(OP_REPLACE_BZ, len(pa), len(pb)),
           mk_op(OP_REPLACE_XZ, len(pa) + len(pb), len(pc))]
    data = build([mk_part("boot", ops, size=3 * BLK)], [pa, pb, pc])
    p = Payload(write(tmp_path / "p.bin", data))
    out = tmp_path / "boot.img"
    p.extract("boot", out)
    assert out.read_bytes() == a + b + c
    p.close()


def test_extents_place_data_at_block(tmp_path):
    """带 dst_extents 时按目标块号写，不按顺序追加。

    这是 LZ4DIFF 那类包的行为；顺序追加会把后一段写到前一段后面，
    最终镜像的尾部全是错位数据 —— 但文件长度对，看上去「成功」。
    """
    a, b = page(b"X"), page(b"Y")
    ops = [mk_op(OP_REPLACE, 0, len(a), extents=((0, 1),)),
           mk_op(OP_REPLACE, len(a), len(b), extents=((3, 1),))]
    data = build([mk_part("boot", ops, size=4 * BLK)], [a, b])
    p = Payload(write(tmp_path / "p.bin", data))
    out = tmp_path / "boot.img"
    p.extract("boot", out)
    got = out.read_bytes()
    assert len(got) == 4 * BLK
    assert got[:BLK] == a
    assert got[3 * BLK:4 * BLK] == b
    assert got[BLK:2 * BLK] == b"\0" * BLK       # 中间那段是空洞
    p.close()


def test_zero_op_fills_blocks(tmp_path):
    ops = [mk_op(OP_ZERO, 0, 0, extents=((0, 2),))]
    data = build([mk_part("boot", ops, size=2 * BLK)], [])
    p = Payload(write(tmp_path / "p.bin", data))
    out = tmp_path / "boot.img"
    p.extract("boot", out)
    assert out.read_bytes() == b"\0" * (2 * BLK)
    p.close()


def test_discard_op_fills_blocks(tmp_path):
    ops = [mk_op(OP_DISCARD, 0, 0, extents=((0, 1),))]
    data = build([mk_part("boot", ops, size=BLK)], [])
    p = Payload(write(tmp_path / "p.bin", data))
    out = tmp_path / "boot.img"
    p.extract("boot", out)
    assert out.read_bytes() == b"\0" * BLK
    p.close()


def test_zip_stored_payload(tmp_path):
    """update.zip 里的 payload.bin 是 STORED —— 走 _Slice 按绝对偏移读，
    不走 zipfile.open（那要解压 1.8GB）。"""
    body = page(b"Z")
    data = build([mk_part("boot", [mk_op(OP_REPLACE, 0, len(body))])], [body])
    z = write(tmp_path / "u.zip", data, as_zip=True)
    p = Payload(z)
    out = tmp_path / "boot.img"
    p.extract("boot", out)
    assert out.read_bytes() == body
    p.close()


def test_find_boot_prefers_init_boot(tmp_path):
    """Android 13 起 ramdisk 搬到了 init_boot：两个都在时必须选 init_boot，
    打 boot 会得到没有 ramdisk 的镜像，刷上去直接不进系统。"""
    a, b = page(b"b"), page(b"i")
    parts = [mk_part("boot", [mk_op(OP_REPLACE, 0, len(a))]),
             mk_part("init_boot", [mk_op(OP_REPLACE, 0, len(b))])]
    data = build(parts, [a, b])
    p = Payload(write(tmp_path / "p.bin", data))
    assert p.find_boot().name == "init_boot"
    p.close()


def test_extract_boot_names_output(tmp_path):
    body = page(b"Q")
    data = build([mk_part("boot", [mk_op(OP_REPLACE, 0, len(body))])], [body])
    p = Payload(write(tmp_path / "p.bin", data))
    info = p.extract_boot(tmp_path / "out")
    assert Path(info["path"]).name == "boot_stock.img"
    assert info["is_init_boot"] is False
    p.close()


# ---------------------------------------------------------------- 错误路径 --
def test_incremental_op_rejected(tmp_path):
    """增量 OTA 里 boot 可能是 MOVE/BSDIFF —— 没有旧镜像算不出来，
    硬解出来的东西必然是坏的，必须在写文件之前就停。"""
    ops = [mk_op(OP_MOVE, 0, BLK, extents=((0, 1),))]
    data = build([mk_part("boot", ops, size=BLK)], [])
    p = Payload(write(tmp_path / "p.bin", data))
    with pytest.raises(ImageError) as e:
        p.extract("boot", tmp_path / "boot.img")
    assert "增量" in e.value.message
    assert not (tmp_path / "boot.img").exists(), "报错后不能留下半成品"
    p.close()


def test_missing_partition_lists_names(tmp_path):
    body = page(b"A")
    data = build([mk_part("boot", [mk_op(OP_REPLACE, 0, len(body))])], [body])
    p = Payload(write(tmp_path / "p.bin", data))
    with pytest.raises(ImageError) as e:
        p.extract("vbmeta", tmp_path / "x.img")
    assert "没有分区 vbmeta" in e.value.message
    assert "boot" in e.value.hint
    p.close()


def test_short_result_rejected(tmp_path):
    """manifest 声明 8192，实际只解出 4096 —— 说明解析偏了，
    交出去会被当成合法镜像刷进设备。"""
    body = page(b"A")
    data = build([mk_part("boot", [mk_op(OP_REPLACE, 0, len(body))], size=2 * BLK)],
                 [body])
    p = Payload(write(tmp_path / "p.bin", data))
    with pytest.raises(ImageError) as e:
        p.extract("boot", tmp_path / "boot.img")
    assert "manifest 声明" in e.value.message
    assert not (tmp_path / "boot.img").exists()
    p.close()


def test_bad_magic(tmp_path):
    f = tmp_path / "p.bin"
    f.write_bytes(b"NOTPAYLOAD" + b"\0" * 64)
    with pytest.raises(ImageError) as e:
        Payload(f)
    assert "魔数" in e.value.message


def test_upx_refused(tmp_path):
    f = tmp_path / "x.upx"
    f.write_bytes(b"\xea\x17\x1f37" + b"\0" * 64)
    with pytest.raises(ImageError) as e:
        Payload(f)
    assert "加密容器" in e.value.message


def test_missing_file(tmp_path):
    with pytest.raises(ImageError) as e:
        Payload(tmp_path / "nope.zip")
    assert "找不到固件包" in e.value.message


def test_zip_without_payload(tmp_path):
    z = tmp_path / "bad.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("readme.txt", "hi")
    with pytest.raises(ImageError) as e:
        Payload(z)
    assert "里没有 payload.bin" in e.value.message


def test_truncated_data_rejected(tmp_path):
    """数据段被截断（zip 没下完）时 read_at 必须报错，不能返回短字节。"""
    body = page(b"A")
    data = build([mk_part("boot", [mk_op(OP_REPLACE, 0, len(body))])], [body])
    f = tmp_path / "p.bin"
    f.write_bytes(data[:len(data) - 100])
    p = Payload(f)
    with pytest.raises(ImageError) as e:
        p.extract("boot", tmp_path / "boot.img")
    assert "越界" in e.value.message or "不满" in e.value.message
    p.close()


def test_find_ota_only_zip(tmp_path):
    (tmp_path / "a.zip").write_bytes(b"x" * 100)
    (tmp_path / "b.upx").write_bytes(b"x" * 200)
    (tmp_path / "c.bin").write_bytes(b"x" * 300)
    got = [p.name for p in find_ota(tmp_path)]
    assert got == ["a.zip"], "只认 .zip；.upx 是加密容器，列出来只会误导"


# ------------------------------------------------------- 真固件（可选）-----
FW = Path(__file__).resolve().parent.parent.parent / "firmware"
OTAS = sorted(FW.glob("*_update.zip")) if FW.is_dir() else []


@pytest.mark.skipif(not OTAS, reason="本机没有 firmware/*_update.zip")
@pytest.mark.parametrize("ota", OTAS, ids=[p.name.split("_")[0] for p in OTAS])
def test_real_firmware_parses(ota, tmp_path):
    """真固件集成验证：四台设备都应该是 12~13 个分区、boot 96MB、ANDROID! 魔数。

    「能解析」不够 —— 抽出来的镜像必须是合法 boot（魔数 + 长度等于 manifest
    声明），否则拿它去刷就是刷砖。
    """
    with Payload(ota) as p:
        assert 10 <= len(p.manifest.partitions) <= 20
        assert p.manifest.block_size == 4096
        boot = p.find_boot()
        assert boot is not None
        assert boot.size == 100663296, "ONYX 4.2 的 boot 分区是 96MB"
        # 写到 tmp_path 而不是 firmware/ 旁边：测试中途被打断时，
        # 1.8GB 的固件目录里留一个 96MB 的半成品，下次跑容易被误认成固件
        out = tmp_path / ("%s_stock.img" % ota.stem.split("_")[0])
        info = p.extract(boot.name, out)
        try:
            assert info["bytes"] == boot.size
            with open(info["path"], "rb") as f:
                assert f.read(8) == b"ANDROID!"
        finally:
            out.unlink(missing_ok=True)
