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


@dataclass
class Report:
    serial: str = ""
    detection: Detection | None = None
    props: dict[str, str] = field(default_factory=dict)
    kernel: str = ""
    su: bool = False
    adb_root_ok: bool = False
    magisk: str = ""
    slot: str = ""
    locked: str = ""
    verified_boot: str = ""
    packages: dict[str, str] = field(default_factory=dict)
    store_version: str = ""
    google_accounts: list[str] = field(default_factory=list)
    foreground: str = ""
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
            "Root        : %s" % ("有（%s）" % (self.magisk or "su") if self.rooted
                                  else ("可 adb root" if self.adb_root_ok else "无")),
            "GMS / GSF   : %s / %s" % (self.packages.get(GMS, "缺"),
                                       self.packages.get(GSF, "缺")),
            "Google Play : %s%s" % (
                self.store_version or "未安装",
                "（前台运行中）" if self.foreground == STORE else ""),
            "Google 账号 : %s" % (", ".join(self.google_accounts) or "无"),
        ]
        if p and p.notes and not p.verified:
            lines.append("提示        : 档案未在真机验证 —— %s" % p.notes[0])
        if self.warnings:
            lines.append("注意        : " + "；".join(self.warnings))
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "detection": self.detection.as_dict() if self.detection else None,
            "props": dict(self.props),
            "kernel": self.kernel,
            "root": {"su": self.su, "adb_root": self.adb_root_ok, "magisk": self.magisk},
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

    def run(self, deep: bool = True) -> Report:
        dev = self.adb.pick()
        rep = Report(serial=dev.serial)
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
            rep.su = self.adb.su_available()
            rep.magisk = self._magisk_version()
            if not rep.su:
                rep.adb_root_ok = self.adb.adb_root()

            self.log.info("检查 Google 账号")
            rep.google_accounts = self._accounts()
            rep.foreground = self.adb.foreground_pkg()

        self._sanitize(rep)
        return rep

    # ---------------- 子检查 ----------------
    def _magisk_version(self) -> str:
        """Magisk 有的版本把 magisk 放在 /debug_ramdisk，三个路径都试。"""
        for binary in ("/system/bin/magisk", "/debug_ramdisk/magisk",
                       "/sbin/magisk"):
            r = self.adb.sh(binary, "-v", timeout=10)
            if r.ok and re.match(r"^\d", r.out.strip()):
                return "Magisk %s" % r.out.strip().splitlines()[0]
        return ""

    def _accounts(self) -> list[str]:
        """dumpsys account 里的 type=com.google 账号。取不到就是取不到。"""
        r: ExecResult = self.adb.run("shell", "dumpsys", "account",
                                     timeout=self.adb.cfg.timeout_normal)
        if not r.ok:
            return []
        found: list[str] = []
        for block in re.split(r"\n\s*\n", r.out):
            if "com.google" not in block:
                continue
            m = re.search(r"Account \{name=([^,]+),\s*type=([^}]+)\}", block)
            if m and "google" in m.group(2).lower():
                found.append(m.group(1).strip())
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
