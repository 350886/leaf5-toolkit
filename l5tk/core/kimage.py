"""内核镜像：切段、解压、认版本、找镜像、导出符号。

这一层只碰文件与字节，不碰 target.h、不碰编译 —— 那些在 ops/adapt.py。
从 V1 adapter.py 拆出来，去掉了 V1 里写死的 %LOCALAPPDATA%\\Leaf5RootTool，
路径由调用方（Config.adapt_root）传进来。
"""

from __future__ import annotations

import bz2
import gzip
import json
import lzma
import os
import re
import struct
import subprocess
import tempfile
import time
import zlib
from pathlib import Path

from . import elfsyms
from .errors import ImageError

BOOT_MAGIC = b"ANDROID!"

# 解压魔数 → 算法名。顺序即尝试顺序（gzip 最常见，放最前）
MAGICS = [
    (b"\x1f\x8b\x08", "gzip"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"\x5d\x00\x00\x80", "lzma"),
    (b"\x5d\x00\x00\x00", "lzma"),
    (b"\x02\x21\x4c\x18", "lz4legacy"),
    (b"\x04\x22\x4d\x18", "lz4frame"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"BZh", "bzip2"),
    (b"\x89LZO\x00\r\n\x1a\n", "lzo"),
]

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ----------------------------------------------------------------------
# boot 镜像
# ----------------------------------------------------------------------
def parse_boot_hdr(path) -> dict | None:
    """解析 ANDROID! 头。返回 {kernel,ramdisk,page,size,hdr_ver} 或 None"""
    try:
        with open(path, "rb") as f:
            d = f.read(0x1000)
    except OSError:
        return None
    if len(d) < 64 or d[:8] != BOOT_MAGIC:
        return None
    kernel_size, = struct.unpack("<I", d[8:12])
    ramdisk_size, = struct.unpack("<I", d[16:20])
    hdr_ver, = struct.unpack("<I", d[0x28:0x2C])
    page = 2048 if hdr_ver < 2 else 4096
    total = page + ((kernel_size + ramdisk_size + page - 1) // page) * page
    return {"magic": True, "kernel": kernel_size, "ramdisk": ramdisk_size,
            "page": page, "size": total, "hdr_ver": hdr_ver}


def slice_kernel(path, out_path=None) -> bytes:
    """从 boot.img 里切出 kernel 段的原始字节（未解压）"""
    h = parse_boot_hdr(path)
    if not h:
        raise ImageError("不是合法的 Android boot 镜像（缺 ANDROID! 魔数）")
    with open(path, "rb") as f:
        f.seek(h["page"])
        blob = f.read(h["kernel"])
    if len(blob) != h["kernel"]:
        raise ImageError("kernel 段被截断：读到 %d，头里写 %d"
                         % (len(blob), h["kernel"]))
    if out_path:
        with open(out_path, "wb") as f:
            f.write(blob)
    return blob


# ----------------------------------------------------------------------
# 解压
# ----------------------------------------------------------------------
def _gunzip(blob: bytes) -> bytes:
    """处理多 member 的 gzip（内核镜像是单个，但别假设）"""
    out = bytearray()
    while blob:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            out += d.decompress(blob)
            out += d.flush()
        except zlib.error:
            break
        blob = d.unused_data
        if not blob.startswith(b"\x1f\x8b"):
            break
    return bytes(out)


def _lz4_block(src: bytes) -> bytes:
    """解一个 raw LZ4 block"""
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while i < n:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = token & 0x0F
        if ml == 15:
            while i < n:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(out) - off
        if start < 0:
            raise ImageError("LZ4 数据损坏（回溯越界）")
        for k in range(ml):
            out.append(out[start + k])
    return bytes(out)


def _lz4_legacy(blob: bytes) -> bytes:
    """legacy frame：magic(4) + blocksize(4)，之后每块 4 字节长度 + raw block"""
    pos = 8
    out = bytearray()
    while pos + 4 <= len(blob):
        n, = struct.unpack("<I", blob[pos:pos + 4])
        pos += 4
        if n == 0 or pos + n > len(blob):
            break
        out += _lz4_block(blob[pos:pos + n])
        pos += n
    return bytes(out)


def _dec(kind: str, stream: bytes) -> bytes:
    """按算法解一段流。失败抛异常，由调用方换下一个候选位置"""
    if kind == "gzip":
        return _gunzip(stream)
    if kind == "xz":
        return lzma.decompress(stream, format=lzma.FORMAT_XZ)
    if kind == "lzma":
        return lzma.decompress(stream, format=lzma.FORMAT_ALONE)
    if kind == "bzip2":
        return bz2.decompress(stream)
    if kind == "lz4legacy":
        return _lz4_legacy(stream)
    if kind == "lz4frame":
        try:
            import lz4.frame
        except ImportError:
            raise ImageError("kernel 是 LZ4 frame 压缩，需要 lz4 模块。"
                             "执行：pip install lz4")
        return lz4.frame.decompress(stream)
    if kind == "zstd":
        try:
            import zstandard
        except ImportError:
            raise ImageError("kernel 是 zstd 压缩，需要 zstandard 模块。"
                             "执行：pip install zstandard")
        return zstandard.ZstdDecompressor().decompress(stream)
    if kind == "lzo":
        raise ImageError("kernel 是 LZO 压缩，没有纯 Python 解压器。"
                         "用 `lzop -d` 手工解，或换用固件包里自带的 vmlinux")
    raise ImageError("未知压缩算法 %s" % kind)


def looks_like_arm64_image(blob: bytes) -> bool:
    """ARM64 Image：偏移 0x38 处是 "ARM\\x64" 魔数"""
    return len(blob) > 0x40 and blob[0x38:0x3C] == b"ARM\x64"


def decompress_kernel(blob: bytes) -> tuple[bytes, str]:
    """返回 (解压后字节, "算法@偏移")。

    为什么不是"找到第一个魔数就解"：未压缩的 kernel 里随机数据撞上 gzip
    魔数（1f 8b 08）很常见，直接解会得到空或垃圾。所以每个算法往后试多个
    候选位置，解出来必须真的是 ELF / ARM64 Image 才认。
    """
    if blob[:4] == b"\x7fELF":
        return blob, "none"
    if looks_like_arm64_image(blob):
        # 裸 Image 本身就是可用原料：kallsyms 的 token 表和偏移数组都编在
        # .rodata 里，能现场反解。返回它自己，让下游按 Image 处理。
        return blob, "image"

    seen = []
    for magic, kind in MAGICS:
        start = 0
        tried = 0
        while tried < 16:
            idx = blob.find(magic, start)
            if idx < 0:
                break
            tried += 1
            try:
                data = _dec(kind, blob[idx:])
            except ImageError:
                raise
            except Exception:
                data = b""
            # 必须是 ELF / Image：kernel 段里撞上 gzip 魔数的假流解出来是
            # 许可文本、字符串表之类（V1 实测遇到过一次，解出 "#\n# "）
            if data[:4] == b"\x7fELF":
                return data, "%s@%d" % (kind, idx)
            if data and looks_like_arm64_image(data):
                return data, "%s@%d" % (kind, idx)
            if data:
                seen.append("%s@%d→%r" % (kind, idx, data[:8]))
            start = idx + 1

    hint = ""
    if seen:
        hint = "。解出过但都不是 ELF：" + "，".join(seen[:4])
    raise ImageError(
        "kernel 段里找不到能解成 ELF 的压缩流（gzip/xz/lzma/lz4/zstd 都试过）"
        + hint + "。这个 kernel 可能被二次封装，或直接给固件包里的 vmlinux.elf 更快")


def vmlinux_from_boot(boot_path, out_path, log=None) -> tuple[str, str]:
    """boot.img → vmlinux.elf / Image，返回 (out_path, 算法)"""
    def say(s):
        if log:
            log(s)
    raw = slice_kernel(boot_path)
    say("  kernel 段 %d 字节" % len(raw))
    data, kind = decompress_kernel(raw)
    if data[:4] != b"\x7fELF" and not looks_like_arm64_image(data):
        raise ImageError("解压出来既不是 ELF 也不是 ARM64 Image"
                         "（前 4 字节 %r，算法 %s）" % (data[:4], kind))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(data)
    what = "ELF" if data[:4] == b"\x7fELF" else "裸 ARM64 Image（kallsyms 可反解）"
    say("  解压算法 %s，得到 %d 字节 %s" % (kind, len(data), what))
    return out_path, kind


# ----------------------------------------------------------------------
# 构建身份（linux_banner）
# ----------------------------------------------------------------------
BANNER_RE = re.compile(rb"Linux version ([\x20-\x7e]{16,240})")
_COMMIT_RE = re.compile(r"perf-g([0-9a-f]{8,16})")
_BUILD_RE = re.compile(r"#(\d+)")


def read_kernel_banner(path) -> str | None:
    """在 kernel Image 里找 linux_banner 字符串。

    这是唯一一个「零权限、从镜像本身就能读到」的构建身份：内核启动时
    /proc/version 打印的就是它。不依赖符号表、不依赖文件格式，直接字节搜。
    """
    try:
        with open(path, "rb") as f:
            is_boot = f.read(8) == BOOT_MAGIC
    except OSError:
        return None
    try:
        data = slice_kernel(path) if is_boot else open(path, "rb").read()
    except (ImageError, OSError):
        return None
    m = BANNER_RE.search(data)
    if not m:
        return None
    return m.group(1).decode("latin1").strip()


def parse_banner(text) -> dict:
    """把 banner 拆成可比对的字段。缺的给 None，不抛。"""
    if not text:
        return {}
    out = {"raw": text}
    m = _COMMIT_RE.search(text)
    out["commit"] = m.group(1) if m else None
    m = _BUILD_RE.search(text)
    out["build"] = int(m.group(1)) if m else None
    if "(" in text and ")" in text:
        out["compiler"] = text[text.find("(") + 1:text.rfind(")")]
    # 镜像里抓到的串以 "4.19.157-perf-g..." 开头，设备 /proc/version 带前缀，
    # 两种都得认，别按空格切死。
    m = re.search(r"(\d+\.\d+\.\d+[^\s]*)", text)
    out["version"] = m.group(1) if m else ""
    out["dirty"] = "-dirty" in text
    return out


def banner_match(banner, dev_version) -> tuple[str, str, str]:
    """镜像 banner vs 设备 /proc/version。

    返回 (verdict, why, advice)。verdict：
      same      同一构建 —— 偏移直接用
      resame    同 commit、build 号不同 —— 同源码重编，多半能对上
      dirty     commit 与 build 都同、但带 -dirty —— commit 不代表内容
      different 不同内核 —— 偏移必错
      unknown   有一边解析不出来
    """
    a = parse_banner(banner)
    b = parse_banner(dev_version)
    if not a.get("commit") or not b.get("commit"):
        return ("unknown",
                "有一边读不到 git commit（镜像里没有 linux_banner，或设备没连）",
                "设备已 root 的话，dd 出对应 boot 分区比对 sha256 更硬")
    if a["commit"] != b["commit"]:
        return ("different",
                "镜像 commit %s，设备 commit %s" % (a["commit"], b["commit"]),
                "这不是同一个内核。用它算出的偏移会让漏洞链打偏、在内核里"
                "随机位置崩 —— 换设备上正在跑的那个 boot 分区镜像")
    if a.get("build") != b.get("build"):
        return ("resame",
                "同 commit %s，build 号 %s vs %s"
                % (a["commit"], a.get("build"), b.get("build")),
                "同源码不同次编译，.config 一致时偏移应当相同；先在设备上验一遍")
    if a.get("dirty") or b.get("dirty"):
        return ("dirty",
                "commit 与 build 号都对得上（%s #%s），但带 -dirty"
                % (a["commit"], a.get("build")),
                "-dirty 说明编译时工作区有未提交改动，commit 代表不了二进制内容；"
                "镜像段的 sha256 才是硬证据")
    return ("same",
            "同一构建 %s #%s %s" % (a.get("version"), a.get("build"), a["commit"]),
            "偏移可用")


VERDICT_TAG = {"same": "[OK]", "resame": "[!]", "dirty": "[!]",
               "different": "[X]", "unknown": "[?]"}


def adb_read(remote, adb=None, serial=None) -> tuple[bool, bytes]:
    """读设备上一个文本文件，返回 (ok, bytes)。不做 su，失败不抛。"""
    adb = adb or "adb"
    base = [adb] + (["-s", serial] if serial else [])
    try:
        r = subprocess.run(base + ["exec-out", "cat", remote],
                           capture_output=True, timeout=30,
                           creationflags=_NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError):
        return False, b""
    return r.returncode == 0 and bool(r.stdout), (r.stdout or b"")


def banner_check(path, adb=None, serial=None, log=None) -> dict | None:
    """读镜像 banner → 连设备读 /proc/version → 给结论。设备不在也不抛。"""
    def say(x):
        if log:
            log(x)

    mine = read_kernel_banner(path)
    if not mine:
        return None
    # banner 原文不在这里打：CLI 会带标题打一遍，这里再打就是两遍。
    dev = None
    ok, out = adb_read("/proc/version", adb=adb, serial=serial)
    if ok:
        dev = out.decode("latin1").strip()
    if not dev:
        return dict(image=mine, device=None, verdict="unknown",
                    why="设备没连（或 /proc/version 读不出来），只解析了镜像",
                    advice="连上设备再跑一次")
    v, why, adv = banner_match(mine, dev)
    # 判定不在这里打印：CLI 要点的是「结论：...」那一行，GUI 有自己的展示位。
    # 两边各记一次会变成同一条信息出现两遍，反而不好读。
    return dict(image=mine, device=dev, verdict=v, why=why, advice=adv)


# ----------------------------------------------------------------------
# 从设备导出 kallsyms
# ----------------------------------------------------------------------
def grab_kallsyms(out_path, adb=None, serial=None, log=None) -> tuple[str, dict]:
    """从连着 USB 的设备上现场导出 /proc/kallsyms，返回 (out_path, 统计dict)。

    为什么用 exec-out 而不是 shell：`adb shell` 会做 CRLF 转换和编码转换，
    12 万行里混进 \\r 之后有些解析器会吃掉行尾字符；exec-out 是二进制透传。

    为什么先 su 再 cat：kptr_restrict=1 时非 root 读到的地址全是 0，
    这份文本看着有 12 万行其实一个字都用不了 —— 所以必须 su，
    而且拉完要统计零地址数量并直接报错，不能让用户拿着废文件去生成 target.h。
    """
    def say(s):
        if log:
            log(s)

    adb = adb or "adb"
    base = [adb] + (["-s", serial] if serial else [])

    r = subprocess.run(base + ["devices"], capture_output=True, timeout=30,
                       creationflags=_NO_WINDOW)
    txt = (r.stdout or b"").decode("utf-8", "replace")
    devs = [l.split("\t")[0] for l in txt.splitlines()[1:] if "\tdevice" in l]
    if not devs:
        raise ImageError("没有处于 device 状态的设备。\n"
                         "  adb devices 输出：\n" + txt.strip())
    if not serial and len(devs) > 1:
        raise ImageError("连了多台设备（%s），用 --serial 指定一台"
                         % ", ".join(devs))
    cur = serial or devs[0]
    say("  设备 %s" % cur)

    # 1) 关掉地址隐藏（失败不算致命，后面用零地址比例判定）
    subprocess.run(base + ["shell", "su", "-c",
                           "echo 0 > /proc/sys/kernel/kptr_restrict"],
                   capture_output=True, timeout=30, creationflags=_NO_WINDOW)

    # 2) exec-out 二进制透传
    say("  导出 /proc/kallsyms（12 万行左右，约 6MB，等十几秒）")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as f:
        p = subprocess.Popen(base + ["exec-out", "su", "-c",
                                     "cat /proc/kallsyms"],
                             stdout=f, stderr=subprocess.PIPE,
                             creationflags=_NO_WINDOW)
        try:
            err = p.communicate(timeout=180)[1]
        except subprocess.TimeoutExpired:
            # 挂住的 adb 必须杀掉，否则它占着 server，后面每个 adb 命令都会卡
            p.kill()
            p.communicate()
            try:
                os.remove(out_path)        # 半截文件不能留着误事
            except OSError:
                pass
            raise ImageError("导出超过 180 秒没结束，已中止。\n"
                             "  设备可能锁屏了（锁屏后 exec-out 会挂住），"
                             "解锁后重试")
    if p.returncode != 0:
        try:
            os.remove(out_path)
        except OSError:
            pass
        e = (err or b"").decode("utf-8", "replace").strip()
        raise ImageError(
            "导出失败（exit %s）：%s\n"
            "  多半是 su 没授权。先在设备上确认：\n"
            "    adb shell su -c 'id'    → 应输出 uid=0(root)"
            % (p.returncode, e or "无 stderr"))

    # 3) 校验：正常应是 6MB / 12 万行；全零地址必须报错
    size = os.path.getsize(out_path)
    ks = elfsyms.Kallsyms(out_path)
    say("  %d 字节，%d 行，有效符号 %d，零地址 %d"
        % (size, ks.total, len(ks.symbols()), ks.zeroed))

    def _reject(msg: str) -> None:
        """校验不过就把文件删掉。

        留着的话它和正常导出同名同大小，`ls | head -1` 一条命令就会
        挑中它，下一步 gen 报「一个有效地址都没有」——错在上一层，
        却要人在下一层猜原因。
        """
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise ImageError(msg)

    if size < 512 * 1024:
        _reject(
            "导出只有 %d 字节，太小了。设备上 /proc/kallsyms 一般 5~7MB。\n"
            "  su 可能没拿到 root，或内核编译时关了 CONFIG_KALLSYMS。" % size)
    if ks.zeroed:
        _reject(
            "有 %d/%d 条地址是 0，这份文件不能用（kptr_restrict 没关掉）。\n"
            "  设备上手动确认：\n"
            "    adb shell su -c 'cat /proc/sys/kernel/kptr_restrict'   → 想要 0\n"
            "    adb shell su -c 'head -3 /proc/kallsyms'              → 第一列不能全 0\n"
            "  能写时先 `su -c 'echo 0 > /proc/sys/kernel/kptr_restrict'` 再导一次；\n"
            "  有些内核不允许改这个值，那就只能走 boot.img / vmlinux.elf 那条路。"
            % (ks.zeroed, ks.total))
    if "_text" not in ks.symbols():
        _reject("这份 kallsyms 里没有 _text，可能被裁剪过")
    base_addr = ks.symbols()["_text"][0]
    say("  _text = 0x%016X" % base_addr)
    return out_path, {"lines": ks.total, "syms": len(ks.symbols()),
                      "size": size, "text": base_addr, "serial": cur}


# ----------------------------------------------------------------------
# 统一符号来源
# ----------------------------------------------------------------------
def resolve_syms_source(path, keep=None, log=None) -> tuple[str, str, str | None]:
    """把任意来源统一成「open_syms 能直接吃的文件路径」。

    boot.img 会切出 kernel 段、解压到临时文件再用 —— 调用方不需要知道
    进来的是 boot 还是裸 Image 还是 ELF，拿到的永远是一个能读符号表的文件。

    返回 (path, how, banner)。how 是说明用的字符串（"direct" / "gzip@0x..."）。
    """
    def say(x):
        if log:
            log(x)
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError as ex:
        raise ImageError("打不开 %s：%s" % (path, ex))
    if head != BOOT_MAGIC:
        return path, "direct", read_kernel_banner(path)

    say("  %s 是 boot.img，先切 kernel 段" % os.path.basename(path))
    raw = slice_kernel(path)
    data, how = decompress_kernel(raw)
    if keep:
        out = keep
    else:
        suf = ".elf" if data[:4] == b"\x7fELF" else ".img"
        fd, out = tempfile.mkstemp(prefix="leaf5-kernel-", suffix=suf)
        os.close(fd)
    with open(out, "wb") as f:
        f.write(data)
    say("  kernel 段 %d 字节，%s，落盘 %d 字节 → %s"
        % (len(raw), how, len(data), out))
    return out, how, read_kernel_banner(out)


# ----------------------------------------------------------------------
# 判定一个文件能不能当内核用
# ----------------------------------------------------------------------
def probe_kernel(path, deep=True) -> dict | None:
    """判定单个文件能不能当内核用。

    返回 dict(path, size, kind, usable, note) 或 None（不是内核镜像）。
    usable 的判据是「能取到符号且有 _text」—— 少了这个，后面生成 target.h
    那步必然失败，所以提前判掉，别让用户选了才发现白忙。

    kind 四种：elf（vmlinux）、syms（kallsyms / System.map 文本）、
    boot（boot.img）、image（裸 ARM64 Image）
    """
    try:
        size = os.path.getsize(path)
        if size < 64 * 1024:
            return None
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None

    if head[:4] == b"\x7fELF":
        if not deep:
            return dict(path=path, size=size, kind="elf", usable=True, note="")
        try:
            with elfsyms.Elf64(path) as e:
                syms = e.symbols()
        except Exception as ex:
            return dict(path=path, size=size, kind="elf", usable=False,
                        note="ELF 取不到符号表：%s" % ex)
        if "_text" not in syms:
            return dict(path=path, size=size, kind="elf", usable=False,
                        note="ELF 但没有 _text 符号（strip 过），偏移无从算起")
        return dict(path=path, size=size, kind="elf", usable=True,
                    note="%d 个符号" % len(syms))

    if head[:8] == BOOT_MAGIC:
        if not deep:
            return dict(path=path, size=size, kind="boot", usable=True,
                        note="boot.img（kernel 段可反解 kallsyms）")
        try:
            raw = slice_kernel(path)
            data, how = decompress_kernel(raw)
        except Exception as ex:
            return dict(path=path, size=size, kind="boot", usable=False,
                        note="boot.img 解不出内核：%s" % ex)
        if data[:4] == b"\x7fELF":
            return dict(path=path, size=size, kind="boot", usable=True,
                        note="kernel 段 %s → ELF" % how)
        return dict(path=path, size=size, kind="boot", usable=True,
                    note="kernel 段 %s → 裸 ARM64 Image（可反解 kallsyms）" % how)

    # 裸 ARM64 Image：偏移 0x38 是 "ARM\x64"。设备没 root、外部又拿不到
    # vmlinux 时，boot 分区的 kernel 段就是唯一原料 —— kallsyms 的 token 表
    # 与偏移数组本来就编在 .rodata 里，现场反解即可。
    try:
        with open(path, "rb") as f:
            f.seek(0x38)
            is_image = f.read(4) == b"ARM\x64"
    except OSError:
        is_image = False
    if is_image:
        if not deep:
            return dict(path=path, size=size, kind="image", usable=True,
                        note="裸 ARM64 Image（可反解 kallsyms）")
        try:
            from . import imageksym
            with imageksym.ImageKallsyms(path) as ks:
                lay = ks.locate()
                syms = ks.symbols()
        except Exception as ex:
            return dict(path=path, size=size, kind="image", usable=False,
                        note="裸 ARM64 Image，kallsyms 反解失败：%s" % ex)
        if "_text" not in syms:
            return dict(path=path, size=size, kind="image", usable=False,
                        note="反解出了符号表但没有 _text，偏移无从算起")
        return dict(path=path, size=size, kind="image", usable=True,
                    note="%d 个符号（从镜像反解，%d 条）" % (len(syms), lay["count"]))

    # kallsyms / System.map 文本：地址 类型 名字
    if size >= 256 * 1024 and elfsyms.looks_like_kallsyms(path):
        if not deep:
            return dict(path=path, size=size, kind="syms", usable=True,
                        note="kallsyms 文本")
        ks = elfsyms.Kallsyms(path)
        syms = ks.symbols()
        if ks.zeroed:
            return dict(path=path, size=size, kind="syms", usable=False,
                        note="kallsyms 文本，但 %d 条地址为 0（kptr_restrict 没关）"
                             % ks.zeroed)
        if "_text" not in syms:
            return dict(path=path, size=size, kind="syms", usable=False,
                        note="kallsyms 文本，但没有 _text，片段不完整")
        return dict(path=path, size=size, kind="syms", usable=True,
                    note="%d 个符号（kallsyms 文本）" % len(syms))

    return None


# ----------------------------------------------------------------------
# 本机镜像自动发现
# ----------------------------------------------------------------------
SCAN_SKIP_DIRS = {
    "node_modules", ".git", ".svn", "$RECYCLE.BIN", "System Volume Information",
    "Windows", "WinSxS", "Installer", "System32", "__pycache__", ".gradle",
    ".android", ".cache", "site-packages",
    "AppData", "Application Data", "Local Settings", "ProgramData",
    "Program Files", "Program Files (x86)", "Recovery", "PerfLogs", "MSOCache",
}
# 故意不收 ".txt"：全盘扫时 txt 几十万个，每个都要读头部判格式，代价远大于
# 收益。kallsyms 导出靠 _SCAN_HINTS 里的文件名（kallsyms/system.map）命中。
SCAN_EXT = (".elf", ".img", ".bin", ".image", ".map")
_SCAN_HINTS = ("vmlinux", "kernel", "boot", "image", "kallsyms", "system.map",
               "system_map")
SCAN_MIN_BYTES = 256 * 1024


def scan_roots(extra=None) -> list[str]:
    """扫这些地方：exe 旁边、当前目录、工具目录、下载/桌面/文档"""
    roots = []

    def add(p):
        if not p:
            return
        try:
            if os.path.isdir(p):
                rp = os.path.abspath(p)
                if rp not in roots:
                    roots.append(rp)
        except OSError:
            pass

    for p in (extra or []):
        add(p)
    import sys
    add(os.path.dirname(os.path.abspath(
        sys.argv[0] if getattr(sys, "frozen", False) else __file__)))
    add(os.getcwd())
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    for sub in ("Downloads", "Desktop", "Documents",
                os.path.join("OneDrive", "Desktop"),
                os.path.join("OneDrive", "Downloads")):
        add(os.path.join(home, sub))
    return roots


def scan_kernels(roots=None, max_depth=3, budget=8.0, limit=20000, log=None,
                 stop=None, first_hit=False) -> tuple[list, list]:
    """在常见目录里找现成的内核镜像。返回 (usable, others)。

      usable —— 能直接拿去生成 target.h
      others —— 找到了但用不上（strip 过 / 解不开），列出来免得用户以为没扫到

    复杂度 O(访问的文件数)，用深度、条目数、墙钟三重预算卡住：用户目录可能
    有几十万文件，扫穿了会让向导启动卡十几秒。
    """
    import collections

    def say(s):
        if log:
            log(s)

    roots = roots or scan_roots()
    usable, others = [], []
    seen = set()
    seen_dir = set()
    t0 = time.time()
    visited = 0

    def norm(p):
        return os.path.normcase(os.path.abspath(p))

    q = collections.deque((r, 0) for r in roots)
    while q:
        if time.time() - t0 > budget or visited > limit:
            say("  扫描预算用尽（%d 个条目，%.1fs），只列已找到的"
                % (visited, time.time() - t0))
            break
        if stop and stop():
            say("  已取消（扫了 %d 个条目）" % visited)
            break
        d, depth = q.popleft()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for ent in it:
                visited += 1
                if visited > limit:
                    break
                try:
                    if ent.is_dir(follow_symlinks=False):
                        name = ent.name
                        if depth >= max_depth:
                            continue
                        if name in SCAN_SKIP_DIRS or name.startswith("."):
                            continue
                        # 用规范化路径去重，不用 inode —— Windows 下
                        # DirEntry.stat() 的 st_ino 恒为 0（走 FindFirstFile，
                        # 拿不到 file index），拿它去重会把 116 个目录判成同一个，
                        # 整棵树只展开第一层。V1 实测踩过，别改回去。
                        key = norm(ent.path)
                        if key in seen_dir:
                            continue
                        seen_dir.add(key)
                        q.append((ent.path, depth + 1))
                        continue
                    if not ent.is_file(follow_symlinks=False):
                        continue
                    low = ent.name.lower()
                    if not (low.endswith(SCAN_EXT) or
                            any(h in low for h in _SCAN_HINTS)):
                        continue
                    if ent.path in seen:
                        continue
                    seen.add(ent.path)
                    info = probe_kernel(ent.path)
                    if not info:
                        continue
                    if info["usable"]:
                        usable.append(info)
                        if first_hit:
                            say("  命中：%s" % info["path"])
                            q.clear()
                            break
                    else:
                        others.append(info)
                except OSError:
                    continue

    # 带符号的优先：文件大的通常是完整 vmlinux，排前面
    usable.sort(key=lambda x: -x["size"])
    others.sort(key=lambda x: -x["size"])
    say("  扫了 %d 个条目 / %d 个起点 / %.1fs：可用 %d 个，不可用 %d 个"
        % (visited, len(roots), time.time() - t0, len(usable), len(others)))
    return usable, others


def scan_disks(budget=180.0, max_depth=8, log=None, stop=None,
               first_hit=False) -> tuple[list, list]:
    """全盘扫。常见目录扫不到时才跑。

    只按扩展名/文件名筛，命中才读文件头，所以主要成本在遍历目录本身。
    first_hit=True 找到第一个可用镜像就停 —— 用户要的是「能用」，不是清单。
    """
    roots = []
    if os.name == "nt":
        # 非系统盘先扫：内核镜像几乎都在数据盘，系统盘放最后当兜底
        for drive in "DEFGHC":
            d = "%s:\\" % drive
            if os.path.isdir(d):
                roots.append(d)
    else:
        roots.append("/")

    def say(s):
        if log:
            log(s)

    say("  全盘扫描：%s（深度 %d，最多 %d 秒）"
        % (", ".join(roots), max_depth, int(budget)))
    return scan_kernels(roots=roots, max_depth=max_depth, budget=budget,
                        limit=400000, log=log, stop=stop, first_hit=first_hit)


# ----------------------------------------------------------------------
# 最近用过的镜像（记忆）
# ----------------------------------------------------------------------
# 自动扫描只覆盖常见目录。用户的 vmlinux 常放在任意盘的工作目录里，扫不到。
# 所以选过一次就记下来，下次打开直接填上 —— 命中率最高的一层「自动」。
def recent_file(root) -> str:
    return os.path.join(str(root), "recent_kernels.json")


def load_recent(root, max_n=8) -> list[str]:
    """返回仍存在的最近路径，最近的在前面。失效的顺手剔掉。"""
    p = recent_file(root)
    try:
        with open(p, encoding="utf-8") as f:
            items = json.load(f)
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    alive = []
    for it in items:
        if not isinstance(it, str) or not os.path.isfile(it):
            continue
        if it not in alive:
            alive.append(it)
        if len(alive) >= max_n:
            break
    if alive != items:
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(alive, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    return alive


def remember_kernel(root, path) -> None:
    """把一个路径提到最近列表最前面。原子写，避免并发时写坏。"""
    if not path:
        return
    path = os.path.abspath(path)
    items = [path] + [x for x in load_recent(root) if x != path]
    items = items[:8]
    dst = recent_file(root)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, dst)
    except OSError:
        pass


def human_size(n) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n
