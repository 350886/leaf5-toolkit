"""adb 封装。

设计取舍：
  * 只用 subprocess 调 adb，不自己实现 ADB 协议 —— V1 里为了「不依赖平台工具」
    把 adb.exe 一起打包就行，没必要抄协议。
  * 每条命令都带超时。Windows 上 USB 重新枚举时 adb 会挂在 server 启动阶段，
    没有超时就是界面永久卡死。
  * 输出解码单独一层：国产 ROM 的 getprop 会吐 GBK 中文，直接 utf-8 会变问号。
  * runner 可注入，测试用假 adb（tests/fake_adb.py）跑真逻辑，不打桩到函数级。
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from ..config import Config
from .errors import AdbError
from .result import ExecResult

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

Runner = Callable[[Sequence[str], int], ExecResult]


@dataclass(frozen=True)
class Device:
    serial: str
    state: str          # device / offline / unauthorized / bootloader ...
    model: str = ""
    product: str = ""

    @property
    def usable(self) -> bool:
        return self.state == "device"


def _decode(raw: bytes) -> str:
    """优先 utf-8；含替换符且能按 gbk 解出更少替换符时用 gbk。"""
    if not raw:
        return ""
    try:
        s = raw.decode("utf-8")
        return s
    except UnicodeDecodeError:
        pass
    for enc in ("gbk", "cp936"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _default_runner(argv: Sequence[str], timeout: int) -> ExecResult:
    t0 = time.monotonic()
    try:
        p = subprocess.run(list(argv), capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
        ms = int((time.monotonic() - t0) * 1000)
        return ExecResult(tuple(argv), p.returncode,
                          _decode(p.stdout or b""), _decode(p.stderr or b""), ms)
    except subprocess.TimeoutExpired as e:
        ms = int((time.monotonic() - t0) * 1000)
        return ExecResult(tuple(argv), -1, _decode(e.stdout or b""),
                          _decode(e.stderr or b""), ms, timed_out=True)
    except FileNotFoundError:
        raise AdbError("找不到 adb：%s" % argv[0],
                       hint="把 adb.exe 放到 assets/bin/，或用 L5TK_ADB 指定完整路径")
    except OSError as e:
        raise AdbError("adb 启动失败：%s" % e,
                       hint="检查是否被杀软拦了子进程")


class Adb:
    """一台设备的操作句柄。serial 为空时用「唯一在线设备」。"""

    def __init__(self, cfg: Config, serial: str = "",
                 runner: Runner | None = None):
        self.cfg = cfg
        self._serial = serial or cfg.serial
        self._run = runner or _default_runner

    # ---------------- 底层 ----------------
    def argv(self, *args: str) -> list[str]:
        base = [self.cfg.adb]
        if self._serial:
            base += ["-s", self._serial]
        return base + [str(a) for a in args]

    def run(self, *args: str, timeout: int | None = None) -> ExecResult:
        return self._run(self.argv(*args), timeout or self.cfg.timeout_normal)

    # ---------------- 设备枚举 ----------------
    def devices(self, timeout: int | None = None) -> list[Device]:
        r = self._run([self.cfg.adb, "devices", "-l"],
                      timeout or self.cfg.timeout_short)
        if not r.ok:
            raise AdbError("adb devices 执行失败", hint=r.err.strip()[:300])
        out: list[Device] = []
        for line in r.out.splitlines()[1:]:
            line = line.strip()
            if not line or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            kv = {}
            for tok in parts[2:]:
                if ":" in tok:
                    k, v = tok.split(":", 1)
                    kv[k] = v
            out.append(Device(serial, state, kv.get("model", ""), kv.get("product", "")))
        return out

    def pick(self) -> Device:
        """挑设备。指定了 serial 就必须在线；否则要求恰好一台 device。"""
        devs = self.devices()
        if self._serial:
            for d in devs:
                if d.serial == self._serial:
                    if not d.usable:
                        raise AdbError(
                            "设备 %s 状态是 %s，不是 device" % (d.serial, d.state),
                            hint=state_hint(d.state))
                    return d
            raise AdbError("指定的设备 %s 不在线" % self._serial,
                           hint="在线的是：%s" % (", ".join(d.serial for d in devs) or "无"))
        usable = [d for d in devs if d.usable]
        if not usable:
            # 只有一台但状态不对时，提示必须指名道姓 —— 这句话是用户唯一的线索
            if len(devs) == 1:
                d = devs[0]
                raise AdbError("设备 %s 状态是 %s，不能执行命令" % (d.serial, d.state),
                               hint=state_hint(d.state))
            states = ", ".join("%s(%s)" % (d.serial, d.state) for d in devs) or "没插设备"
            raise AdbError("没有可用的设备", hint="当前状态：%s" % states)
        if len(usable) > 1:
            raise AdbError("连了 %d 台设备" % len(usable),
                           hint="先只插一台，或用 --serial 指定：%s"
                                % ", ".join(d.serial for d in usable))
        self._serial = usable[0].serial
        return usable[0]

    # ---------------- shell ----------------
    def sh(self, *cmd: str, timeout: int | None = None, root: bool = False) -> ExecResult:
        """在设备上跑一条命令。root=True 时套 su -c（需要设备已 root）。"""
        if root:
            joined = " ".join(_quote(c) for c in cmd)
            return self.run("shell", "su", "-c", joined, timeout=timeout)
        return self.run("shell", *cmd, timeout=timeout)

    def sh_out(self, *cmd: str, timeout: int | None = None, root: bool = False) -> str:
        return self.sh(*cmd, timeout=timeout, root=root).out.strip()

    def getprop(self, key: str) -> str:
        return self.run("shell", "getprop", key, timeout=self.cfg.timeout_short).out.strip()

    def props(self, keys: Iterable[str] | None = None) -> dict[str, str]:
        """一次 getprop 拿全量，比逐个键快 20 倍（每个键一次 adb 往返）。"""
        r = self.run("shell", "getprop", timeout=self.cfg.timeout_normal)
        if not r.ok:
            raise AdbError("getprop 读取失败", hint=r.err.strip()[:300])
        all_props: dict[str, str] = {}
        for line in r.out.splitlines():
            m = re.match(r"^\[([^\]]+)\]:\s*\[(.*)\]\s*$", line.strip())
            if m:
                all_props[m.group(1)] = m.group(2)
        if keys is None:
            return all_props
        return {k: all_props.get(k, "") for k in keys}

    # ---------------- 包管理 ----------------
    def pm_path(self, pkg: str) -> str:
        """返回安装路径，没装返回空串。pm path 非零退出也正常（包不存在）。"""
        r = self.run("shell", "pm", "path", pkg, timeout=self.cfg.timeout_short)
        for line in r.out.splitlines():
            if line.startswith("package:"):
                return line.split(":", 1)[1].strip()
        return ""

    def installed(self, pkg: str) -> bool:
        return bool(self.pm_path(pkg))

    def version_name(self, pkg: str) -> str:
        r = self.run("shell", "dumpsys", "package", pkg, timeout=self.cfg.timeout_normal)
        m = re.search(r"versionName=(\S+)", r.out)
        return m.group(1) if m else ""

    def install(self, apk_path: str, allow_downgrade: bool | None = None,
                reinstall: bool = True) -> ExecResult:
        """adb install。默认 -r -d：-d 允许降级，否则 44.x 装不回 42.x。"""
        args = ["install"]
        if reinstall:
            args.append("-r")
        if allow_downgrade if allow_downgrade is not None else self.cfg.allow_downgrade:
            args.append("-d")
        args.append(str(apk_path))
        r = self.run(*args, timeout=self.cfg.timeout_flash)
        return r

    # ---------------- 活动 ----------------
    def start(self, component: str, wait: bool = True) -> ExecResult:
        args = ["shell", "am", "start"]
        if wait:
            args.append("-W")
        args.append(component)
        return self.run(*args, timeout=self.cfg.timeout_normal)

    def top_activity(self) -> str:
        """前台 Activity 全名，拿不到返回空串。

        三种 dump 位置都试：新系统在 topResumedActivity，老系统在 mResumedActivity，
        Android 11 起 dumpsys activity activities 有 ResumedActivity。
        """
        r = self.run("shell", "dumpsys", "activity", "activities",
                     timeout=self.cfg.timeout_normal)
        if not r.ok:
            return ""
        for pat in (r"topResumedActivity=.*?\s([\w.]+/[\w.$]+)",
                    r"mResumedActivity:.*?\s([\w.]+/[\w.$]+)",
                    r"ResumedActivity:.*?\s([\w.]+/[\w.$]+)"):
            m = re.search(pat, r.out)
            if m:
                return m.group(1)
        return ""

    def foreground_pkg(self) -> str:
        act = self.top_activity()
        return act.split("/", 1)[0] if act else ""

    def current_user(self) -> str:
        r = self.run("shell", "am", "get-current-user", timeout=self.cfg.timeout_short)
        return r.out.strip() or "0"

    # ---------------- 文件 ----------------
    def pull(self, remote: str, local: str,
             timeout: int | None = None) -> ExecResult:
        return self.run("pull", remote, str(local),
                        timeout=timeout or self.cfg.timeout_flash)

    def push(self, local: str, remote: str,
             timeout: int | None = None) -> ExecResult:
        return self.run("push", str(local), remote,
                        timeout=timeout or self.cfg.timeout_flash)

    def file_size(self, remote: str) -> int:
        """块设备/文件大小，读不到返回 -1。"""
        r = self.sh("stat", "-c", "%s", remote, timeout=self.cfg.timeout_short)
        try:
            return int(r.out.strip())
        except ValueError:
            return -1

    def su_available(self) -> bool:
        r = self.sh("su", "-c", "id", timeout=self.cfg.timeout_short)
        return r.ok and "uid=0" in r.out

    def adb_root(self) -> bool:
        """userdebug 上 adb root 能直接提权；user 版会返回 cannot run as root。"""
        r = self.run("root", timeout=self.cfg.timeout_short)
        if "cannot run as root" in r.text or "adbd cannot" in r.text:
            return False
        if not r.ok:
            return False
        # adb root 会重启 adbd，等它回来
        for _ in range(20):
            time.sleep(0.3)
            d = self.run("get-state", timeout=self.cfg.timeout_short)
            if d.out.strip() == "device":
                return True
        return False

    def __repr__(self) -> str:
        return "<Adb %s>" % (self._serial or "(auto)")


def state_hint(state: str) -> str:
    """把 adb 的状态字符串翻成一句能照做的话。"""
    table = {
        "unauthorized": "手机屏幕上弹了「允许 USB 调试吗」，点允许；没弹就拔插一次 USB",
        "offline": "adb 进程与设备失联，先 adb kill-server，再拔插 USB",
        "recovery": "设备在 recovery 模式，重启到系统",
        "sideload": "设备在 sideload 模式，重启到系统",
        "bootloader": "设备在 bootloader，先 fastboot reboot",
        "no permissions": "Linux 下是 udev 规则问题；Windows 下装 OEM USB 驱动",
    }
    return table.get(state.strip().lower(), "把设备切到正常系统、解锁屏幕后再试")


def _quote(s: str) -> str:
    """给 su -c 拼命令用。含空格/特殊字符就单引号包起来并转义内部单引号。"""
    s = str(s)
    if not s:
        return "''"
    if re.fullmatch(r"[\w@%+=:,./\-]+", s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def shell_quote(s: str) -> str:
    """对外暴露：需要在远端 shell 里拼复杂命令时用。"""
    return _quote(s)


def split_args(text: str) -> list[str]:
    """把用户输入的参数串按 shell 规则拆开（Windows 上 shlex 的 posix 模式够用）。"""
    return shlex.split(text, posix=(os.name != "nt"))
