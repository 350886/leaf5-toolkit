"""Magisk 全自动安装：设备内打补丁 + dd 写回。

为什么不走 `fastboot flash`：ONYX 的 abl 把 flash 子命令裁掉了，
`fastboot flash boot_b` 实测返回 `remote: 'unknown command'`。
所以写回只能 dd —— 而有 dd 权限的前提是已经 root，
这就是为什么这条路线必须排在拿到 root 之后。

整条链的四个"不许继续"点，任何一条不满足都立刻停、且分区一个字节没动：

  1. boot 备份没拉回 PC 并校验 md5 一致  → 不 patch
  2. 备份不是合法 boot 镜像（无 ANDROID!）→ 不 patch
  3. 打出来的 new-boot.img 的 kernel 段长度变了 → 不写回
  4. 写回后回读 md5 与镜像不符 → 不重启，把备份路径直接打给用户

第 3 条最容易被忽略：md5 只证明"写进去的和生成的一样"，
证明不了"生成的是对的"。kernel_size 变了说明分区拿错或补丁工具版本不对，
这时候写进去必砖。
"""

from __future__ import annotations

import hashlib
import struct
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from ..core.adb import Adb
from ..core.errors import AdbError, DeviceError, ImageError, L5Error
from ..core.result import Outcome
from ..logx import Logger
from .root import parse_boot

# APK 里的库名 -> 设备上的可执行名。
# Magisk 把每个二进制都伪装成 .so 塞在 lib/ 下（Android 的 noexec 策略逼的）。
APK_TOOLS = {
    "lib/arm64-v8a/libmagiskboot.so": "magiskboot",
    "lib/arm64-v8a/libmagiskinit.so": "magiskinit",
    "lib/arm64-v8a/libmagisk.so": "magisk64",
    "lib/armeabi-v7a/libmagisk.so": "magisk32",
    "lib/arm64-v8a/libmagiskpolicy.so": "magiskpolicy",
    "lib/arm64-v8a/libinit-ld.so": "init-ld",
    "lib/arm64-v8a/libbusybox.so": "busybox",
    "assets/boot_patch.sh": "boot_patch.sh",
    "assets/util_functions.sh": "util_functions.sh",
}

# 「有就抠」的项。31.0 的 boot_patch.sh 里有
#   ./magiskboot compress=xz stub.apk stub.xz      （第 179 行）
#   add 0644 overlay.d/sbin/stub.xz stub.xz        （第 197 行）
# 缺 stub.apk 打出来的 ramdisk 里没有 App 存根，Magisk App 装了也看不到超级用户。
# 31.1 把这个文件从 APK 里整个删了，所以不能放 APK_TOOLS（那会直接报"版本不对"）。
APK_EXTRA = {
    "assets/stub.apk": "stub.apk",
}

# 默认装哪个版本。31.1 把 su 的授权请求删了：daemon 侧只剩 app_notify()，
# policies 表里没有的 uid 一律 SuPolicy::default() = Deny，客户端看到
# Permission denied / exit=13 —— 跟"root 没做好"长得一模一样。
# SuRequestActivity 在 26.3~31.0 之间都有，所以钉在 31.0。
PREFERRED_APK = "Magisk-v31.0.apk"

# /data/adb/magisk 里必须有 busybox —— setup_magisk_env 只看它在不在，
# 不满足直接 abort，模块永远不挂载。这一步必须在重启之前做。
RUNTIME = ("busybox", "magiskboot", "magiskinit", "magisk32", "magisk64",
           "magiskpolicy", "init-ld", "boot_patch.sh", "util_functions.sh")

DEV_ROOT = "/data/local/tmp"
DEV_MGK = DEV_ROOT + "/mgk"
DEV_KITS = DEV_MGK + "/kits"

# ---- ONYX 的 services.jar 补丁 -------------------------------------------
# AOSP 原版：proc == null 就直接 return；文石 4.2.3 把这个分支改成跳去自己的
# WebView 使用追踪，一进去就 iget-object v0, v1, ProcessRecord;->info，
# v1 为 null ⇒ NPE。Magisk 的守护进程由 app_process 起、不在 PidMap 里，
# 所以 proc 恒 null ⇒ Magisk App 每次都在启动画面崩掉（root 本身正常）。
MOD_ID = "boox-ams-fix"
MOD_DIR = "/data/adb/modules/" + MOD_ID
DEX_OFF = 0x2B2FEE
DEX_OLD = bytes([0x38, 0x01, 0x33, 0x00])   # if-eqz v1, +0x33（文石追踪分支）
DEX_NEW = bytes([0x38, 0x01, 0x3F, 0x00])   # if-eqz v1, +0x3F（return-void）
DEX_FW = "4.2.3-rel_0928"

MOD_PROP = """id={id}
name=Boox AMS NPE Fix (Leaf5 {fw})
version=v1.0-leaf5-{fw}
versionCode=1
author=l5tk
description=修 ONYX services.jar 里 ActivityManagerService.addPackageDependency 对 null ProcessRecord 的解引用。不修的话 Magisk App 会卡在启动画面（root 本身正常）。单字节补丁 classes.dex 0x{off:x}: {old} -> {new}。
""".format(id=MOD_ID, fw=DEX_FW, off=DEX_OFF,
           old=DEX_OLD.hex(" "), new=DEX_NEW.hex(" "))


def find_apk(cfg) -> Path | None:
    """内置 Magisk APK：EXE 同级 -> 打包内。同级优先，换版本不用重新打包。

    两轮扫描，不是按目录顺序撞运气：目录里同时放着 31.0 和 31.1 时，
    原来那句 `sorted(...)[0]` 靠字符串序恰好选到 31.0 —— 一旦谁重命名
    （Magisk-31.1-official.apk）就会静默换成没弹框的版本。先把钉住的
    版本在所有候选目录里找遍，找不到才退回「文件名最大的那个」。
    """
    bases = (cfg.base_dir, cfg.bundle_dir)

    for base in bases:                       # 第一轮：钉住的版本
        p = Path(base) / "assets" / "magisk" / PREFERRED_APK
        if p.is_file():
            return p

    fallback = None                          # 第二轮：任意可用 APK
    for base in bases:
        d = Path(base) / "assets" / "magisk"
        if not d.is_dir():
            continue
        apks = sorted((p for p in d.glob("*.apk") if p.is_file()),
                      key=lambda p: p.name)
        if apks:
            fallback = apks[-1]
            break
    return fallback


def extract_tools(apk: Path, dest: Path) -> dict[str, Path]:
    """把 magiskboot/boot_patch.sh 等从 APK 里抠出来。

    为什么不在 PC 上跑 boot_patch.sh：magiskboot 是 aarch64 ELF，
    Windows 执行不了。只能推到设备上跑。
    """
    dest.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    with zipfile.ZipFile(apk) as z:
        names = set(z.namelist())
        missing = [a for a in APK_TOOLS if a not in names]
        if missing:
            raise DeviceError(
                "APK 里缺 %d 个文件：%s" % (len(missing), ", ".join(missing[:3])),
                hint="Magisk 版本不对，或这根本不是 Magisk APK")
        for arc, dst in APK_TOOLS.items():
            p = dest / dst
            with z.open(arc) as src:
                p.write_bytes(src.read())
            out[dst] = p

        # 「有就抠」的项：stub.apk 只有 31.0 及更早才有
        for arc, dst in APK_EXTRA.items():
            if arc not in names:
                continue
            p = dest / dst
            with z.open(arc) as src:
                p.write_bytes(src.read())
            out[dst] = p

    # boot_patch.sh 第 178 行无条件跑 `./magiskboot compress=xz magisk magisk.xz`，
    # 而 APK 里那份叫 magisk64（官方 App 的准备阶段会替它复制过去）。
    # 少了这个别名，脚本不报错，只是打出来的 ramdisk 里没有 magisk 二进制 ——
    # 表现是"刷完开机有 Magisk App、su 却一直 Permission denied"。
    if "magisk64" in out and "magisk" not in out:
        alias = dest / "magisk"
        alias.write_bytes(out["magisk64"].read_bytes())
        out["magisk"] = alias
    return out


def patch_services_jar(src: Path, dst: Path) -> tuple[Path, bool]:
    """把 ONYX services.jar 里 classes.dex 的 null 分支改回 return-void。

    返回 (产物路径, 是否真的改了)。改完必须重算 DEX 头的两个校验，
    否则 ART 直接拒绝加载 —— 这一步漏了的表现是"开机卡在 logo"，
    比不修还糟。
    """
    with zipfile.ZipFile(src) as zin:
        if "classes.dex" not in zin.namelist():
            raise ImageError("services.jar 里没有 classes.dex")
        raw = zin.read("classes.dex")
        if len(raw) <= DEX_OFF + 4:
            raise ImageError("classes.dex 比补丁点还短，固件不对")
        if raw[:4] != b"dex\n":
            raise ImageError("classes.dex 魔数不对")

        cur = raw[DEX_OFF:DEX_OFF + 4]
        if cur == DEX_NEW:
            dst.write_bytes(src.read_bytes())
            return dst, False
        if cur != DEX_OLD:
            raise ImageError(
                "0x%x 处不是预期字节（读到 %s，期望 %s）"
                % (DEX_OFF, cur.hex(), DEX_OLD.hex()),
                hint="这个偏移只对固件 %s 成立，换版本必须重算" % DEX_FW)

        buf = bytearray(raw)
        buf[DEX_OFF:DEX_OFF + 4] = DEX_NEW
        buf[0x0C:0x20] = hashlib.sha1(bytes(buf[32:])).digest()
        buf[0x08:0x0C] = struct.pack("<I", zlib.adler32(bytes(buf[12:])) & 0xFFFFFFFF)

        with zipfile.ZipFile(dst, "w") as zout:
            for item in zin.infolist():
                data = bytes(buf) if item.filename == "classes.dex" else zin.read(item.filename)
                zi = zipfile.ZipInfo(item.filename, date_time=item.date_time)
                zi.compress_type = item.compress_type
                zi.external_attr = item.external_attr
                zout.writestr(zi, data)
    return dst, True


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def assert_kernel_unchanged(stock, new) -> None:
    """写回之前的最后一道闸。

    md5 只能证明「写进去的和生成的一样」，证明不了「生成的是对的」。
    kernel 段长度变了说明分区拿错，或者补丁工具版本对不上 ——
    这时候写进去必砖，必须在 dd 之前拦住。
    """
    if new.kernel_size != stock.kernel_size:
        raise DeviceError(
            "kernel 段变了：%d → %d。patch 异常或分区拿错，禁止写入（分区未改动）"
            % (stock.kernel_size, new.kernel_size),
            hint="确认 boot_patch.sh 与 Magisk 版本匹配；换过内核对不上也会这样")


@dataclass
class Plan:
    """动设备之前先把要做什么列清楚，--dry-run 只打印它。"""
    apk: Path
    part: str
    device: str
    size: int
    stock: Path
    patched: Path

    def describe(self) -> str:
        return ("分区 %s（%s，%.0f MB）\n"
                "  原版备份 → %s\n"
                "  补丁产物 → %s\n"
                "  打补丁用的 APK → %s"
                % (self.part, self.device, self.size / 1048576.0,
                   self.stock, self.patched, self.apk))


class MagiskOps:
    def __init__(self, adb: Adb, log: Logger):
        self.adb = adb
        self.log = log
        self.cfg = adb.cfg

    # ---------------- 前置 ----------------
    def _su(self) -> str:
        ok, why = self.adb.su_probe()
        if not ok:
            raise DeviceError(
                "没有可用的 su，没法 dd 读写 boot 分区：%s" % (why or "未知"),
                hint="这条路线的前提是已经 root（V1 的 exploit 或已有 Magisk）")
        return "su"

    def resolve_target(self, part: str | None = None) -> tuple[str, str]:
        """定下要写的分区与块设备路径。"""
        names = self.adb.sh_out("ls", "/dev/block/by-name/",
                                timeout=self.cfg.timeout_short).split()
        slot = (self.adb.getprop("ro.boot.slot_suffix") or "").lstrip("_")
        base = part or "boot"
        if base in names:
            name = base
        elif slot and "%s_%s" % (base, slot) in names:
            name = "%s_%s" % (base, slot)
        elif names:
            raise DeviceError(
                "分区表里没有 %s（有 %s）" % (base, " ".join(sorted(names)[:8])),
                hint="用 --part 指定；A/B 机器要写成 boot_a / boot_b")
        else:
            # 读不到分区表时按 slot 推断，但把依据写出来
            if not slot:
                raise DeviceError("读不到分区表，也读不到当前槽位",
                                  hint="用 --part boot_a / boot_b 明确指定")
            name = "%s_%s" % (base, slot)
        return name, "/dev/block/by-name/" + name

    # ---------------- 主流程 ----------------
    def install_full(self, part: str | None = None, with_module: bool = False,
                     reboot: bool = True, dry_run: bool | None = None) -> Outcome:
        """完整流程：备份 → 设备内 patch → 校验 → dd 写回 → 回读校验 → 模块 → 装 App → 重启。"""
        dry = self.cfg.dry_run if dry_run is None else dry_run
        su = self._su()
        apk = find_apk(self.cfg)
        if apk is None:
            raise DeviceError(
                "找不到内置 Magisk APK",
                hint="把 Magisk-*.apk 放进 EXE 同级的 assets\\magisk\\，"
                     "或用 python build.py 重新打包")

        name, dev = self.resolve_target(part)
        # blockdev 读块设备大小比 stat 可靠：stat 对块设备返回的是 inode 大小（0）
        try:
            size = int(self.adb.sh_out("blockdev", "--getsize64", dev,
                                       timeout=self.cfg.timeout_short, root=True).strip())
        except ValueError:
            size = -1
        if size <= 0:
            raise DeviceError("读不到 %s 的大小" % dev,
                              hint="确认这台机器已经 root，且分区名对得上")

        stamp = _stamp()
        bak_dir = self.cfg.work_dir
        bak_dir.mkdir(parents=True, exist_ok=True)
        plan = Plan(apk=apk, part=name, device=dev, size=size,
                    stock=bak_dir / ("stock_%s_%s.img" % (name, stamp)),
                    patched=bak_dir / ("patched_%s_%s.img" % (name, stamp)))

        self.log.info("== Magisk 全自动安装 ==")
        self.log.info("  目标 %s  %.0f MB  %s" % (name, size / 1048576.0, dev))
        if dry:
            self.log.info("  [dry-run] 只走到这里，分区未改动")
            return Outcome(True, "dry_run",
                           "dry-run：计划已列出，设备一个字节没动",
                           data={"plan": plan.describe()})

        # 1) 备份：设备 dd → pull → md5 校对。备份不成立就绝不往下走。
        self.log.info("  备份原版 boot…")
        self.adb.sh("mkdir", "-p", DEV_MGK, timeout=self.cfg.timeout_short, root=True)
        r = self.adb.sh("dd", "if=" + dev, "of=%s/stock.img" % DEV_MGK, "bs=1M",
                        timeout=self.cfg.timeout_flash, root=True)
        if not r.ok:
            raise DeviceError("dd 备份失败：%s" % (r.text[:200]),
                              hint="分区被占用时 dd 会失败；重启再试")
        self.adb.sh("chmod", "644", "%s/stock.img" % DEV_MGK,
                    timeout=self.cfg.timeout_short, root=True)
        dev_md5 = self.adb.sh_out("md5sum", "%s/stock.img" % DEV_MGK,
                                  timeout=self.cfg.timeout_normal, root=True).split()
        if not dev_md5:
            raise DeviceError("备份的 md5 读不到，不敢继续（分区未改动）")

        self.log.info("  拉回 PC 留档…")
        self.adb.pull("%s/stock.img" % DEV_MGK, str(plan.stock))
        if not plan.stock.is_file():
            raise DeviceError("备份没拉回来（分区未改动）")
        local_md5 = md5_of(plan.stock)
        if local_md5 != dev_md5[0]:
            raise DeviceError(
                "备份校验失败：设备 %s != 本地 %s（分区未改动）"
                % (dev_md5[0], local_md5))
        stock_img = parse_boot(plan.stock)   # 不是 boot 镜像直接抛
        self.log.info("  原版 md5=%s  %s" % (local_md5, stock_img.describe()))

        # 2) 工具从 APK 抠出来推上去
        with tempfile.TemporaryDirectory(prefix="l5tk_mgk_") as tmp:
            tools = extract_tools(apk, Path(tmp) / "kits")
            self.adb.sh("mkdir", "-p", DEV_KITS, timeout=self.cfg.timeout_short, root=True)
            # mkdir 是 root 跑的，建出来是 755；adb push 走的是 shell 身份，
            # 往 755 的目录里写会直接 Permission denied。先放开再推。
            self.adb.sh("chmod", "777", DEV_KITS, timeout=self.cfg.timeout_short, root=True)
            for fname, local in tools.items():
                pr = self.adb.push(str(local), "%s/%s" % (DEV_KITS, fname))
                if not pr.ok:
                    raise DeviceError("推 %s 失败：%s" % (fname, pr.text[:150]))
            self.adb.sh("chmod", "755", DEV_KITS + "/*",
                        timeout=self.cfg.timeout_short, root=True)
            self.log.info("  已推 %d 个工具到 %s" % (len(tools), DEV_KITS))

            # 3) 设备上打补丁
            self.log.info("  跑 boot_patch.sh（30~90 秒）…")
            cmd = ("cd %s && cp -f %s/* . && chmod 755 * && "
                   "KEEPVERITY=true KEEPFORCEENCRYPT=true PATCHVBMETAFLAG=false "
                   "BOOTMODE=false sh boot_patch.sh stock.img" % (DEV_MGK, DEV_KITS))
            r = self.adb.sh(cmd, timeout=self.cfg.timeout_flash, root=True)
            tail = [ln for ln in (r.out or r.err).splitlines() if ln.strip()][-4:]
            for ln in tail:
                self.log.info("    | " + ln.strip()[:160])
            if "new-boot.img" not in self.adb.sh_out("ls", DEV_MGK + "/new-boot.img",
                                                     timeout=self.cfg.timeout_short,
                                                     root=True):
                raise DeviceError("boot_patch.sh 没产出 new-boot.img",
                                  hint="上面四行是它的输出；常见原因是 su 权限被拒")

            # 4) 拉回验 kernel 段 —— md5 证明不了"生成的是对的"
            self.adb.sh("chmod", "644", DEV_MGK + "/new-boot.img",
                        timeout=self.cfg.timeout_short, root=True)
            self.adb.pull(DEV_MGK + "/new-boot.img", str(plan.patched))
            new_img = parse_boot(plan.patched)
            assert_kernel_unchanged(stock_img, new_img)
            self.log.info("  kernel 未变 %d；ramdisk %d → %d（%+d）"
                          % (new_img.kernel_size, stock_img.ramdisk_size,
                             new_img.ramdisk_size,
                             new_img.ramdisk_size - stock_img.ramdisk_size))

            # 5) 写回 + 回读校验
            self.log.info("  写回 %s…" % name)
            w = self.adb.sh("dd", "if=%s/new-boot.img" % DEV_MGK, "of=" + dev,
                            "bs=1M", "conv=fsync",
                            timeout=self.cfg.timeout_flash, root=True)
            if not w.ok:
                raise DeviceError("写回失败：%s\n原版备份在 %s（别重启，先确认）"
                                  % (w.text[:200], plan.stock))

            m1 = self.adb.sh_out("md5sum", DEV_MGK + "/new-boot.img",
                                 timeout=self.cfg.timeout_normal, root=True).split()
            blk = (new_img.size + 1048575) // 1048576
            m2 = self.adb.sh_out(
                "dd if=%s bs=1M count=%d 2>/dev/null | head -c %d | md5sum"
                % (dev, blk, new_img.size),
                timeout=self.cfg.timeout_flash, root=True).split()
            if m1 and m2 and m1[0] != m2[0]:
                raise DeviceError(
                    "回读 md5 不符（%s != %s），写回没成功。先别重启，原版在 %s"
                    % (m1[0], m2[0], plan.stock))
            self.log.info("  回读 md5 一致 %s" % (m1[0] if m1 else "?"))

            # 6) /data/adb/magisk 运行时 —— 必须在重启前
            self.adb.sh("mkdir", "-p", "/data/adb/magisk",
                        timeout=self.cfg.timeout_short, root=True)
            for f in RUNTIME:
                self.adb.sh("cp", "-f", "%s/%s" % (DEV_KITS, f),
                            "/data/adb/magisk/" + f,
                            timeout=self.cfg.timeout_short, root=True)
            self.adb.sh("chmod", "755",
                        "/data/adb/magisk/busybox", "/data/adb/magisk/magiskboot",
                        "/data/adb/magisk/magiskinit", "/data/adb/magisk/magisk32",
                        "/data/adb/magisk/magisk64", "/data/adb/magisk/magiskpolicy",
                        "/data/adb/magisk/init-ld",
                        timeout=self.cfg.timeout_short, root=True)
            cnt = self.adb.sh_out("ls", "/data/adb/magisk", root=True).split()
            self.log.info("  /data/adb/magisk 现有 %d 项" % len(cnt))

        # 7) AMS 修复模块（可选，与 Magisk 同一次重启生效）
        module_msg = ""
        if with_module:
            module_msg = self._module_or_skip()

        # 8) 装 App
        self.log.info("  安装 Magisk App…")
        ir = self.adb.install(str(apk))
        if not ir.ok:
            raise DeviceError("Magisk App 安装失败：%s" % ir.text[:200],
                              hint="boot 已写好，重启后 root 仍会生效")

        if reboot:
            self.log.info("  重启，Magisk 这时才接管…")
            self.adb.sh("reboot", timeout=self.cfg.timeout_short, root=True)
            back = self._wait_back()
            self.log.info("  %s" % ("已回到 adb" if back else "180 秒内没回到 adb"))

        ok, why = self.adb.su_probe()
        return Outcome(
            True, "magisk_installed",
            "Magisk 已写入 %s 并装好 App%s" % (name, "；" + module_msg if module_msg else ""),
            reasons=[] if ok else ["重启后暂时取不到 su：打开 Magisk → 超级用户，"
                                   "把 Shell 的开关打开（%s）" % (why or "未知")],
            data={"part": name, "device": dev,
                  "stock": str(plan.stock), "patched": str(plan.patched),
                  "su": ok, "module": module_msg},
        )

    # ---------------- AMS 模块 ----------------
    def _module_or_skip(self) -> str:
        """铺 AMS 修复模块，失败降级成一句提示。

        走到这一步时 boot 已经写回并回读校验过，root 马上就会生效。
        补丁点 `0x2b2fee` 只对 Leaf5 的 `4.2.3-rel_0928`（A13 framework）成立；
        Tab 三台是 A11 framework，同一偏移的字节必然不符 —— 那时候正确的结果是
        「root 成功、模块跳过」，不是「刷机失败」。把异常吞在这里，
        让 install_full 继续往装 App、重启走。
        """
        try:
            return self._prepare_module()
        except L5Error as e:
            self.log.warn("  AMS 模块未铺：%s" % e.message)
            return "AMS 模块跳过（%s）" % e.message

    def _prepare_module(self) -> str:
        """把补丁后的 services.jar 铺成 Magisk 模块。

        必须在重启前完成：模块由 post-fs-data 阶段 magic mount，
        重启后 /system/framework/services.jar 才会变成打过补丁的那份。
        """
        self.log.info("  准备 AMS 修复模块…")
        remote = DEV_MGK + "/services.jar"
        self.adb.sh("cp", "-f", "/system/framework/services.jar", remote,
                    timeout=self.cfg.timeout_normal, root=True)
        self.adb.sh("chmod", "644", remote, timeout=self.cfg.timeout_short, root=True)

        with tempfile.TemporaryDirectory(prefix="l5tk_jar_") as tmp:
            src = Path(tmp) / "services.jar"
            dst = Path(tmp) / "services_patched.jar"
            pr = self.adb.pull(remote, str(src))
            if not src.is_file() or src.stat().st_size < 1024:
                raise DeviceError("拉 services.jar 失败：%s" % pr.text[:150])
            self.log.info("  原版 services.jar %d KB md5=%s"
                          % (src.stat().st_size // 1024, md5_of(src)))
            dst, changed = patch_services_jar(src, dst)
            self.log.info("  %s" % ("已打补丁 0x%x" % DEX_OFF if changed
                                    else "已经是补丁后的状态，跳过"))

            self.adb.sh("mkdir", "-p", MOD_DIR + "/system/framework",
                        timeout=self.cfg.timeout_short, root=True)
            self.adb.push(str(dst), MOD_DIR + "/system/framework/services.jar")
            prop = Path(tmp) / "module.prop"
            prop.write_text(MOD_PROP, encoding="utf-8", newline="\n")
            self.adb.push(str(prop), MOD_DIR + "/module.prop")
            self.adb.sh("chmod", "-R", "755", MOD_DIR, timeout=self.cfg.timeout_short, root=True)
            chk = self.adb.sh_out("md5sum", MOD_DIR + "/system/framework/services.jar",
                                  timeout=self.cfg.timeout_normal, root=True).split()
            self.log.info("  模块已铺 %s  md5=%s" % (MOD_DIR, chk[0] if chk else "?"))
        return "AMS 修复模块已铺 %s" % MOD_ID

    # ---------------- 收尾 ----------------
    def _wait_back(self, seconds: int = 180) -> bool:
        import time
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            try:
                devs = self.adb.devices()
            except AdbError:
                devs = []
            if any(d.state == "device" for d in devs):
                return True
            time.sleep(3)
        return False


def _stamp() -> str:
    import time
    return time.strftime("%Y%m%d_%H%M%S")
