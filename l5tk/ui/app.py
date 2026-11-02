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
ACCENT = "#4c9aff"
OK = "#4ec9a0"
WARN = "#e2b341"
ERR = "#ef6b6b"

W = 900
H = 720 if gp_ui_enabled() else 688   # 少一行按钮就矮 32px，不留空档


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
        actions = [("① 检测设备", self.act_probe)]
        if gp_ui_enabled():
            actions.append(("② 装 Google Play", self.act_gplay))
        actions += [
            ("③ 检查 Root", self.act_root),
            ("④ 刷入 boot 镜像", self.act_flash),
            ("打开日志目录", self.act_open_logs),
        ]
        for text, cmd in actions:
            b = ttk.Button(left, text=text, command=cmd)
            b.pack(fill="x", pady=4)
            self.btns.append(b)

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
            rep = Prober(self.adb, self._bridge()).run(deep=False)
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
