"""tkinter 界面。

三条硬规矩（都是 V1 现场踩出来的）：
  1. 任何 adb 调用都不许在主线程 —— 一次 USB 卡顿就是「未响应」白窗口
  2. 后台线程不碰控件，只往 queue 里放；主线程 after() 泵出来
  3. 按钮忙碌时整组禁用，否则用户连点会同时跑两个刷机流程

界面只在装了 tkinter 时可用；打包成 EXE 走 PyInstaller 自带。
"""

from __future__ import annotations

import queue
import threading
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .. import __version__
from ..config import load as load_cfg
from ..core.adb import Adb
from ..core.errors import L5Error
from ..core.fastboot import Fastboot
from ..flags import gp_ui_enabled
from ..logx import Logger, mask
from ..ops.gplay import GPlay
from ..ops.probe import Prober
from ..ops.root import RootOps

# 深色配浅色字，和 V1 一致，避免用户两台工具切来切去不适应
BG = "#1e1f22"
PANEL = "#2b2d30"
FG = "#d7d9dd"
MUTED = "#8b8f96"
# 比 MUTED 再暗一档，专门给「用在什么情况」那类条件说明用：
# 两行说明如果同样亮度，会糊成一整段，用户分不出哪句是使用条件。
DIM = "#7a7f86"
ACCENT = "#4c9aff"
OK = "#4ec9a0"
WARN = "#e2b341"
ERR = "#ef6b6b"

W = 900
# 每个按钮多了「用在」那一行，左栏比原来高约 8×14px；窗口跟着长高，
# 否则日志区被挤成一条缝 —— 日志是本工具唯一的事后证据。
# 又多了一个「EDL 刷 boot」入口（fastboot 在 ONYX 上被裁，得留替代通道）。
H = 858 if gp_ui_enabled() else 830


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("l5tk %s — 多机型 Android 工具箱" % __version__)
        self.geometry("%dx%d" % (W, H))
        self.minsize(780, 600)
        self.configure(bg=BG)

        self.cfg = load_cfg()
        self.cfg.ensure_dirs()
        self.log = Logger(self.cfg.log_dir, echo=False)
        self.adb = Adb(self.cfg)
        self.fb = Fastboot(self.cfg)
        self.report = None

        self._q: queue.Queue[tuple[str, str]] = queue.Queue()
        self._busy = False

        # 一键 root 里 magisk dd 是固定的一步，所以模块默认跟着装 ——
        # 等于 V1「勾了 Magisk 就自动勾上模块」的结果。Leaf5 上不装的话
        # Magisk App 会卡在启动画面（AMS 对 null ProcessRecord 解引用）。
        self.var_module = tk.BooleanVar(value=True)

        self._build()
        self.after(120, self._pump)
        self.after(300, self.act_probe)

    # ---------------- 布局 ----------------
    def _build(self) -> None:
        head = tk.Frame(self, bg=PANEL, height=56)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="l5tk", bg=PANEL, fg=FG,
                 font=("Segoe UI", 15, "bold")).pack(side="left", padx=(16, 8))
        tk.Label(head, text="v" + __version__, bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left", pady=(6, 0))
        self.lamp = tk.Label(head, text="●", bg=PANEL, fg=MUTED,
                             font=("Segoe UI", 13))
        self.lamp.pack(side="right", padx=(0, 14))
        self.status = tk.Label(head, text="正在查找设备…", bg=PANEL, fg=MUTED,
                               font=("Segoe UI", 10))
        self.status.pack(side="right", padx=8)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=12)

        left = tk.Frame(body, bg=BG, width=220)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        self.btns: list[ttk.Button] = []
        # (按钮文案, 回调, 做什么, 什么情况下用)。
        # 每项两句，缺一不可：第一句说「点下去发生什么 / 前置条件」，第二句说
        # 「什么时候该点这个、什么时候不该点」。只写功能名等于把判断留给用户，
        # 而用户面对八个按钮时最缺的恰恰是那个判断。
        actions = [("① 检测设备", self.act_probe,
                    "读型号 / 内核 / root，只读",
                    "刚连上设备、或换了机器")]
        if gp_ui_enabled():
            actions.append(("② 装 Google Play", self.act_gplay,
                            "需先登录 Google 账号并通过验证",
                            "商店打不开、桌面没图标"))
        actions += [
            ("③ 检查 Root", self.act_root,
             "su / Magisk / SELinux / 槽位",
             "不确定现在有没有 root"),
            ("一键 Root", self.act_autoroot,
             "已 root→adb→exploit→Magisk",
             "第一次用，想一把过"),
            ("装 Magisk App", self.act_magisk,
             "只装 App，不写 boot 分区",
             "已有 root，只缺管理器"),
            ("④ 刷入 boot 镜像", self.act_flash,
             "fastboot 写入补丁好的 img",
             "bootloader 能进、且没被裁"),
            ("EDL 刷 boot（9008）", self.act_edl_flash,
             "9008 直写分区，先备份再写",
             "fastboot 刷不进去时"),
            ("内核适配（换固件）", self.act_adapt,
             "解内核符号 → 编 preload.so",
             "OTA 升级、换 ROM 后"),
            ("打开日志目录", self.act_open_logs,
             "排障用，日志默认已脱敏",
             "任何一步失败、要贴日志"),
        ]
        for text, cmd, hint, when in actions:
            b = ttk.Button(left, text=text, command=cmd)
            b.pack(fill="x", pady=(4, 0))
            self.btns.append(b)
            # 第一行：做什么。ONYX 在 framework 层拦商店，判据是 MMKV 里的
            # gms_enable，而那个值只在账号验证通过的那一刻由系统自己写 ——
            # 不登录就永远停在 need_login，这条前置条件必须摆在按钮上。
            tk.Label(left, text=hint, bg=BG, fg=MUTED, font=("Segoe UI", 9),
                     justify="left", anchor="w", wraplength=190).pack(
                fill="x", padx=(10, 0), pady=(2, 0))
            # 第二行：什么时候用。压一层灰、加「用在」前缀，跟第一行区分开 ——
            # 两行同样亮度会糊成一段话，用户读不出哪句是条件。
            tk.Label(left, text="用在·" + when, bg=BG, fg=DIM,
                     font=("Segoe UI", 8),
                     justify="left", anchor="w", wraplength=190).pack(
                fill="x", padx=(10, 0), pady=(0, 2))
            if text == "一键 Root":
                # 紧跟着一键按钮摆，不塞到列表末尾 —— 它是这条链的选项，
                # 不是独立动作。默认勾上，理由见 __init__ 里那段注释。
                self.chk_module = tk.Checkbutton(
                    left, text="顺带装 AMS 修复模块",
                    variable=self.var_module,
                    bg=BG, fg=MUTED, selectcolor=PANEL,
                    activebackground=BG, activeforeground=FG,
                    font=("Segoe UI", 9), anchor="w",
                    highlightthickness=0, bd=0, padx=0)
                self.chk_module.pack(fill="x", padx=(10, 0), pady=(2, 0))
                # 选项也要交代使用条件：这个框默认勾着，用户要知道「什么时候
                # 该取消」。答案只有一种 —— Magisk App 能正常打开时它是多余的。
                tk.Label(left, text="用在·Magisk App 点开闪退（ONYX 的 AMS 有 NPE）",
                         bg=BG, fg=DIM, font=("Segoe UI", 8),
                         justify="left", anchor="w", wraplength=190).pack(
                    fill="x", padx=(10, 0), pady=(0, 4))

        ttk.Separator(left, orient="horizontal").pack(fill="x", pady=10)
        tk.Label(left, text="机型档案", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self.profile_var = tk.StringVar(value="—")
        tk.Label(left, textvariable=self.profile_var, bg=BG, fg=FG,
                 font=("Consolas", 9), justify="left", wraplength=200).pack(anchor="w", pady=4)

        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        self.text = tk.Text(right, bg="#15161a", fg=FG, insertbackground=FG,
                            relief="flat", wrap="word", font=("Consolas", 9),
                            padx=10, pady=8, state="disabled")
        self.text.pack(fill="both", expand=True)
        self.text.tag_config("ok", foreground=OK)
        self.text.tag_config("warn", foreground=WARN)
        self.text.tag_config("err", foreground=ERR)
        self.text.tag_config("dim", foreground=MUTED)
        self.text.tag_config("head", foreground=ACCENT)

        bar = tk.Frame(self, bg=PANEL, height=26)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.foot = tk.Label(bar, text="就绪", bg=PANEL, fg=MUTED,
                             font=("Segoe UI", 9))
        self.foot.pack(side="left", padx=12)

    # ---------------- 日志 ----------------
    def emit(self, msg: str, tag: str = "") -> None:
        self._q.put((msg, tag))
        self.log.info(msg)

    def _pump(self) -> None:
        try:
            while True:
                msg, tag = self._q.get_nowait()
                self.text.configure(state="normal")
                self.text.insert("end", msg + "\n", tag or ())
                self.text.see("end")
                self.text.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self._pump)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for b in self.btns:
            b.configure(state=state)
        # 勾选框不在 btns 里，单独管：跑到一半改它，读取时机就说不清了
        if getattr(self, "chk_module", None) is not None:
            self.chk_module.configure(state=state)
        self.foot.configure(text="执行中…" if busy else "就绪")

    def run_async(self, fn) -> None:
        """跑一个可能卡住的动作。异常在这里兜住，别让它悄悄死在子线程里。"""
        if self._busy:
            return
        self._set_busy(True)

        def wrapper():
            try:
                fn()
            except L5Error as e:
                self.emit("✗ " + e.render(), "err")
            except Exception:
                self.emit("✗ 未预期错误：\n" + traceback.format_exc(), "err")
            finally:
                self.after(0, lambda: self._set_busy(False))

        threading.Thread(target=wrapper, daemon=True).start()

    # ---------------- 动作 ----------------
    def act_probe(self) -> None:
        def job():
            self.emit("读取设备信息…", "dim")
            # 必须 deep=True：deep=False 不填 root/账号，界面上就永久显示「无」，
            # 和旁边「检查 Root」的结论直接打架。adb root 会重启 adbd，这里不自动做。
            rep = Prober(self.adb, self._bridge()).run(deep=True, allow_adb_root=False)
            self.report = rep
            p = rep.detection.profile
            self.after(0, lambda: self.profile_var.set(
                "%s\n%s\nAndroid %s" % (p.label, p.soc_label or "-",
                                        rep.props.get("ro.build.version.release", "?"))))
            self.after(0, lambda: self.lamp.configure(fg=OK))
            self.after(0, lambda: self.status.configure(text=rep.serial, fg=FG))
            self.emit("机型：%s（匹配分 %d）" % (p.label, rep.detection.score), "head")
            for line in rep.summary().splitlines():
                self.emit("  " + line)
            for w in rep.warnings:
                self.emit("  ! " + w, "warn")
        self.run_async(job)

    def act_gplay(self) -> None:
        def job():
            self.emit("检查商店状态…", "dim")
            gp = GPlay(self.adb, self._bridge(echo_ok=True))
            st = gp.state()
            self.emit("当前：%s  GMS=%s  GSF=%s" % (
                st["version"] or "未安装", st["gms"], st["gsf"]))
            o = gp.enable(base_dir=self.cfg.base_dir, reinstall_if_blocked=True)
            tag = "ok" if o.ok else ("warn" if o.stage == "need_login" else "err")
            self.emit(("✓ " if o.ok else "· ") + o.detail, tag)
            for r in o.reasons:
                self.emit("  " + r, "dim")
            if o.stage == "need_login":
                self.emit("  到设备上登录 Google 账号并完成验证，然后点②再跑一次。", "warn")
        self.run_async(job)

    def act_root(self) -> None:
        def job():
            ops = RootOps(self.adb, self.fb, self._bridge())
            o = ops.status()
            self.emit(("✓ " if o.ok else "· ") + o.detail,
                      "ok" if o.ok else "warn")
            self.emit("  flash.locked=%s  unlocked=%s" % (
                o.data.get("flash_locked") or "?", o.data.get("unlocked")), "dim")
            if not o.ok:
                self.emit("  下一步：userdebug 机型点这里试 adb root；"
                          "user 机型走 Magisk 补 boot → ④ 刷入。", "dim")
        self.run_async(job)

    def act_magisk(self) -> None:
        def job():
            ops = RootOps(self.adb, self.fb, self._bridge())
            apk = ops.magisk_apk()
            if apk is None:
                self.emit("× 这个构建里没有内置 Magisk APK", "err")
                self.emit("  把 Magisk-*.apk 丢进 EXE 同级的 assets\\magisk\\ 再点一次", "dim")
                return
            self.emit("安装内置 %s…" % apk.name, "dim")
            o = ops.install_magisk()
            self.emit(("✓ " if o.ok else "× ") + o.detail, "ok" if o.ok else "err")
            for r in o.reasons:
                self.emit("  " + r, "dim")
        self.run_async(job)

    def act_autoroot(self) -> None:
        """一键：不弹确认框，但会在日志里逐步说明走到了哪一条路。

        Why 不确认：这条链的第 1、2 步（adb root / dd 写 boot）本身都是
        「已备份才写」，第 2 步内部已有四个硬闸。真正的不可逆操作
        （fastboot flash）只在给了镜像时才走，而 UI 这里不给镜像。
        """
        def job():
            from ..ops.autoroot import AutoRoot
            self.emit("== 一键 root ==", "head")
            r = AutoRoot(self.adb, self.fb, self._bridge()).run(
                with_module=self.var_module.get(), reboot=True, image=None,
                allow_fastboot=False)
            self.emit(("✓ " if r.ok else "✗ ") + r.detail,
                      "ok" if r.ok else "err")
            for s in r.steps:
                mark = "✓" if s.ok else ("✗" if s.tried else "·")
                self.emit("  %s %s" % (mark, s.detail.replace("\n", "\n    ")),
                          "dim")
        self.run_async(job)

    def act_flash(self) -> None:
        boot_dir = self.cfg.base_dir / "boot"
        path = filedialog.askopenfilename(
            title="选择 patched boot 镜像",
            initialdir=str(boot_dir if boot_dir.is_dir() else self.cfg.base_dir),
            filetypes=[("镜像", "*.img"), ("全部", "*.*")])
        if not path:
            return
        if not messagebox.askyesno(
                "确认刷入",
                "将刷入：\n%s\n\n刷错 boot 分区会导致设备无法开机。\n"
                "程序会先备份当前 boot，失败时自动回滚。继续？" % Path(path).name):
            return

        def job():
            from ..device import detect
            det = detect(self.adb.props())
            ops = RootOps(self.adb, self.fb, self._bridge())
            self.emit("机型：%s" % det.profile.label, "head")
            o = ops.flash_boot(path, det.profile, backup=True, force=False)
            self.emit(("✓ " if o.ok else "✗ ") + o.detail,
                      "ok" if o.ok else "err")
            for r in o.reasons:
                self.emit("  " + r, "dim")
            if o.data.get("backup"):
                self.emit("  备份：%s" % o.data["backup"], "dim")
        self.run_async(job)

    def act_edl_flash(self) -> None:
        """9008 直写 boot 分区。

        ONYX 的 abl 把 fastboot 的 flash handler 裁掉了（`remote: 'unknown
        command'`），所以上面那个按钮在这几台机器上必然失败。9008 是 SoC
        自己的下载模式，走 Firehose，跟 fastboot 无关 —— 裁 fastboot 裁不掉它。

        前提是 programmer 与 SoC 匹配：Leaf5 是 LITO，Tab 三台是 BENGAL，
        lito 那份在 Tab 上握手就不过。这里不猜机型，只把真实失败原因打出来。
        """
        boot_dir = self.cfg.base_dir / "boot"
        path = filedialog.askopenfilename(
            title="选择要写入的 boot 镜像（Magisk 打过补丁的）",
            initialdir=str(boot_dir if boot_dir.is_dir() else self.cfg.base_dir),
            filetypes=[("镜像", "*.img"), ("全部", "*.*")])
        if not path:
            return
        if not messagebox.askyesno(
                "确认用 9008 写 boot",
                "将写入：\n%s\n\n"
                "流程：备份原分区 → 写入 → 读回比对 sha256（不符自动回滚）\n"
                "期间设备会黑屏并重启进 9008 下载模式，完成后自动回系统。\n"
                "全程约 3-6 分钟（96 MB 分区要把备份、写入、读回各走一遍）。\n\n"
                "继续？" % Path(path).name):
            return

        def job():
            from ..ops.edl import EdlOps
            ops = EdlOps(self.adb, self._bridge())
            self.emit("== EDL 刷 boot ==", "head")
            self.emit("  分区按当前槽位自动判定", "dim")
            o = ops.flash(path)
            self.emit(("✓ " if o.ok else "✗ ") + o.detail,
                      "ok" if o.ok else "err")
            for r in o.reasons:
                self.emit("  " + r, "dim")
            for k, label in (("backup", "备份"), ("readback", "读回")):
                if o.data.get(k):
                    self.emit("  %s：%s" % (label, o.data[k]), "dim")
        self.run_async(job)

    def act_adapt(self) -> None:
        """内核适配向导。单独一个窗口 —— 它要下 NDK、跑几分钟编译，
        不能占住主界面的按钮。带着当前 adb 进去，省得用户再选设备。"""
        from .adapt_win import open_window
        open_window(self, self.cfg, self.adb, self.log,
                    serial=self.cfg.serial or "")

    def act_open_logs(self) -> None:
        try:
            webbrowser.open(self.cfg.log_dir.as_uri())
        except Exception as e:
            self.emit("打不开目录：%s（%s）" % (self.cfg.log_dir, e), "err")

    def _bridge(self, echo_ok: bool = False) -> Logger:
        """把 Logger 的输出接到界面：界面拿一行，落盘仍走 self.log。"""
        app = self
        base = self.log

        class UiLog(Logger):
            def _emit(self, level: str, msg: str) -> None:
                body = msg if base.plain else mask(msg)
                tag = {"ok": "ok", "warn": "warn", "err": "err"}.get(level, "")
                if level == "ok" and not echo_ok:
                    tag = ""
                app._q.put((body, tag))
                base._emit(level, msg)

        return UiLog(log_dir=None, echo=False)

    def destroy(self) -> None:
        self.log.close()
        super().destroy()


def run() -> int:
    app = App()
    app.mainloop()
    return 0
