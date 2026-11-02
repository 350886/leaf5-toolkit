"""设备画像：一次把后面所有决策要用的信息全取回来。

顺序讲究：先 getprop（一次往返），再 pm/gms（各一次），最后才 su 探测
（su 在未 root 的机器上会卡到超时，放最后不拖慢前面的结果）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..core.adb import Adb
from ..core.result import ExecResult
from ..device import detect
from ..device.models import Detection
from ..logx import Logger, mask, register_secret

# 只取用得上的键，全量 prop 有 600+ 条，塞进报告没意义
KEYS = (
    "ro.product.model", "ro.product.device", "ro.product.name", "ro.product.brand",
    "ro.build.display.id", "ro.build.version.release", "ro.build.version.sdk",
    "ro.build.type", "ro.build.tags", "ro.build.fingerprint", "ro.build.date.utc",
    "ro.board.platform", "ro.hardware", "ro.soc.model", "ro.soc.manufacturer",
    "ro.boot.hardware", "ro.boot.slot_suffix", "ro.boot.verifiedbootstate",
    "ro.boot.flash.locked", "ro.boot.veritymode", "ro.debuggable",
    "ro.serialno", "ro.kernel.version",
)

STORE = "com.android.vending"
GMS = "com.google.android.gms"
GSF = "com.google.android.gsf"

# Magisk 可执行文件的位置随安装方式变。`/product/bin/magisk` 是 ONYX 这类
# 把 su 做成软链（`/product/bin/su -> ./magisk`）的系统上真正的那份，
# 只列 /system/bin 与 /debug_ramdisk 会漏。
MAGISK_PATHS = ("/product/bin/magisk", "/system/bin/magisk",
                "/debug_ramdisk/magisk", "/sbin/magisk", "magisk")


def magisk_version(adb: Adb, timeout: int = 10) -> str:
    """Magisk 版本串（形如 `31.1:MAGISK:R`），没有就返回空串。"""
    for binary in MAGISK_PATHS:
        r = adb.sh(binary, "-v", timeout=timeout)
        if r.ok and re.match(r"^\d", r.out.strip()):
            return "Magisk %s" % r.out.strip().splitlines()[0]
    return ""


def detect_root(adb: Adb, allow_adb_root: bool = True
                ) -> tuple[bool, str, str, bool]:
    """root 探测的唯一入口，返回 (su, 失败原因, magisk, adb_root 成功)。

    「检查设备」和「检查 Root」以前各写一份，路径列表还不一样，
    于是同一台机器上两个按钮能给出互相矛盾的结论。统一到这里。
    """
    su, detail = adb.su_probe()
    magisk = magisk_version(adb)
    adb_root = False
    if not su and not magisk and allow_adb_root:
        adb_root = adb.adb_root()
    return su, detail, magisk, adb_root


@dataclass
class Report:
    serial: str = ""
    detection: Detection | None = None
    props: dict[str, str] = field(default_factory=dict)
    kernel: str = ""
    su: bool = False
    su_detail: str = ""
    adb_root_ok: bool = False
    magisk: str = ""
    slot: str = ""
    locked: str = ""
    verified_boot: str = ""
    packages: dict[str, str] = field(default_factory=dict)
    store_version: str = ""
    google_accounts: list[str] = field(default_factory=list)
    foreground: str = ""
    deep: bool = False           # 是否真的探过 root/账号；没探过就不能显示「无」
    warnings: list[str] = field(default_factory=list)

    # --- 派生结论 ---
    @property
    def rooted(self) -> bool:
        return self.su or bool(self.magisk)

    @property
    def unlocked(self) -> bool:
        return self.locked.strip() == "0"

    @property
    def store_installed(self) -> bool:
        return bool(self.packages.get(STORE))

    @property
    def gms_ok(self) -> bool:
        return bool(self.packages.get(GMS)) and bool(self.packages.get(GSF))

    def summary(self) -> str:
        p = self.detection.profile if self.detection else None
        # serial 在界面上给人看是必要的，但落进日志/截图前要打码 ——
        # 这里是唯一一处直接输出 serial 的地方，统一过一遍 mask
        lines = [
            "设备        : %s  (%s)" % (p.label if p else "-", mask(self.serial)),
            "SoC         : %s" % (p.soc_label if p and p.soc_label else "-"),
            "Android     : %s (API %s)  %s" % (
                self.props.get("ro.build.version.release", "-"),
                self.props.get("ro.build.version.sdk", "-"),
                self.props.get("ro.build.type", "")),
            "固件        : %s" % self.props.get("ro.build.display.id", "-"),
            "内核        : %s" % (self.kernel or "-"),
            "槽位        : %s" % (self.slot or "单槽"),
            "引导锁      : flash.locked=%s  verifiedboot=%s" % (
                self.locked or "?", self.verified_boot or "?"),
            "Root        : %s" % self._root_line(),
            "GMS / GSF   : %s / %s" % (self.packages.get(GMS, "缺"),
                                       self.packages.get(GSF, "缺")),
            "Google Play : %s%s" % (
                self.store_version or "未安装",
                "（前台运行中）" if self.foreground == STORE else ""),
            "Google 账号 : %s" % self._accounts_line(),
        ]
        if p and p.notes and not p.verified:
            lines.append("提示        : 档案未在真机验证 —— %s" % p.notes[0])
        if self.warnings:
            lines.append("注意        : " + "；".join(self.warnings))
        return "\n".join(lines)

    def _root_line(self) -> str:
        # 没探过就说没探过。「无」是一个结论，deep=False 时我们并没有这个结论
        if not self.deep:
            return "未探测（本次只读了属性与包）"
        if self.rooted:
            return "有（%s）" % (self.magisk or "su")
        if self.adb_root_ok:
            return "可 adb root"
        # 「无」加一句为什么，否则用户没法判断是要刷机还是要授权
        return "无（%s）" % self.su_detail if self.su_detail else "无"

    def _accounts_line(self) -> str:
        if self.google_accounts:
            return ", ".join(self.google_accounts)
        return "无" if self.deep else "未探测"

    def as_dict(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "detection": self.detection.as_dict() if self.detection else None,
            "props": dict(self.props),
            "kernel": self.kernel,
            "root": {"su": self.su, "adb_root": self.adb_root_ok, "magisk": self.magisk,
                     "detail": self.su_detail},
            "slot": self.slot,
            "boot": {"flash_locked": self.locked, "verified": self.verified_boot},
            "packages": dict(self.packages),
            "store_version": self.store_version,
            "google_accounts": list(self.google_accounts),
            "foreground": self.foreground,
            "warnings": list(self.warnings),
        }


class Prober:
    def __init__(self, adb: Adb, log: Logger):
        self.adb = adb
        self.log = log

    def run(self, deep: bool = True, allow_adb_root: bool = True) -> Report:
        """采集设备画像。

        deep=False 只取属性与包（快），但不填 root / 账号 —— 那两行会显示成「无」，
        是假信息。UI 点「读取设备信息」曾经用 deep=False，于是「Root: 无」
        与旁边「检查 Root: 有」自相矛盾。现在 UI 用 deep=True 但 allow_adb_root=False：
        只读探测照做，`adb root`（会重启 adbd、断掉连接）不自动执行。
        """
        dev = self.adb.pick()
        rep = Report(serial=dev.serial, deep=deep)
        register_secret(dev.serial)

        self.log.info("读取系统属性")
        rep.props = self.adb.props(KEYS)
        rep.detection = detect(rep.props)

        # serial 以设备自报为准（adb 的 serial 可能是 IP:port 或模拟器串）
        real_serial = rep.props.get("ro.serialno", "")
        if real_serial:
            register_secret(real_serial)

        rep.kernel = self.adb.sh_out("cat", "/proc/version", timeout=15)
        rep.slot = (rep.props.get("ro.boot.slot_suffix") or "").lstrip("_")
        rep.locked = rep.props.get("ro.boot.flash.locked", "")
        rep.verified_boot = rep.props.get("ro.boot.verifiedbootstate", "")

        self.log.info("检查包")
        for pkg in (STORE, GMS, GSF):
            rep.packages[pkg] = self.adb.pm_path(pkg)
        if rep.packages.get(STORE):
            rep.store_version = self.adb.version_name(STORE)

        if deep:
            self.log.info("检查 root 能力")
            rep.su, rep.su_detail, rep.magisk, rep.adb_root_ok = detect_root(
                self.adb, allow_adb_root=allow_adb_root)

            self.log.info("检查 Google 账号")
            rep.google_accounts = self._accounts()
            rep.foreground = self.adb.foreground_pkg()

        self._sanitize(rep)
        return rep

    # ---------------- 子检查 ----------------
    def _magisk_version(self) -> str:
        return magisk_version(self.adb)

    def _accounts(self) -> list[str]:
        """dumpsys account 里的 type=com.google 账号。

        不能按空行切块再 re.search：一台机上有多个账号时，它们挤在同一段里
        （`Accounts: 2` 后面连续两行 Account），re.search 只拿第一个，
        于是「有 Google 账号」被判成「无」。必须全文 findall 再按 type 过滤。
        """
        r: ExecResult = self.adb.run("shell", "dumpsys", "account",
                                     timeout=self.adb.cfg.timeout_normal)
        # dumpsys 某些服务 dump 失败时 rc 非 0，但账号段照常输出，所以看 out 不看 ok
        if not r.out:
            return []
        found = [
            name.strip()
            for name, typ in re.findall(
                r"Account \{name=([^,]+),\s*type=([^}]+)\}", r.out)
            if "google" in typ.lower()
        ]
        return sorted(set(found))

    def _sanitize(self, rep: Report) -> None:
        """把会影响后续判断的矛盾标出来，不替用户拍板。"""
        p = rep.detection.profile if rep.detection else None
        if rep.detection and rep.detection.unknown:
            rep.warnings.append("机型未识别，写分区的操作应停手")
        if p and p.build_type and rep.props.get("ro.build.type") and \
                rep.props["ro.build.type"] != p.build_type:
            rep.warnings.append("构建类型是 %s，档案写的是 %s"
                                % (rep.props["ro.build.type"], p.build_type))
        if p and p.android_major and rep.props.get("ro.build.version.release") and \
                rep.props["ro.build.version.release"].split(".")[0] != str(p.android_major):
            rep.warnings.append("Android 版本与档案不符")
        if rep.store_installed and not rep.gms_ok:
            rep.warnings.append("商店已装但 GMS/GSF 不全")
        if rep.locked == "1":
            rep.warnings.append("bootloader 处于锁定状态，fastboot flash 会被拒绝")
        # 首次 su 请求会触发 Magisk 授权框，没人点就超时 —— 这不是「没 root」，
        # 直接报到「注意」里，免得用户按一次就以为机器没 root
        if rep.su_detail and ("超时" in rep.su_detail or "拒绝" in rep.su_detail):
            rep.warnings.append("su 未通过：%s；在手机上点「允许」后重新检测即可"
                                % rep.su_detail)
