"""内核适配：把 preload.so 迁到另一版内核。

流程（每步都能单独跑，对应 CLI 的 `l5tk adapt <动作>`）：

    boot.img ──切 kernel 段──> 解压 ──> vmlinux.elf / 裸 Image
                                          │
                                          ├─ 提 60 个符号偏移（自动）
                                          └─ 生成 targets/<id>/target.h
                                                   │
                                                   ├─ 4 个结构体/栈常量 → 人工填
                                                   └─ 三段编译 → preload.so

能自动的：符号偏移、target.h 骨架、编译、产物登记、设备↔target 匹配
不能自动的：TASK_CRED_OFF / WAITER_LOCAL_OFF / PRCTL_WAITER_WORD_DEFAULT /
           PRCTL_FRAME_TOTAL —— 结构体布局与栈帧大小，符号表里没有，
           必须反汇编锚点或 panic 定标（见 MANUAL_MACROS 的提示）

已验证的编译参数（NDK 27.0.12077973）产出 md5
6f36e1e889e0d05aa2fd9d93f3771168，与出厂载荷逐字节一致。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..core import ainsn, elfsyms, kimage, ndk
from ..core.errors import L5Error

TARGET_API = "35"

# 这些宏符号表里没有，需要人工。顺序即界面上的填写顺序。
MANUAL_MACROS = [
    ("WAITER_LOCAL_OFF", "rt_mutex_waiter 里 task 字段的偏移。"
                         "出厂内核 0x50，主线 4.19 是 0x40（本机多了 prio+deadline）"),
    ("TASK_CRED_OFF", "task_struct.cred 的偏移。看 commit_creds 里 "
                      "ldr x8,[x19,#0x7e0] 那个立即数"),
    ("PRCTL_FRAME_TOTAL", "__arm64_sys_prctl 的总帧大小。看函数开头的 "
                          "stp/sub sp 指令"),
    ("PRCTL_WAITER_WORD_DEFAULT", "copy word 的下标，决定从栈上哪个槽读 waiter。"
                                  "必须 panic 定标：PRCTL_POISON=1 跑一次，从 x21 反推"),
]

# 生成文件里用来标记"还没填"的记号。编译前会检查它，留着就拒绝编译。
FILL_MARK = "/*FILL_ME*/"

C_SRCS = ["main.c", "util.c", "slide.c", "fops.c", "pipe.c", "root.c",
          "preload.c", "heap_spray.c", "kgsl_route.c"]
ASM_SRCS = ["su_blob.S", "wallpaper_blob.S"]

# 出厂载荷的字节数。编出来对得上说明结构没跑偏（同一套源码 + 同一份 target.h）
STOCK_SO_SIZE = 2494504


class AdaptError(L5Error):
    """适配流程里的失败：镜像不对、符号提不到、编译器不给、模板缺宏。"""


@dataclass
class Target:
    id: str
    dir: Path
    meta: dict = field(default_factory=dict)
    legacy: bool = False          # 来自 V1 的 %LOCALAPPDATA%\Leaf5RootTool\targets

    @property
    def so(self) -> Path:
        return self.dir / "preload.so"

    @property
    def kernel(self) -> str:
        return str(self.meta.get("kernel") or "")

    @property
    def fingerprint(self) -> str:
        return str(self.meta.get("fingerprint") or "")

    def has_so(self) -> bool:
        return self.so.is_file()


@dataclass
class GenResult:
    tid: str
    tdir: Path
    unfilled: list[str]
    diffs: list[tuple[str, int, int]]
    offsets: int
    missing: list[str]
    banner: str | None
    symfp: Path | None
    manual: str                   # "template" / "inherited" / "forced"
    auto: dict = field(default_factory=dict)   # 反汇编扫出来的手工宏

    @property
    def ready(self) -> bool:
        """target.h 里没有占位符，可以编译"""
        return not self.unfilled


# ----------------------------------------------------------------------
# target.h 生成
# ----------------------------------------------------------------------
def _sub_macro(txt: str, name: str, literal: str, comment_keep: bool = True):
    """把 `#define NAME <数字><后缀><行尾注释>` 里的数字换掉，保留后缀与注释"""
    pat = re.compile(
        r"^(#define\s+" + re.escape(name) + r"\s+)"
        r"(0[xX][0-9a-fA-F]+|\d+)"
        r"(ULL|UL|U|L|LL|ull|ul|u|l|ll)?"
        r"(.*)$", re.M)
    m = pat.search(txt)
    if not m:
        return txt, False
    suffix = m.group(3) or ""
    tail = m.group(4) if comment_keep else ""
    new = "%s%s%s%s" % (m.group(1), literal, suffix, tail)
    return txt[:m.start()] + new + txt[m.end():], True


def suggest_id(fingerprint: str, kernel_ver: str) -> str:
    """给新 target 起个短 id：migr-<内核串>-<指纹前 8 位>"""
    h = hashlib.sha1((fingerprint or "unknown").encode("utf-8")).hexdigest()[:8]
    k = re.sub(r"[^0-9A-Za-z._-]+", "_", (kernel_ver or "kernel"))[:24]
    return "migr-%s-%s" % (k, h)


def template_offsets(template_path) -> dict[str, int]:
    """模板里所有 `#define X 0x..` 的原值，用来判「同源」还是「换版本」。"""
    try:
        txt = Path(template_path).read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for m in re.finditer(r"^#define\s+(\w+)\s+(0[xX][0-9a-fA-F]+|\d+)", txt, re.M):
        out.setdefault(m.group(1), int(m.group(2), 0))
    return out


def template_manuals(template_path) -> dict[str, int]:
    """模板里那 4 个手工宏的原值。用于「同源重编」时直接继承。"""
    try:
        txt = Path(template_path).read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for macro, _hint in MANUAL_MACROS:
        m = re.search(r"^#define\s+%s\s+(0[xX][0-9a-fA-F]+|\d+)"
                      % re.escape(macro), txt, re.M)
        if m:
            out[macro] = int(m.group(1), 0)
    return out


def build_target_h(template_path, out_path, offsets, fingerprint, kernel_ver,
                   log=None, syms_mode: str = "trust",
                   manual_values: dict | None = None):
    """读模板，替换符号偏移，把手工宏标成占位符或直接写值。

    syms_mode:
      "trust" —— 全部采用镜像提取值（适配新内核必须用这个）
      "keep"  —— 只替换"模板值与提取值相同"的宏，其余保留模板原值

    manual_values: {宏名: 值} —— 推得出来的手工宏。给了就写进文件，
      不再留占位符；没给的照旧留 FILL_MARK，逼人来看。

    返回 (unfilled, notfound, diffs)
      diffs: [(macro, 模板值, 镜像值)] —— 两边不一致的清单
    """
    def say(s):
        if log:
            log(s)

    txt = Path(template_path).read_text(encoding="utf-8")
    orig_len = len(txt)
    replaced, notfound, diffs = [], [], []

    # 先记下模板原值，用来判"同源"还是"换版本"
    pat = re.compile(r"^#define\s+(\w+)\s+(0[xX][0-9a-fA-F]+|\d+)", re.M)
    orig = {}
    for m in pat.finditer(txt):
        orig.setdefault(m.group(1), int(m.group(2), 0))

    for macro, val in offsets.items():
        o = orig.get(macro)
        if o is not None and o != val:
            diffs.append((macro, o, val))
            if syms_mode == "keep":
                continue          # 保留模板值，只报告
        txt, ok = _sub_macro(txt, macro, "0x%012X" % val)
        if ok:
            replaced.append(macro)
        else:
            notfound.append(macro)

    unfilled = []
    manual_values = manual_values or {}
    for macro, _hint in MANUAL_MACROS:
        v = manual_values.get(macro)
        if v is not None:
            txt, ok = _sub_macro(txt, macro, "0x%X" % v)
            if ok:
                say("  %-28s = 0x%X  （已定标）" % (macro, v))
                continue
        txt, ok = _sub_macro(txt, macro, "%s 0x0" % FILL_MARK)
        if ok:
            unfilled.append(macro)
        else:
            say("  [警告] 模板里没有 %s，这个版本的 target.h 结构变了" % macro)

    tid = os.path.basename(os.path.dirname(str(out_path)))
    txt = re.sub(r'#define\s+BUILD_VARIANT_LABEL\s+"[^"]*"',
                 '#define BUILD_VARIANT_LABEL "migrated_%s"'
                 % re.sub(r"[^0-9A-Za-z._-]", "_", tid), txt)
    if fingerprint:
        txt = re.sub(r'#define\s+BUILD_FINGERPRINT\s+"[^"]*"',
                     '#define BUILD_FINGERPRINT "%s"' % fingerprint, txt)

    banner = (
        "/* ===== 由 l5tk 内核适配向导生成 %s =====\n"
        " * kernel   : %s\n"
        " * version  : %s\n"
        " * 符号偏移 : 自动从镜像提取（%d 个）\n"
        " * 模板差异 : %d 个宏与模板原值不同%s\n"
        " * 待人工填 : %s\n"
        " * 填完才能编译：占位符留在源码里会编不过\n"
        " * ======================================================== */\n"
        % (time.strftime("%Y-%m-%d %H:%M:%S"), kernel_ver or "?",
           fingerprint or "?", len(replaced), len(diffs),
           "（syms_mode=keep，已保留模板值）" if syms_mode == "keep" else "",
           ", ".join(unfilled) if unfilled else "无"))
    txt = banner + txt

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(txt, encoding="utf-8", newline="\n")

    say("  写出 %s（%d 字节，模板 %d）" % (out_path, len(txt), orig_len))
    if notfound:
        say("  [警告] 模板里找不到这些宏：%s" % ", ".join(notfound))
    if diffs:
        say("  [i] %d 个宏与模板原值不同：" % len(diffs))
        for macro, o, v in diffs:
            say("      %-32s 模板 0x%X  镜像 0x%X  (%+d)"
                % (macro, o, v, v - o))
    return unfilled, notfound, diffs


def fill_manuals(tdir, values: dict, log=None) -> list[str]:
    """把用户填的 4 个值写进 target.h，返回还剩下的占位符。

    占位符那行长这样：`#define TASK_CRED_OFF /*FILL_ME*/ 0x0` —— 数字前面
    多了个标记，所以不能直接用 _sub_macro（它的数字在宏名后面第一位置），
    先按占位符形态替换，再退回普通形态（用户手工改过的行）。
    """
    p = Path(tdir) / "target.h"
    txt = p.read_text(encoding="utf-8")
    for macro, v in values.items():
        if v is None:
            continue
        pat = re.compile(r"^(#define\s+" + re.escape(macro) + r"\s+)"
                         + re.escape(FILL_MARK) + r"\s+(?:0[xX]0+|\d+)", re.M)
        new, n = pat.subn(lambda m: "%s0x%X" % (m.group(1), int(v)), txt)
        if n:
            txt = new
        else:
            txt, n = _sub_macro(txt, macro, "0x%X" % int(v))
        if n and log:
            log("  %-28s = 0x%X" % (macro, int(v)))
    p.write_text(txt, encoding="utf-8", newline="\n")
    return placeholders(tdir)


def placeholders(tdir) -> list[str]:
    p = Path(tdir) / "target.h"
    if not p.is_file():
        return list(m for m, _ in MANUAL_MACROS)
    txt = p.read_text(encoding="utf-8")
    return re.findall(r"#define\s+(\w+)\s+" + re.escape(FILL_MARK), txt)


# ----------------------------------------------------------------------
# 编译
# ----------------------------------------------------------------------
def _run(args, cwd, timeout=600):
    return subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout,
                          creationflags=0x08000000 if os.name == "nt" else 0)


def _as_cmd(cc: str, argv: list[str]) -> list[str]:
    """Windows 下 NDK 给的是 aarch64-linux-android35-clang.cmd（批处理包装）。

    走 cmd /c 传参时，-DTARGET_CONFIG_H=\\"...\\" 这种带引号的宏会被 cmd
    二次转义，实测直接编译会失败（报宏未定义）。解法：用同目录的
    clang.exe 直接调，target triple 由 argv 里的 --target= 指定。
    """
    if cc.lower().endswith(".cmd"):
        exe = os.path.join(os.path.dirname(cc), "clang.exe")
        if os.path.isfile(exe):
            return [exe] + argv
        return ["cmd", "/c", cc] + argv
    return [cc] + argv


def _rel(p, base) -> str:
    """跨盘符时 relpath 会抛 ValueError，退回绝对路径（统一正斜杠）"""
    try:
        return os.path.relpath(p, base).replace("\\", "/")
    except ValueError:
        return os.path.abspath(p).replace("\\", "/")


def _sysroot_of(cc: str) -> str:
    # .../prebuilt/<host>/bin/<cc>  →  .../prebuilt/<host>/sysroot
    prebuilt = os.path.dirname(os.path.dirname(os.path.abspath(cc)))
    return os.path.join(prebuilt, "sysroot")


def ensure_wallpaper(exploit_dir) -> str:
    """src/assets/wallpaper.webp 是 .incbin 的相对目标，缺了就补 40 字节占位"""
    exploit_dir = str(exploit_dir)
    dst = os.path.join(exploit_dir, "src", "assets", "wallpaper.webp")
    if os.path.isfile(dst):
        return dst
    src = os.path.join(exploit_dir, "assets", "wallpaper.webp")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isfile(src):
        shutil.copyfile(src, dst)
        return dst
    # 兜底：跟 Makefile 里那段 python 生成逻辑等价
    d = b"RIFF" + struct.pack("<I", 0) + b"WEBP" + b"VP8 " + struct.pack("<I", 30)
    d += bytes([0x9d, 0x01, 0x2a, 0x01, 0x00, 0x01, 0x00])
    d += bytes([0x01, 0x40, 0x25, 0xa4, 0x00, 0x03, 0x70, 0x00])
    d += bytes([0xfe, 0xfb, 0x94, 0x00, 0x00])
    d = d[:4] + struct.pack("<I", len(d) - 8) + d[8:]
    with open(dst, "wb") as f:
        f.write(d)
    return dst


def compile_preload(exploit_dir, target_dir, cc, out_so, log=None, cancel=None):
    """三段编译（顺序不能换）：

      ① su_daemon → out/exploit/aarch64/embed/su_daemon_aarch64_pie
      ② su_blob.S 里的 .incbin 直接吃 ①的产物
      ③ 主链接

    返回 (out_so, md5, size)。
    """
    def say(s):
        if log:
            log(s)

    def chk():
        if cancel and cancel():
            raise AdaptError("已取消")

    exploit_dir = os.path.abspath(exploit_dir)
    target_dir = os.path.abspath(target_dir)
    sysroot = _sysroot_of(cc)
    if not os.path.isdir(sysroot):
        raise AdaptError("sysroot 不存在：%s（NDK 安装不完整？）" % sysroot)
    if not os.path.isfile(os.path.join(target_dir, "target.h")):
        raise AdaptError("%s 下没有 target.h" % target_dir)

    # target.h 里的占位符还没填就别浪费 3 分钟
    left = placeholders(target_dir)
    if left:
        raise AdaptError("target.h 里这些宏还是占位符没填：%s。"
                         "按 MIGRATION 说明填完再编译，否则编出来是错的"
                         % ", ".join(left))

    ensure_wallpaper(exploit_dir)

    embed_dir = os.path.join(exploit_dir, "out", "exploit", "aarch64", "embed")
    os.makedirs(embed_dir, exist_ok=True)
    daemon = os.path.join(embed_dir, "su_daemon_aarch64_pie")

    # ① su_daemon（PIE 独立可执行，会被 incbin 进 .rodata）
    #    必须 -static：动态版只有 15KB，设备上没有对应 linker/interp 路径，
    #    push 上去直接跑不起来。静态版 2251616 字节，EXE 里出厂载荷就是它。
    say("  ① 编译 su_daemon（静态，约 30 秒）…")
    chk()
    r = _run(_as_cmd(cc, [
        "--target=aarch64-linux-android%s" % TARGET_API,
        "--sysroot=%s" % sysroot, "-O2", "-static", "-pie",
        "src/su_daemon.c", "-o", _rel(daemon, exploit_dir),
    ]), cwd=exploit_dir, timeout=300)
    if r.returncode != 0 or not os.path.isfile(daemon):
        raise AdaptError("su_daemon 编译失败：\n"
                         + (r.stderr or b"").decode("utf-8", "replace")[-1500:])
    say("     %d 字节" % os.path.getsize(daemon))

    # .incbin "../out/exploit/aarch64/embed/su_daemon_aarch64_pie" 的解析基准
    # 在 clang 上不唯一：实测 cwd 优先、源文件所在目录兜底。两种位置都放一份，
    # 否则会读到上一次编译留下的旧文件（尺寸对不上，编出来的 so 是错的）。
    alt_embed = os.path.join(os.path.dirname(exploit_dir),
                             "out", "exploit", "aarch64", "embed")
    if os.path.abspath(alt_embed) != os.path.abspath(embed_dir):
        os.makedirs(alt_embed, exist_ok=True)
        try:
            shutil.copyfile(daemon, os.path.join(alt_embed,
                                                 "su_daemon_aarch64_pie"))
        except OSError as e:
            say("    [!] 备用 embed 目录写入失败（不影响本次编译）：%s" % e)

    # ②③ 主链接
    say("  ② 链接 preload.so（9 个 .c + 2 个 .S，约 1 分钟）…")
    chk()
    os.makedirs(os.path.dirname(os.path.abspath(out_so)), exist_ok=True)
    rel_tgt = _rel(target_dir, exploit_dir)
    argv = [
        "--target=aarch64-linux-android%s" % TARGET_API,
        "--sysroot=%s" % sysroot,
        "-I.", "-Isrc", "-I%s" % rel_tgt,
        "-O2", "-fPIC", "-shared",
        # 这里必须是纯双引号，不能写成 \" —— clang 会把反斜杠当字面量，
        # 宏展开成 \"targets/... 缺终止引号，#include 直接失效（V1 实测报
        # "missing terminating '\"' character"，然后整批宏未定义）
        '-DTARGET_CONFIG_H="%s/target.h"' % rel_tgt,
        "-DFUTEX_KEY_LAYOUT_V1",
        "-pthread",
    ] + ["src/%s" % s for s in C_SRCS + ASM_SRCS] + [
        "-o", _rel(out_so, exploit_dir),
    ]
    r = _run(_as_cmd(cc, argv), cwd=exploit_dir, timeout=900)
    if r.returncode != 0 or not os.path.isfile(out_so):
        raise AdaptError("preload.so 链接失败：\n"
                         + (r.stderr or b"").decode("utf-8", "replace")[-2000:])

    size = os.path.getsize(out_so)
    md5 = hashlib.md5(open(out_so, "rb").read()).hexdigest()
    say("     %d 字节  md5=%s" % (size, md5))
    if size == STOCK_SO_SIZE:
        say("     尺寸与出厂载荷一致（%d）—— 结构没跑偏" % STOCK_SO_SIZE)
    return out_so, md5, size


# ----------------------------------------------------------------------
# 主体
# ----------------------------------------------------------------------
def pick_identity(banner: str | None, dev: dict,
                  kernel: str = "", fingerprint: str = "") -> tuple[str, str]:
    """决定这份 target 记哪个内核串、哪个指纹。

    镜像 banner 里写着这份内核到底是什么版本，它优先于「手里连着的设备」：
    解析别的型号的 ROM 时，设备内核串属于那台机器，不属于这份镜像。
    借过来会让 target 记错版本 —— root exploit 的内核闸永远比不中，
    或者更糟：两个不同的镜像被认成同一份。

    指纹同理。只有镜像内核串与设备完全一致（说明镜像就是这台机器的）
    才沿用设备的 build fingerprint；否则留空，靠内核串和符号指纹去匹配，
    编一个指纹只会认错机器。
    """
    img_ver = (kimage.parse_banner(banner) or {}).get("version") or ""
    out_kernel = kernel or img_ver or (dev.get("kernel") or "")
    if fingerprint:
        return out_kernel, fingerprint
    same_dev = bool(img_ver) and img_ver == (dev.get("kernel") or "")
    if same_dev or not img_ver:
        return out_kernel, dev.get("fingerprint") or ""
    return out_kernel, ""


class Adapter:
    """一次适配会话。cfg 决定工作区与target仓库位置，adb 只用来读设备。"""

    def __init__(self, cfg, log, adb=None, serial: str = ""):
        self.cfg = cfg
        self.log = log
        self.adb = adb
        self.serial = serial or getattr(cfg, "serial", "") or ""
        self.root = Path(cfg.adapt_root)
        self.src_root = self.root / "src"
        self.exploit_dir = self.src_root / "exploit"
        self.targets_root = self.root / "targets"
        self.ndk_root = self.root / "ndk"

    # ---------------- 路径与源码 ----------------
    def _say(self, s):
        if self.log:
            self.log.info(s)

    def bundled_exploit(self) -> Path | None:
        """EXE 里带的 exploit 源码；开发模式退回 ../leaf5-ghostlock/exploit"""
        for base in (self.cfg.base_dir, self.cfg.bundle_dir):
            p = Path(base) / "assets" / "exploit_src" / "exploit"
            if (p / "src" / "main.c").is_file():
                return p
        for base in (self.cfg.base_dir, self.cfg.bundle_dir, Path(__file__).parents[2]):
            p = Path(base).parent / "leaf5-ghostlock" / "exploit"
            if (p / "src" / "main.c").is_file():
                return p
        return None

    def ensure_src(self, force: bool = False) -> Path:
        """把 exploit 源码铺到工作区，返回 <root>/src/exploit。

        覆盖策略：
          src/ 与 assets/            —— 每次按大小差异同步（模板，用户不该改）
          targets/onyx-leaf5/        —— 同样同步（模板 target.h 的来源）
          targets/migr-*/            —— 绝不覆盖（用户适配出来的成果）
        """
        src = self.bundled_exploit()
        if not src:
            raise AdaptError(
                "找不到 exploit 源码",
                hint="包里没打 assets/exploit_src；开发模式下需要同级的 "
                     "leaf5-ghostlock/exploit")

        dst = self.exploit_dir
        dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for sub in ("src", "assets"):
            s = src / sub
            if not s.is_dir():
                continue
            d = dst / sub
            d.mkdir(parents=True, exist_ok=True)
            # 必须递归：src/kernelsnitch/ 里的头文件是编译依赖
            for root, _dirs, files in os.walk(s):
                rel = os.path.relpath(root, s)
                tdir = d if rel == "." else d / rel
                tdir.mkdir(parents=True, exist_ok=True)
                for name in files:
                    if ".bak" in name:
                        continue
                    sp = Path(root) / name
                    dp = tdir / name
                    need = (force or not dp.is_file()
                            or sp.stat().st_size != dp.stat().st_size)
                    if need:
                        shutil.copyfile(sp, dp)
                        n += 1

        t = src / "targets" / "onyx-leaf5" / "target.h"
        if t.is_file():
            td = dst / "targets" / "onyx-leaf5"
            td.mkdir(parents=True, exist_ok=True)
            dp = td / "target.h"
            if force or not dp.is_file() or t.stat().st_size != dp.stat().st_size:
                shutil.copyfile(t, dp)
                n += 1

        # src/assets/wallpaper.webp 是 .incbin 的相对目标，必须在 src/ 下
        ensure_wallpaper(dst)
        (dst / "out" / "exploit" / "aarch64" / "embed").mkdir(
            parents=True, exist_ok=True)
        self._say("  源码工作区同步了 %d 个文件 → %s" % (n, dst) if n
                  else "  源码工作区已是最新 → %s" % dst)
        return dst

    def template(self) -> Path:
        t = self.exploit_dir / "targets" / "onyx-leaf5" / "target.h"
        if not t.is_file():
            self.ensure_src()
        if not t.is_file():
            raise AdaptError("模板缺失：%s" % t)
        return t

    # ---------------- 设备信息 ----------------
    def device_meta(self) -> dict:
        """读设备指纹与内核串。读不到返回空 dict，不抛。"""
        out = {}
        if not self.adb:
            return out
        try:
            out["fingerprint"] = self.adb.getprop("ro.build.fingerprint") or ""
            out["model"] = self.adb.getprop("ro.product.model") or ""
            out["kernel"] = self.adb.sh_out(
                "uname", "-r", timeout=self.cfg.timeout_short).strip()
        except L5Error:
            return {}
        return out

    # ---------------- 自动定标手工宏 ----------------
    def calibrate_image(self, src) -> dict:
        """从裸 Image 里把「指令里就写着」的手工宏扫出来。

        能扫的：TASK_REAL_CRED_OFF / TASK_CRED_OFF / PRCTL_FRAME_TOTAL。
        扫不出的（WAITER_LOCAL_OFF 只给提示，PRCTL_WAITER_WORD_DEFAULT 是
        语义常量）交给 build_target_h 留占位符。

        ELF 源直接跳过：ELF 里符号是绝对地址，文件偏移与 Image 不同，
        硬扫会得出看着对、其实错位的数。宁可少填。
        """
        try:
            with open(src, "rb") as f:
                if f.read(4) == b"\x7fELF":
                    self._say("  （源是 ELF，跳过指令定标：只对裸 Image 有效）")
                    return {}
        except OSError:
            return {}

        from ..core import imageksym
        try:
            syms = imageksym.ImageKallsyms(str(src)).symbols()
        except Exception as ex:                    # 不是内核镜像就安静退出
            self._say("  （符号表读不出来，跳过指令定标：%s）" % ex)
            return {}
        if "_text" not in syms:
            return {}
        try:
            buf = Path(src).read_bytes()
        except OSError:
            return {}
        got, _ev = ainsn.calibrate(buf, syms, log=self._say)
        return got

    # ---------------- 生成 target.h ----------------
    def gen(self, src_path, fingerprint: str = "", kernel: str = "",
            tid: str | None = None, inherit_manual: bool = True,
            force_inherit: bool = False, keep: bool = False,
            log=None) -> GenResult:
        """把一份镜像/kallsyms 变成 targets/<id>/target.h。

        inherit_manual=True（默认）：若提取到的符号偏移与模板完全一致，
        说明这就是模板对应的那次构建，4 个手工宏直接沿用模板值 ——
        这种情况「同源重编」，没有理由让人再去反汇编一遍。
        有任何一处不同就不继承，全部留占位符（宁可多点一步，也不拿错值去
        patch 内核）。

        force_inherit=True：明知有差异也沿用模板的 4 个值。只在「差异全是
        源码没引用的死宏」时该用 —— 比如出厂 target.h 里 10 个旧构建的
        偏移，内核里是别的值，但 .c 里一次都没引用。
        """
        say = log or self._say
        exploit = self.ensure_src()
        tmpl = self.template()

        src, how, banner = kimage.resolve_syms_source(src_path, log=say)
        if how != "direct":
            say("  （自动解包：%s）" % how)

        dev = self.device_meta()
        kernel, fingerprint = pick_identity(
            banner, dev, kernel=kernel, fingerprint=fingerprint)

        if banner:
            say("  镜像 banner: %s" % banner)
            if dev.get("kernel"):
                v, why, adv = kimage.banner_match(banner, dev["kernel"])
                say("  %s %s" % (kimage.VERDICT_TAG[v], why))
                if v != "same":
                    # 离线适配别的型号的 ROM 时这条必然触发，不是错误：
                    # 它只说「这份镜像不来自手上这台机器」。
                    say("      %s" % adv)
            if (kimage.parse_banner(banner) or {}).get("version"):
                say("  记入 target 的内核串: %s（来源：镜像 banner）" % kernel)
        else:
            say("  [!] 读不到 linux_banner —— 这个文件可能不是完整内核镜像")

        offs, miss = elfsyms.extract_offsets(src, tmpl, log=say)
        say("  提到 %d 个符号偏移，%d 个宏没有同名符号（多为硬编码槽位）"
            % (len(offs), len(miss)))

        tid = tid or suggest_id(fingerprint, kernel or Path(src_path).stem)
        tdir = self.targets_root / tid
        tdir.mkdir(parents=True, exist_ok=True)

        manual, manual_mode = {}, "template"
        tm = template_manuals(tmpl)
        if force_inherit and tm:
            manual, manual_mode = tm, "forced"
            say("  [!] --inherit：明知与模板有差异，仍沿用模板的 4 个手工宏；"
                "只在这些宏没被源码引用时才是安全的")
        elif inherit_manual:
            base = template_offsets(tmpl)
            # 「同源」= 镜像里每个宏的值都等于模板原值。差一个就说明换版本了，
            # 手工宏必须重新定标，不能沿用。
            same = bool(offs) and all(base.get(k) == v for k, v in offs.items())
            if tm and same and not miss_manual(tm):
                manual, manual_mode = tm, "inherited"

        # 3 个手工宏（real_cred / cred / prctl 帧）本来就写在指令立即数里，
        # 先扫一遍：扫到的按镜像写死，扫不到的才留占位符让人反汇编。
        # 这一步让「换一台不同构建的机器」从「四个宏全靠人肉」变成「零人工」。
        auto = self.calibrate_image(src)
        if auto:
            for k, v in auto.items():
                old = manual.get(k)
                if old is not None and old != v:
                    say("  [!] %s：模板 0x%X，镜像 0x%X —— 按镜像写"
                        % (k, old, v))
            manual = dict(manual, **auto)

            # PRCTL_WAITER_WORD_DEFAULT 是「从 prctl 栈上第几个 word 取 waiter」，
            # 只由帧大小和 waiter 布局决定。这两样都定标成功、且与模板一致时，
            # 沿用模板值是有依据的推断，不是猜；不一致就继续留占位符。
            if ("WAITER_LOCAL_OFF" in auto and "PRCTL_FRAME_TOTAL" in auto
                    and "PRCTL_WAITER_WORD_DEFAULT" not in manual
                    and tm.get("WAITER_LOCAL_OFF") == auto["WAITER_LOCAL_OFF"]
                    and tm.get("PRCTL_FRAME_TOTAL") == auto["PRCTL_FRAME_TOTAL"]):
                manual["PRCTL_WAITER_WORD_DEFAULT"] = tm["PRCTL_WAITER_WORD_DEFAULT"]
                say("  [推断] PRCTL_WAITER_WORD_DEFAULT = 0x%X"
                    "（waiter 0x%X 与帧 0x%X 都与模板一致）"
                    % (tm["PRCTL_WAITER_WORD_DEFAULT"],
                       auto["WAITER_LOCAL_OFF"], auto["PRCTL_FRAME_TOTAL"]))

        unf, nf, diffs = build_target_h(
            tmpl, tdir / "target.h", offs, fingerprint, kernel,
            log=say, syms_mode="keep" if keep else "trust",
            manual_values=manual)

        sfp = write_sym_fp(src, tdir, banner)
        if sfp:
            say("  符号指纹已存 %s（之后同一台设备不用 root 就能被认出来）" % sfp)

        # 还没编译的 target 也先登记，list 里看得见「差编译」这一步
        save_manifest(tdir, {
            "id": tid, "fingerprint": fingerprint, "kernel": kernel,
            "banner": banner, "manual": manual_mode,
            "auto": {k: "0x%X" % v for k, v in auto.items()},
            "offsets": len(offs), "diffs": len(diffs),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": os.path.abspath(str(src_path)),
        })
        return GenResult(tid=tid, tdir=tdir, unfilled=unf, diffs=diffs,
                         offsets=len(offs), missing=miss, banner=banner,
                         symfp=sfp, manual=manual_mode, auto=auto)

    # ---------------- 编译 ----------------
    def compile(self, tdir, out_so=None, cc: str | None = None,
                progress=None, cancel=None, log=None) -> tuple[Path, str, int]:
        say = log or self._say
        exploit = self.ensure_src()
        tdir = Path(tdir)
        left = placeholders(tdir)
        if left:
            raise AdaptError(
                "target.h 还差 %d 个手工宏没填：%s" % (len(left), ", ".join(left)),
                hint="用 `l5tk adapt fill %s --set MACRO=0x..` 或界面上的填表" % tdir.name)
        if not cc:
            _, cc = ndk.install(dest_root=str(self.ndk_root), log=say,
                                progress=progress, cancel=cancel)
        out = Path(out_so) if out_so else (self.root / "out" / ("%s.so" % tdir.name))
        so, md5, size = compile_preload(exploit, tdir, cc, str(out),
                                        log=say, cancel=cancel)
        return Path(so), md5, size

    # ---------------- target 仓库 ----------------
    def _legacy_root(self) -> Path | None:
        p = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / \
            "Leaf5RootTool" / "targets"
        return p if p.is_dir() else None

    def list_targets(self, include_legacy: bool = True) -> list[Target]:
        """返回所有已登记 target，按 created 倒序；本工具的排在 V1 遗产前面。"""
        out: list[Target] = []
        roots = [(self.targets_root, False)]
        if include_legacy:
            lg = self._legacy_root()
            if lg and lg.resolve() != self.targets_root.resolve():
                roots.append((lg, True))
        for root, legacy in roots:
            if not root.is_dir():
                continue
            for d in root.iterdir():
                mf = d / "manifest.json"
                if not d.is_dir() or not mf.is_file():
                    continue
                try:
                    meta = json.loads(mf.read_text(encoding="utf-8"))
                except Exception:
                    continue
                meta["id"] = d.name
                out.append(Target(d.name, d, meta, legacy=legacy))
        out.sort(key=lambda t: str(t.meta.get("created") or ""), reverse=True)
        return out

    def pick(self, fingerprint: str = "", kernel: str = "",
             sym_digest: str = "") -> Target | None:
        """挑一份能用的 target，只返回已经编出 preload.so 的。

        优先级：
          ① 符号序列哈希完全一致      —— 同一次构建，最可信
          ② 设备指纹一致              —— 同固件
          ③ 内核版本串一致            —— 同版本不同构建，次优
        """
        cands = [t for t in self.list_targets() if t.has_so()]
        if sym_digest:
            for t in cands:
                if t.meta.get("sym_digest") == sym_digest:
                    return t
        if fingerprint:
            for t in cands:
                if t.fingerprint and t.fingerprint == fingerprint:
                    return t
        if kernel:
            for t in cands:
                if t.kernel and t.kernel == kernel:
                    return t
        return None

    def for_device(self) -> Target | None:
        dev = self.device_meta()
        return self.pick(fingerprint=dev.get("fingerprint", ""),
                         kernel=dev.get("kernel", ""))

    def install(self, tid: str, so_path, meta: dict | None = None) -> Target:
        """把编好的 preload.so 收进 targets/<tid>/，写 manifest"""
        tdir = self.targets_root / tid
        tdir.mkdir(parents=True, exist_ok=True)
        dst = tdir / "preload.so"
        shutil.copyfile(so_path, dst)
        m = dict(meta or {})
        # gen 阶段存下的东西并进来（install 的人不必再传一遍）
        for name in ("symfp.json", "manifest.json"):
            p = tdir / name
            if p.is_file():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                for k, v in data.items():
                    m.setdefault(k, v)
        m["id"] = tid
        m["so_md5"] = hashlib.md5(dst.read_bytes()).hexdigest()
        m["so_size"] = dst.stat().st_size
        m["created"] = m.get("created") or time.strftime("%Y-%m-%dT%H:%M:%S")
        save_manifest(tdir, m)
        return Target(tid, tdir, m)

    def active(self) -> str:
        """用户手动指定的 target id（`adapt use`）。空表示按设备自动挑。"""
        p = self.root / "active.json"
        try:
            return str(json.loads(p.read_text(encoding="utf-8")).get("id") or "")
        except Exception:
            return ""

    def use(self, tid: str) -> Target:
        t = next((x for x in self.list_targets() if x.id == tid), None)
        if not t:
            raise AdaptError("没有这个 target：%s" % tid,
                             hint="先 `l5tk adapt list` 看有哪些")
        if not t.has_so():
            raise AdaptError("%s 还没有 preload.so" % tid,
                             hint="先 `l5tk adapt build %s`" % tid)
        p = self.root / "active.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"id": tid, "at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return t


# ----------------------------------------------------------------------
# 符号指纹（供 install / pick 用）
# ----------------------------------------------------------------------
def sym_fingerprint(src_path) -> dict:
    """对"符号来源"算构建指纹，返回可直接塞进 manifest 的 dict。

    只有 kallsyms 文本能算：它的符号顺序是内核按地址排好序后输出到
    /proc/kallsyms 的，同一份构建必然一致，而且地址被 kptr_restrict
    清零也不影响名字序列 —— 这就是无 root 也能认出设备内核的基础。

    ELF 不算（返回 {}）：symtab 的顺序是链接顺序，跟 kallsyms 的地址顺序
    不是一回事，两份值放一起会误判成"不同构建"。
    """
    if not elfsyms.looks_like_kallsyms(src_path):
        return {}
    try:
        from ..core import fp as fpmod
        st = fpmod.fingerprint_kallsyms_file(src_path)
    except Exception:
        return {}
    return {"sym_digest": st["sym_digest"],
            "sym_count": st["kernel_syms"],
            "sym_total": st["total"],
            "sym_module": st["module_syms"]}


def write_sym_fp(src_path, tdir, banner=None) -> Path | None:
    """把符号指纹存到 targets/<id>/symfp.json。

    banner 一起存：同一个符号序列可能对应多个构建（序列一样但代码有改），
    banner 的 commit 是第二个交叉维度，比对时先用它筛。
    """
    meta = sym_fingerprint(src_path)
    if not meta:
        return None
    if banner:
        b = kimage.parse_banner(banner)
        meta["kernel_banner"] = banner
        meta["kernel_commit"] = b.get("commit")
        meta["kernel_build"] = b.get("build")
        meta["kernel_version"] = b.get("version")
    tdir = Path(tdir)
    tdir.mkdir(parents=True, exist_ok=True)
    p = tdir / "symfp.json"
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def save_manifest(tdir, meta: dict) -> Path:
    tdir = Path(tdir)
    tdir.mkdir(parents=True, exist_ok=True)
    p = tdir / "manifest.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def miss_manual(tm: dict) -> bool:
    """模板里 4 个手工宏是否都取到了值（缺一个就不能整套继承）"""
    return len(tm) < len(MANUAL_MACROS)
