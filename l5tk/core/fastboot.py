"""fastboot 封装。

只做 V2 真正需要的四件事：列设备、读变量、刷分区、重启。
刻意不封装 `fastboot oem` 这类厂商私货 —— 每家的参数不一样，写进来就是负债。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from typing import Sequence

from ..config import Config
from .adb import CREATE_NO_WINDOW, Runner, _default_runner
from .errors import FastbootError
from .result import ExecResult


class Fastboot:
    def __init__(self, cfg: Config, serial: str = "", runner: Runner | None = None):
        self.cfg = cfg
        self._serial = serial or cfg.serial
        self._run = runner or _default_runner

    def available(self) -> bool:
        exe = self.cfg.fastboot
        return bool(shutil.which(exe) or __import__("os").path.isfile(exe))

    def argv(self, *args: str) -> list[str]:
        base = [self.cfg.fastboot]
        if self._serial:
            base += ["-s", self._serial]
        return base + [str(a) for a in args]

    def run(self, *args: str, timeout: int | None = None) -> ExecResult:
        if not self.available():
            raise FastbootError(
                "找不到 fastboot：%s" % self.cfg.fastboot,
                hint="把 fastboot.exe 放进 assets/bin/，或用 L5TK_FASTBOOT 指定")
        return self._run(self.argv(*args), timeout or self.cfg.timeout_normal)

    def devices(self) -> list[str]:
        r = self.run("devices", timeout=self.cfg.timeout_short)
        if not r.ok:
            raise FastbootError("fastboot devices 失败", hint=r.text[:300])
        out = []
        for line in r.out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].startswith("fastboot"):
                out.append(parts[0])
        return out

    def pick(self) -> str:
        devs = self.devices()
        if self._serial:
            if self._serial in devs:
                return self._serial
            raise FastbootError("fastboot 下没有设备 %s" % self._serial,
                                hint="在线：%s" % (", ".join(devs) or "无"))
        if not devs:
            raise FastbootError(
                "fastboot 没有设备",
                hint="音量上/下按住再插 USB，或用 adb reboot bootloader 进")
        if len(devs) > 1:
            raise FastbootError("fastboot 下有多台设备：%s" % ", ".join(devs),
                                hint="用 --serial 指定一台")
        self._serial = devs[0]
        return devs[0]

    def getvar(self, name: str) -> str:
        """fastboot getvar 把结果写 stderr，且以 (bootloader) 前缀开头。"""
        r = self.run("getvar", name, timeout=self.cfg.timeout_short)
        m = re.search(r"%s:\s*(\S+)" % re.escape(name), r.text)
        return m.group(1) if m else ""

    def slots(self) -> list[str]:
        """返回可用槽位（A/B 设备为 ['a','b']，单槽为空列表）。"""
        raw = self.getvar("slot-count")
        try:
            n = int(raw)
        except ValueError:
            n = 1
        return [chr(ord("a") + i) for i in range(max(1, min(n, 2)))]

    def flash(self, partition: str, image_path: str,
              timeout: int | None = None) -> ExecResult:
        if self.cfg.dry_run:
            from .errors import Refused
            raise Refused("dry-run：跳过 fastboot flash %s" % partition)
        return self.run("flash", partition, str(image_path),
                        timeout=timeout or self.cfg.timeout_flash)

    def reboot(self, target: str = "") -> ExecResult:
        if self.cfg.dry_run:
            from .errors import Refused
            raise Refused("dry-run：跳过 fastboot reboot %s" % target)
        return self.run("reboot", *([target] if target else []),
                        timeout=self.cfg.timeout_short)

    def wait_for_adb(self, adb_serial: str = "", seconds: int = 180,
                     poll: float = 2.0) -> bool:
        """刷完重启后等设备回到 adb。给 UI 用，别让用户以为卡死。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                r = self._run([self.cfg.adb, "devices"], self.cfg.timeout_short)
                if r.ok and re.search(r"\tdevice\b", r.out):
                    if not adb_serial or adb_serial in r.out:
                        return True
            except Exception:
                pass
            time.sleep(max(0.05, poll))
        return False

    def __repr__(self) -> str:
        return "<Fastboot %s>" % (self._serial or "(auto)")


def list_boot_images(folder) -> list:
    """从目录里挑候选 boot 镜像，按修改时间倒序。

    命名不强求：patched/ 前缀、magisk_patched 都收，只要扩展名像镜像。
    """
    from pathlib import Path
    folder = Path(folder)
    if not folder.is_dir():
        return []
    exts = {".img", ".bin"}
    items = [p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() in exts and p.stat().st_size > 1024 * 1024]
    return sorted(items, key=lambda p: p.stat().st_mtime, reverse=True)
