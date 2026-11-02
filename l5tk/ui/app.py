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

# 配色搬到 theme.py：使用说明窗口、适配向导要用同一套，散在这里会让它们
# 反向 import app（连带把整条 ops 依赖链拖进窗口模块）。
from .theme import ACCENT, BG, DIM, ERR, FG, FONT_CN, FONT_UI, MUTED, OK, PANEL, WARN  # noqa: E402

W = 900
# 每个按钮多了「用在」那一行，左栏比原来高约 8×14px；窗口跟着长高，
# 否则日志区被挤成一条缝 —— 日志是本工具唯一的事后证据。
# 又多了一个「EDL 刷 boot」入口（fastboot 在 ONYX 上被裁，得留替代通道），
# 后来又加了「取原厂 boot」（刷 boot 的镜像从官方 OTA 里抽，不用自己备份）。
H = 918 if gp_ui_enabled() else 890


def _fit_height(h: int, win) -> int:
    """窗口比屏幕高时压进屏幕内 —— 否则小笔记本上日志区整个跑到屏幕外。"""
    try:
        screen = win.winfo_screenheight()
    except Exception:
        return h
    return max(600, min(h, screen - 60))


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("l5tk %s — 多机型 Android 工具箱" % __version__)
        self.geometry("%dx%d" % (W, _fit_height(H, self)))
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
        self._build_menu()
        # 快捷键挂在根窗口上：说明窗口是 Toplevel，它自己另有一套绑定
        self.bind("<F1>", lambda _e: self.act_help())
        self.bind("<Control-l>", lambda _e: self.act_clear_log())
        self.bind("<Control-q>", lambda _e: self.quit())
        self.after(120, self._pump)
        self.after(300, self.act_probe)

    def _on_wheel(self, e) -> None:
        """左栏滚轮。只在指针落在左栏时滚 —— 绑到全局不判位置的话，
        在日志区滚轮会同时滚左栏和日志，两个都跳得很怪。"""
        try:
            px = self.winfo_pointerx() - self._cv.winfo_rootx()
            py = self.winfo_pointery() - self._cv.winfo_rooty()
            if not (0 <= px <= self._cv.winfo_width()
                    and 0 <= py <= self._cv.winfo_height()):
                return
        except Exception:
            return
        num = getattr(e, "num", 0)
        if num in (4, 5):
            step = -2 if num == 4 else 2
        else:
            step = -2 if getattr(e, "delta", 0) > 0 else 2
        self._cv.yview_scroll(step, "units")

    # ---------------- 菜单 ----------------
    def _menu(self, parent: tk.Menu) -> tk.Menu:
        """深色菜单。tk 默认菜单在 Windows 上是浅色的，跟窗口拼在一起很突兀，
        所以每个子菜单都要显式给底色 —— 这个没法继承。"""
        return tk.Menu(parent, tearoff=0, bg=PANEL, fg=FG, bd=0,
                       activebackground=ACCENT, activeforeground="#ffffff",
                       activeborderwidth=0, font=(FONT_CN, 9))

    def _build_menu(self) -> None:
        bar = tk.Menu(self, tearoff=0, bg=PANEL, fg=FG, bd=0,
                      activebackground=ACCENT, activeforeground="#ffffff",
                      activeborderwidth=0, font=(FONT_CN, 9))

        m_file = self._menu(bar)
        m_file.add_command(label="打开日志目录", accelerator="",
                           command=self.act_open_logs)
        m_file.add_command(label="打开工作目录（备份在这）",
                           command=self.act_open_work)
        m_file.add_separator()
        m_file.add_command(label="导出使用说明…", command=self.act_export_help)
        m_file.add_separator()
        m_file.add_command(label="退出", accelerator="Ctrl+Q", command=self.quit)
        bar.add_cascade(label="文件", menu=m_file)

        m_tool = self._menu(bar)
        m_tool.add_command(label="重新检测设备", command=self.act_probe)
        m_tool.add_command(label="清空日志窗口", accelerator="Ctrl+L",
                           command=self.act_clear_log)
        bar.add_cascade(label="工具", menu=m_tool)

        m_help = self._menu(bar)
        m_help.add_command(label="使用说明", accelerator="F1",
                           command=self.act_help)
        m_help.add_command(label="导出为 txt…", command=self.act_export_help)
        m_help.add_separator()
        m_help.add_command(label="项目主页", command=self.act_homepage)
        m_help.add_command(label="关于", command=self.act_about)
        bar.add_cascade(label="帮助", menu=m_help)

        self.configure(menu=bar)
        self.menubar = bar

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

        # 左栏放进 Canvas 里滚动。10 个按钮 + 每项两行说明 ≈ 700px，
        # 而 1366×768 的笔记本可用高度只有 650 上下 —— 固定高度时
        # pack 放不下的按钮会**直接不显示**（不是滚动），用户就看不到
        # 「打开日志目录」，排障第一步就断了。
        wrap = tk.Frame(body, bg=BG, width=224)
        wrap.pack(side="left", fill="y")
        wrap.pack_propagate(False)

        self._cv = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0, width=206)
        self._sb = ttk.Scrollbar(wrap, orient="vertical",
                                 command=self._cv.yview)
        self._cv.configure(yscrollcommand=self._sb.set)
        self._sb.pack(side="right", fill="y")
        self._cv.pack(side="left", fill="both", expand=True)

        left = tk.Frame(self._cv, bg=BG)
        # 留个引用：Canvas 里的窗口不在 winfo_children() 里（它是 item 不是
        # child widget），外部想摸左栏只能靠这个属性。
        self.left_frame = left
        self._left_win = self._cv.create_window((0, 0), window=left, anchor="nw",
                                                width=190)
        left.bind("<Configure>",
                  lambda _e: self._cv.configure(scrollregion=self._cv.bbox("all")))
        self._cv.bind("<Configure>",
                      lambda e: self._cv.itemconfigure(self._left_win, width=e.width))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(seq, self._on_wheel)

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
            ("④ 取原厂 boot", self.act_payload,
             "解官方 update.zip，抽出 boot.img",
             "手上没镜像、或刷坏了要原厂底片"),
            ("⑤ 刷入 boot 镜像", self.act_flash,
             "自动选 dd / fastbootd 写入",
             "有打好补丁的 img，要写进设备"),
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
            b.pack(fill="x", pady=(3, 0))
            self.btns.append(b)
            # 第一行：做什么。ONYX 在 framework 层拦商店，判据是 MMKV 里的
            # gms_enable，而那个值只在账号验证通过的那一刻由系统自己写 ——
            # 不登录就永远停在 need_login，这条前置条件必须摆在按钮上。
            tk.Label(left, text=hint, bg=BG, fg=MUTED, font=("Segoe UI", 9),
                     justify="left", anchor="w", wraplength=190).pack(
                fill="x", padx=(10, 0), pady=(1, 0))
            # 第二行：什么时候用。压一层灰、加「用在」前缀，跟第一行区分开 ——
            # 两行同样亮度会糊成一段话，用户读不出哪句是条件。
            tk.Label(left, text="用在·" + when, bg=BG, fg=DIM,
                     font=("Segoe UI", 8),
                     justify="left", anchor="w", wraplength=190).pack(
                fill="x", padx=(10, 0), pady=(0, 1))
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
                self.chk_module.pack(fill="x", padx=(10, 0), pady=(1, 0))
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
        """刷 boot。通路由 ops 自己选：dd → fastbootd → fastboot。

        ONYX 的 abl 把 bootloader 模式下的 flash handler 裁了，所以这里
        不再默认「重启进 bootloader 刷」：有 su 就直接 dd（不重启），
        没 su 就进 fastbootd（Android 里的 userspace 写入，不经 abl），
        bootloader 那一步只留作最后兜底。
        """
        boot_dir = self.cfg.base_dir / "boot"
        path = filedialog.askopenfilename(
            title="选择打过 Magisk 补丁的 boot 镜像",
            initialdir=str(boot_dir if boot_dir.is_dir() else self.cfg.base_dir),
            filetypes=[("镜像", "*.img"), ("全部", "*.*")])
        if not path:
            return
        if not messagebox.askyesno(
                "确认刷入",
                "将刷入：\n%s\n\n"
                "写入方式：自动 —— 有 root 就直接 dd（不重启）；\n"
                "没 root 就重启进 fastbootd 写（厂商裁了 bootloader 的 flash，\n"
                "fastbootd 走的是另一套实现）。\n\n"
                "有 root 时会先备份当前 boot、失败自动回滚；\n"
                "没 root 时备份不了，失败要用原厂 boot 重刷。继续？"
                % Path(path).name):
            return

        def job():
            from ..device import detect
            det = detect(self.adb.props())
            ops = RootOps(self.adb, self.fb, self._bridge())
            self.emit("机型：%s" % det.profile.label, "head")
            o = ops.flash_boot(path, det.profile, backup=True, force=False,
                               via="auto")
            self.emit(("✓ " if o.ok else "✗ ") + o.detail,
                      "ok" if o.ok else "err")
            for r in o.reasons:
                self.emit("  " + r, "dim")
            if o.data.get("via"):
                self.emit("  通路：%s   分区：%s"
                          % (o.data["via"], o.data.get("partition", "?")), "dim")
            if o.data.get("backup"):
                self.emit("  备份：%s" % o.data["backup"], "dim")
        self.run_async(job)

    def act_payload(self) -> None:
        """从官方 update.zip 里抽原厂 boot。

        为什么要有这个入口：「刷 boot 的镜像从哪来」一直是个坑 ——
        自己 dd 备份的前提是设备已经 root，没 root 的机器连块设备都读不了。
        官方 OTA 里的 payload.bin 带着完整 boot，纯本地解包就能拿到；
        这份原厂镜像既能交给 Magisk 打补丁，也能在刷坏时刷回来。
        """
        fw = self.cfg.base_dir / "firmware"
        path = filedialog.askopenfilename(
            title="选择官方固件包 update.zip",
            initialdir=str(fw if fw.is_dir() else self.cfg.base_dir),
            filetypes=[("OTA 包", "*.zip"), ("全部", "*.*")])
        if not path:
            return
        out = self.cfg.work_dir / "boot"

        def job():
            from ..core.payload import Payload
            try:
                with Payload(path) as p:
                    self.emit("payload v%d，%d 个分区"
                              % (p.version, len(p.manifest.partitions)), "head")
                    boot = p.find_boot()
                    if boot is None:
                        self.emit("✗ 包里既没有 boot 也没有 init_boot", "err")
                        return
                    self.emit("打补丁目标：%s（%.1f MB），解包中…"
                              % (boot.name, boot.size / 1048576.0))
                    info = p.extract(boot.name,
                                     out / ("%s_stock.img" % boot.name))
            except L5Error as e:
                self.emit("✗ " + e.render(), "err")
                return
            self.emit("✓ 已写出 %s（%.1f MB）"
                      % (info["path"], info["bytes"] / 1048576.0), "ok")
            self.emit("  这是原厂镜像：可以交给 Magisk 打补丁，刷坏了也用它刷回来。",
                      "dim")
            self.emit("  下一步点「⑤ 刷入 boot 镜像」，选打好补丁的那份。", "dim")
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

    def act_open_work(self) -> None:
        """备份、抽出来的 boot、适配工作区都在这里 —— 排障时要翻的是它。"""
        d = self.cfg.work_dir
        try:
            d.mkdir(parents=True, exist_ok=True)
            webbrowser.open(d.as_uri())
        except Exception as e:
            self.emit("打不开目录：%s（%s）" % (d, e), "err")

    def act_clear_log(self) -> None:
        """只清界面，不动盘上的日志 —— 日志是事后唯一证据，不能因为看着乱就删。"""
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.foot.configure(text="日志窗口已清空（盘上的日志不受影响）")

    def act_help(self) -> None:
        from .helpwin import open_help
        open_help(self, self.cfg)

    def act_export_help(self) -> None:
        from .helpwin import export_plain
        try:
            path = export_plain(self)
        except OSError as e:
            messagebox.showerror("导出失败", str(e), parent=self)
            return
        if path:
            self.emit("使用说明已导出：%s" % path, "dim")

    def act_homepage(self) -> None:
        webbrowser.open("https://github.com/350886/leaf5-toolkit")

    def act_about(self) -> None:
        from .helpwin import open_about
        open_about(self, self.cfg)

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
