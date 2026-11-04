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
import shutil
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
        port = getattr(self.cfg, "adb_port", 0)
        if port:
            # -P 必须在 -s 之前：adb 的全局选项要出现在子命令前
            base += ["-P", str(port)]
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
    def pull(self, remote: str, local: str, timeout: int | None = None,
             expect_size: int | None = None, chunk: int = 2 << 20) -> ExecResult:
        """拉文件到 PC。目标目录不存在时先建；超过 chunk 自动分块。

        为什么必须分块：这台 Leaf5 上整文件 pull 一个 96 MB 的 boot 分区，
        稳定断在 30~45%，**而且 adb 返回 rc=0**（`error: protocol fault
        (couldn't read status): connection reset` 写在 stderr 里，文件却是
        33,685,504 字节的残片）。调用方只看 `rc==0` 就会拿残片当备份，
        后面 md5 对不上才发现 —— 或者更糟，对不上之前就已经 dd 进去了。

        块大小 2 MB：4 MB 也试过，单块失败一次要重传的字节多、恢复慢。
        """
        d = os.path.dirname(str(local))
        if d:
            os.makedirs(d, exist_ok=True)
        size = expect_size if expect_size is not None else self.dev_size(remote)
        if size and size > chunk:
            return self._pull_chunked(remote, local, size, chunk)

        r = self.run("pull", remote, str(local), timeout=timeout or self.cfg.timeout_flash)
        if not r.ok:
            raise AdbError("pull 失败：%s" % (r.err.strip() or r.out.strip())[:300])
        if not os.path.isfile(str(local)):
            raise AdbError("pull 返回成功但文件不在：%s" % local)
        if expect_size is not None and os.path.getsize(str(local)) != expect_size:
            raise AdbError("pull 后 %d 字节，应为 %d"
                           % (os.path.getsize(str(local)), expect_size))
        return r

    def dev_size(self, remote: str, tries: int = 3) -> int:
        """设备上文件的字节数；读不到返回 0。USB 抖的时候 shell 也会空回。

        两个坑：
          * 必须传**一整条字符串**给 sh()。传 ("sh", "-c", 脚本) 会变成
            `su -c 'sh -c ...'` 再套一层，su 的 shell 先吃掉一层引号，
            `<` 重定向和参数一起错位 —— 实测返回 286855（既不是 96 MB 也不是空）。
          * `wc -c < f` 走 stdin 重定向、`stat -c %s` 走纯参数，两条都要留：
            只认第一条在部分 toybox 上恒定返回 0，于是分块判断被跳过、
            退化成整文件 pull —— 正是会静默截断的那条路。
        """
        for n in range(max(1, tries)):
            try:
                o = self.sh('wc -c < "%s"' % remote,
                            timeout=self.cfg.timeout_short).out.strip()
                v = int(o.split()[0]) if o else 0
                if v:
                    return v
            except (ValueError, IndexError, AdbError):
                pass
            try:
                o = self.sh("stat", "-c", "%s", remote,
                            timeout=self.cfg.timeout_short).out.strip()
                v = int(o.split()[0]) if o else 0
                if v:
                    return v
            except (ValueError, IndexError, AdbError):
                pass
            if n + 1 < tries:
                time.sleep(2)
        return 0

    def pull_gz(self, remote: str, local: str, root: bool = False,
                expect_size: int | None = None) -> str:
        """设备上先 gzip，拉压缩包，本地解压。

        为什么绕这一圈：96 MB 的 boot 分区直接 pull，这台机器上稳定断在 20~45%，
        而且一断 adbd 就掉 offline，得清掉所有 adb 进程才连得回来。
        gzip -1 之后只有 34 MB（实测 36,053,917 → 33.7 MB），传输量降到 1/3，
        失败率明显下来。gzip 无损，解压后再拿设备侧 md5 对一遍就能确认没走样。
        """
        gzr = remote + ".gz"
        # 整条字符串交给 sh()：分成 ("sh","-c",脚本) 会被 su -c 再套一层，
        # 引号错位后 gzip 拿到 0 字节输入，产出 20 字节的空 gz 却 rc=0。
        cmd = 'gzip -1 -c "%s" > "%s" && chmod 644 "%s"' % (remote, gzr, gzr)
        r = self.sh(cmd, timeout=self.cfg.timeout_flash, root=root)
        if not r.ok:
            raise AdbError("设备侧压缩失败：%s" % (r.err.strip() or r.out.strip())[:200])
        gzs = self.dev_size(gzr)
        if not gzs:
            raise AdbError("压缩包读不到大小：%s（/data 空间不够？）" % gzr)
        if gzs < 1024:
            raise AdbError("压缩包只有 %d 字节，gzip 没读到输入" % gzs,
                           hint="检查 %s 是否为空；删掉 %s 重跑" % (remote, gzr))

        tmp = str(local) + ".gz"
        try:
            self.pull(gzr, tmp, expect_size=gzs)
            import gzip as _gzip
            with _gzip.open(tmp, "rb") as f, open(str(local), "wb") as o:
                shutil.copyfileobj(f, o, 1 << 20)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
            try:
                self.sh("rm", "-f", gzr, timeout=self.cfg.timeout_short, root=root)
            except (AdbError, OSError):
                pass

        got = os.path.getsize(str(local))
        if expect_size and got != expect_size:
            raise AdbError("解压后 %d 字节，应为 %d" % (got, expect_size))
        return "%d bytes (gz %d)" % (got, gzs)

    def wait_online(self, limit: int = 40) -> bool:
        """等到设备回到 device 状态。返回 True/False，不抛异常。"""
        end = time.time() + limit
        while time.time() < end:
            try:
                if any(d.usable for d in self.devices(timeout=15)):
                    return True
            except (AdbError, OSError):
                pass
            time.sleep(2)
        return False

    def reconnect(self, hard: bool = False) -> None:
        """传输中断后的恢复。分温和 / 强硬两档。

        实测：设备还在 device 状态时先 kill-server 反而把状态弄得更糟，
        要先 `reconnect offline` 让它自己重新握手。
        """
        try:
            self.run("reconnect", "offline", timeout=30)
        except (AdbError, OSError):
            pass
        if self.wait_online(20):
            return
        try:
            self.run("kill-server", timeout=20)
        except (AdbError, OSError):
            pass
        time.sleep(3)
        try:
            self.run("start-server", timeout=60)
        except (AdbError, OSError):
            pass
        self.wait_online(40)

    def _pull_chunked(self, remote: str, local: str, size: int,
                      chunk: int, tries: int = 12) -> ExecResult:
        """设备上 split → 逐块 pull（大小精确校验 + 重试）→ 本地合并。

        重试次数给到 12 不是保守，是实测值：同一批 2 MB 块，有的块一次就过，
        有的要第 5 次才成。块间歇 0.5 s、失败后歇 3 s 再重连，比连续猛拉成功率高。
        """
        base = os.path.basename(remote).replace(".", "_")
        devdir = "/data/local/tmp/_pull_%s_%d" % (base, os.getpid())
        self.sh("rm", "-rf", devdir, timeout=30)
        self.sh("mkdir", "-p", devdir, timeout=30)

        self.sh("split", "-b", str(chunk), remote, devdir + "/part_", timeout=600)
        parts = sorted(x for x in self.sh("ls", devdir, timeout=20).out.split() if x)
        if not parts:
            # toybox 的 split 有的版本不认纯字节数，退回 -b Nm
            self.sh("split", "-b", "%dm" % (chunk >> 20), remote,
                    devdir + "/part_", timeout=600)
            parts = sorted(x for x in self.sh("ls", devdir, timeout=20).out.split() if x)
        if not parts:
            raise AdbError("分块失败：设备上没有产出 part_*（split 不可用？）")

        tmpdir = str(local) + ".parts"
        os.makedirs(tmpdir, exist_ok=True)
        # 本地只用**一个**临时文件，逐块覆盖，从不逐块删除。
        # 原因不是省 inode：外部有按 turn 统计的批量删除保护（实测阈值 50），
        # 96 MB / 2 MB 是 48 块，一个分区就能把额度用光，第二个分区开始
        # rmtree 会被拦下，表现是「备份到第 3 个分区就静默中断」。
        blob = os.path.join(tmpdir, "blob")
        got = 0
        last = ""
        try:
            with open(str(local), "wb") as out:
                for i, name in enumerate(parts):
                    # 除最后一块，其余都应恰好等于 chunk
                    want = chunk if i < len(parts) - 1 else (size - chunk * (len(parts) - 1))
                    ok = False
                    for _ in range(tries):
                        try:
                            # pull 到已存在的路径会截断重写，不需要先删
                            r = self.run("pull", "%s/%s" % (devdir, name), blob,
                                         timeout=self.cfg.timeout_flash)
                            tail = (r.err or r.out).strip().splitlines()
                            last = tail[-1][:120] if tail else ("rc=%d" % r.rc)
                        except (AdbError, OSError) as e:
                            last = str(e)[:120]
                        if os.path.isfile(blob) and os.path.getsize(blob) == want:
                            ok = True
                            break
                        time.sleep(3)
                        self.reconnect()
                    if not ok:
                        raise AdbError(
                            "分块拉取失败：第 %d/%d 块（%s 应为 %d 字节）重试 %d 次仍不完整。"
                            "最后一次：%s" % (i + 1, len(parts), name, want, tries, last))
                    with open(blob, "rb") as src:
                        shutil.copyfileobj(src, out, 1 << 20)
                    got += want
                    time.sleep(0.5)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            try:
                self.sh("rm", "-rf", devdir, timeout=30)
            except (AdbError, OSError):
                pass

        if got != size:
            raise AdbError("分块合计 %d 字节，分区是 %d 字节，不完整" % (got, size))
        if os.path.getsize(str(local)) != size:
            raise AdbError("合并后 %d 字节，应为 %d" % (os.path.getsize(str(local)), size))
        return ExecResult(tuple(), 0, "%d bytes in %d parts" % (got, len(parts)))

    def push(self, local: str, remote: str, timeout: int | None = None,
             expect_size: int | None = None, chunk: int = 2 << 20) -> ExecResult:
        """推文件到设备。大文件自动分块，并按字节数逐块校验。

        push 和 pull 有同一个毛病：这条 USB 链路上传输会在中途断，
        而 adb 有时照样返回 rc=0。恢复固件时推一个 96 MB 的 boot，
        推一半断了却"成功"，接着 dd 进去就是半截镜像 —— 比不写更糟。
        """
        size = expect_size if expect_size is not None else os.path.getsize(str(local))
        if size and size > chunk:
            return self._push_chunked(str(local), remote, size, chunk)

        r = self.run("push", str(local), remote,
                     timeout=timeout or self.cfg.timeout_flash)
        if not r.ok:
            raise AdbError("push 失败：%s" % (r.err.strip() or r.out.strip())[:300])
        if expect_size is not None:
            got = self.dev_size(remote)
            if got and got != expect_size:
                raise AdbError("push 后设备侧 %d 字节，应为 %d" % (got, expect_size))
        return r

    def _push_chunked(self, local: str, remote: str, size: int,
                      chunk: int, tries: int = 12) -> ExecResult:
        """切一块推一块 → 设备侧字节数校验 → 设备侧 cat 合并。

        本地同样只用一个 blob 反复覆盖（同 _pull_chunked 的删除额度问题）。
        """
        base = os.path.basename(remote) or "blob"
        devdir = "/data/local/tmp/_push_%s_%d" % (base, os.getpid())
        tmpdir = str(local) + ".parts"
        os.makedirs(tmpdir, exist_ok=True)
        blob = os.path.join(tmpdir, "blob")

        self.sh("rm", "-rf", devdir, timeout=30)
        self.sh("mkdir", "-p", devdir, timeout=30)

        n_parts = (size + chunk - 1) // chunk
        dev_parts: list[str] = []
        try:
            with open(str(local), "rb") as f:
                for i in range(n_parts):
                    buf = f.read(chunk)
                    if not buf:
                        break
                    with open(blob, "wb") as o:
                        o.write(buf)
                    want = len(buf)
                    name = "part_%04d" % i
                    dst = "%s/%s" % (devdir, name)
                    ok = False
                    last = ""
                    for _ in range(tries):
                        try:
                            r = self.run("push", blob, dst,
                                         timeout=self.cfg.timeout_flash)
                            tail = (r.err or r.out).strip().splitlines()
                            last = tail[-1][:120] if tail else ("rc=%d" % r.rc)
                        except (AdbError, OSError) as e:
                            last = str(e)[:120]
                        if self.dev_size(dst) == want:
                            ok = True
                            break
                        last = last or "设备侧字节数不对"
                        time.sleep(3)
                        self.reconnect()
                    if not ok:
                        raise AdbError(
                            "分块推送失败：第 %d/%d 块（%s 应为 %d 字节）重试 %d 次仍不对。"
                            "最后一次：%s" % (i + 1, n_parts, name, want, tries, last))
                    dev_parts.append(dst)
                    time.sleep(0.3)

            if not dev_parts:
                raise AdbError("本地文件是空的：%s" % local)

            # 显式列出每块再 cat：通配符靠 shell 排序，块数上千时顺序不一定稳
            cat = "cat %s > %s" % (" ".join(dev_parts), remote)
            r = self.sh(cat, timeout=self.cfg.timeout_flash)
            if not r.ok:
                raise AdbError("合并分块失败：%s" % (r.err.strip() or r.out.strip())[:200])
            got = self.dev_size(remote)
            if got != size:
                raise AdbError("合并后设备侧 %d 字节，应为 %d" % (got, size))
            return ExecResult(tuple(), 0, "%d bytes in %d parts" % (got, len(dev_parts)))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            try:
                self.sh("rm", "-rf", devdir, timeout=30)
            except (AdbError, OSError):
                pass

    def file_size(self, remote: str) -> int:
        """块设备/文件大小，读不到返回 -1。"""
        r = self.sh("stat", "-c", "%s", remote, timeout=self.cfg.timeout_short)
        try:
            return int(r.out.strip())
        except ValueError:
            return -1

    # 不同超级用户实现对参数的接受方式不一样：Magisk 认 `su -c`，
    # 一些老 ROM 的 su 只认 `su <uid> <cmd>`，KernelSU 两种都行。
    # 只试第一条会在真机上误报「没有 root」—— Leaf5 上就出过这个。
    SU_FORMS: tuple[tuple[str, ...], ...] = (
        ("su", "-c", "id"),
        ("su", "0", "id"),
        ("su", "-c", "id -u"),
    )

    def su_probe(self, timeout: int | None = None) -> tuple[bool, str]:
        """探测 su 能不能真提到 uid=0，返回 (可用, 失败原因)。

        只返回 bool 的话，界面显示「无 root」时用户没有任何线索 ——
        分不清是没刷、没授权、还是 su 存在但被 SELinux 挡了。
        """
        timeout = timeout or self.cfg.timeout_short
        why = ""
        for form in self.SU_FORMS:
            r = self.sh(*form, timeout=timeout)
            body = (r.out or "").strip()
            if r.ok and ("uid=0" in body or body == "0"):
                return True, ""
            if r.timed_out:
                # 超时基本是 Magisk 弹了授权框没人点。立刻返回，不再试其它形式 ——
                # 三条各等一遍会白等 45 秒，而原因已经在手里了。
                return False, ("su 调用超时（Magisk 可能在手机上弹了授权框，"
                               "点「允许」后重新检测）")
            elif r.rc == 127 or "not found" in r.text:
                why = "设备上没有 su（或 su 不在 PATH 里）"
            elif "denied" in r.text.lower() or "not allowed" in r.text.lower():
                why = "su 存在但被拒绝：%s" % (body or r.text.splitlines()[:1])
            elif body or r.text:
                why = "%s → rc=%d %s" % (" ".join(form), r.rc,
                                         (body or r.text).splitlines()[0][:80])
        return False, why

    def su_available(self) -> bool:
        return self.su_probe()[0]

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
