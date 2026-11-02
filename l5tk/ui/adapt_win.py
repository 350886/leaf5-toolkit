"""内核适配向导窗口。

给「换了固件 / 换了内核」的机器重新生成 preload.so。独立 Toplevel，关掉不影响
主界面正在跑的流程。

五步：
  ① 内核来源 —— 打开即自动查找（记忆 → 常见目录 → 全盘），可切候选
  ② 生成 target.h —— 从镜像反解符号偏移
  ③ 填 4 个手工宏 —— 结构体/栈常量，符号表里没有
  ④ 编译 preload.so —— 首次会下 NDK（约 700MB）
  ⑤ 装进工具 —— 之后 root exploit 按设备自动挑它

三条硬规矩跟主界面一致：耗时不占主线程、后台只往 queue 放、忙碌时整组禁用。
"""

from __future__ import annotations

import os
import queue
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..core import kimage
from ..logx import Logger
from ..ops.adapt import (Adapter, MANUAL_MACROS, fill_manuals, placeholders,
                         template_manuals)

BG = "#1e1f22"
PANEL = "#2b2d30"
FG = "#d7d9dd"
MUTED = "#8b8f96"
ACCENT = "#4c9aff"
OK = "#4ec9a0"
WARN = "#e2b341"
ERR = "#ef6b6b"

FONT = ("Microsoft YaHei UI", 9)
MONO = ("Consolas", 9)


class AdaptWindow(tk.Toplevel):
    def __init__(self, master, cfg, adb, log: Logger, serial: str = ""):
        super().__init__(master)
        self.title("内核适配向导 — 换固件后重编 preload.so")
        self.geometry("880x700")
        self.minsize(820, 640)
        self.configure(bg=BG)
        self.cfg = cfg
        self.log = log
        self.adapter = Adapter(cfg, log, adb, serial=serial)

        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.src_path = ""
        self.target = None            # GenResult
        self.so_path = Path()
        self.cands: list[dict] = []
        self.others: list[dict] = []
        self._cand_i = 0
        self._disk_stop = None

        self._build()
        self.after(80, self._pump)
        self.after(200, self.act_autoscan)

    # ---------------- 布局 ----------------
    def _build(self) -> None:
        head = tk.Frame(self, bg=PANEL, height=50)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="内核适配向导", bg=PANEL, fg=FG,
                 font=("Segoe UI", 13, "bold")).pack(side="left", padx=14)
        tk.Label(head, text="换过固件 → 用设备上正在跑的内核重算 62 个偏移",
                 bg=PANEL, fg=MUTED, font=FONT).pack(side="left")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=10)

        # ① 来源
        row = tk.Frame(body, bg=BG)
        row.pack(fill="x")
        tk.Label(row, text="① 内核来源", bg=BG, fg=ACCENT,
                 font=(FONT[0], 9, "bold"), width=11, anchor="w").pack(side="left")
        self.ent_src = tk.Entry(row, bg="#15161a", fg=FG, insertbackground=FG,
                                relief="flat", font=MONO)
        self.ent_src.pack(side="left", fill="x", expand=True, padx=(0, 6), ipady=4)
        self.btn_browse = ttk.Button(row, text="浏览…", width=7, command=self.act_browse)
        self.btn_browse.pack(side="left", padx=2)
        self.btn_scan = ttk.Button(row, text="自动查找", width=9, command=self.act_autoscan)
        self.btn_scan.pack(side="left", padx=2)
        self.btn_cand = ttk.Button(row, text="候选", width=6, command=self.act_cand)
        self.btn_cand.pack(side="left", padx=2)
        self.btn_disk = ttk.Button(row, text="全盘搜", width=7, command=self.act_disk_scan)
        self.btn_disk.pack(side="left", padx=2)
        self.btn_ks = ttk.Button(row, text="从设备读符号", width=11, command=self.act_kallsyms)
        self.btn_ks.pack(side="left", padx=2)

        self.lbl_src = tk.Label(body, text="正在查找本机已有的内核镜像…", bg=BG,
                                fg=MUTED, font=FONT, anchor="w", justify="left",
                                wraplength=830)
        self.lbl_src.pack(fill="x", pady=(4, 8))

        # ② 生成
        row2 = tk.Frame(body, bg=BG)
        row2.pack(fill="x")
        tk.Label(row2, text="② 生成 target.h", bg=BG, fg=ACCENT,
                 font=(FONT[0], 9, "bold"), width=11, anchor="w").pack(side="left")
        self.btn_gen = ttk.Button(row2, text="从镜像反解符号 → 生成", width=24,
                                  command=self.act_gen)
        self.btn_gen.pack(side="left")
        self.btn_open = ttk.Button(row2, text="打开 target.h", width=14,
                                   command=self.act_open_target)
        self.btn_open.pack(side="left", padx=6)
        self.lbl_gen = tk.Label(row2, text="未生成", bg=BG, fg=MUTED, font=FONT)
        self.lbl_gen.pack(side="left", padx=6)

        self.tbl = tk.Text(body, bg="#15161a", fg=FG, relief="flat", height=5,
                           font=MONO, padx=8, pady=4, state="disabled", wrap="none")
        self.tbl.pack(fill="x", pady=6)
        self.tbl.tag_config("hdr", foreground=ACCENT)
        self.tbl.tag_config("diff", foreground=WARN)
        self.tbl.tag_config("note", foreground=MUTED)

        # ③ 四个手工宏
        tk.Label(body, text="③ 手工宏（能扫出来的已自动填好，剩下的才要你填）",
                 bg=BG, fg=ACCENT, font=(FONT[0], 9, "bold"), anchor="w").pack(fill="x")
        self.fill_entries: dict[str, tk.Entry] = {}
        grid = tk.Frame(body, bg=BG)
        grid.pack(fill="x", pady=4)
        for i, (macro, hint) in enumerate(MANUAL_MACROS):
            tk.Label(grid, text=macro, bg=BG, fg=FG, font=MONO, width=26,
                     anchor="w").grid(row=i, column=0, sticky="w")
            e = tk.Entry(grid, bg="#15161a", fg=FG, insertbackground=FG,
                         relief="flat", font=MONO, width=12)
            e.grid(row=i, column=1, sticky="w", padx=(0, 8), ipady=2)
            self.fill_entries[macro] = e
            tk.Label(grid, text=hint, bg=BG, fg=MUTED, font=FONT, anchor="w",
                     justify="left").grid(row=i, column=2, sticky="w")
        self.btn_inherit = ttk.Button(grid, text="沿用模板值", width=12,
                                      command=self.act_inherit)
        self.btn_inherit.grid(row=0, column=3, rowspan=2, padx=6)

        # ④⑤ 编译 / 装机
        row3 = tk.Frame(body, bg=BG)
        row3.pack(fill="x", pady=(8, 0))
        tk.Label(row3, text="④ 编译", bg=BG, fg=ACCENT,
                 font=(FONT[0], 9, "bold"), width=11, anchor="w").pack(side="left")
        self.btn_build = ttk.Button(row3, text="编译 preload.so", width=18,
                                    command=self.act_build)
        self.btn_build.pack(side="left")
        self.lbl_build = tk.Label(row3, text="未编译（首次会下 NDK，约 700MB）",
                                  bg=BG, fg=MUTED, font=FONT)
        self.lbl_build.pack(side="left", padx=8)

        row4 = tk.Frame(body, bg=BG)
        row4.pack(fill="x", pady=(6, 0))
        tk.Label(row4, text="⑤ 装进工具", bg=BG, fg=ACCENT,
                 font=(FONT[0], 9, "bold"), width=11, anchor="w").pack(side="left")
        self.btn_use = ttk.Button(row4, text="装进工具并在 root 时使用", width=24,
                                  command=self.act_use)
        self.btn_use.pack(side="left")
        self.lbl_use = tk.Label(row4, text="", bg=BG, fg=MUTED, font=FONT)
        self.lbl_use.pack(side="left", padx=8)

        # 日志
        self.text = tk.Text(body, bg="#15161a", fg=FG, relief="flat", wrap="word",
                            font=MONO, padx=10, pady=8, state="disabled")
        self.text.pack(fill="both", expand=True, pady=(10, 6))
        self.text.tag_config("ok", foreground=OK)
        self.text.tag_config("warn", foreground=WARN)
        self.text.tag_config("err", foreground=ERR)
        self.text.tag_config("dim", foreground=MUTED)

        bar = tk.Frame(self, bg=PANEL, height=26)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.foot = tk.Label(bar, text="就绪", bg=PANEL, fg=MUTED, font=FONT)
        self.foot.pack(side="left", padx=12)

    # ---------------- 队列 ----------------
    def say(self, msg: str, tag: str = "") -> None:
        self.q.put(("log", msg, tag))

    def _pump(self) -> None:
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    _, msg, tag = item
                    self.text.configure(state="normal")
                    self.text.insert("end", msg + "\n", tag or ())
                    self.text.see("end")
                    self.text.configure(state="disabled")
                elif kind == "src":
                    _, path, note, color = item
                    self.ent_src.delete(0, "end")
                    self.ent_src.insert(0, path)
                    self.lbl_src.configure(text=note, fg=color)
                elif kind == "status":
                    _, which, txt, color = item
                    {"gen": self.lbl_gen, "build": self.lbl_build,
                     "use": self.lbl_use, "src": self.lbl_src,
                     "foot": self.foot}[which].configure(text=txt, fg=color)
                elif kind == "gen":
                    self._on_gen(item[1])
                elif kind == "busy":
                    self._set_busy(item[1])
                elif kind == "so":
                    self.so_path = Path(item[1])
                elif kind == "done":
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for w in (self.btn_browse, self.btn_scan, self.btn_cand, self.btn_disk,
                  self.btn_ks, self.btn_gen, self.btn_build, self.btn_use,
                  self.btn_inherit, self.btn_open):
            w.configure(state=state)
        self.foot.configure(text="执行中…" if busy else "就绪")

    def _run(self, fn, label: str) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self.say("▶ %s" % label, "dim")

        def work():
            try:
                fn()
            except Exception as e:
                if isinstance(e, tk.TclError):
                    return
                self.say("✗ %s: %s" % (type(e).__name__, e), "err")
                if os.environ.get("L5TK_DEBUG"):
                    self.say(traceback.format_exc(), "dim")
            finally:
                self.q.put(("done",))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- ① 来源 ----------------
    def act_autoscan(self) -> None:
        def job():
            self.say("查找本机内核镜像（记忆 → 常见目录）…", "dim")
            recent = kimage.load_recent(self.adapter.root)
            if recent:
                self.say("  记忆里有 %d 个上次用过的路径" % len(recent), "dim")
            usable, others = kimage.scan_kernels(
                roots=kimage.scan_roots([str(self.adapter.root)]),
                log=lambda s: self.say(s, "dim"))
            self.cands, self.others = usable, others
            self._cand_i = 0
            if usable:
                self._take(usable[0]["path"], remember=False)
                self.q.put(("status", "src",
                            "找到 %d 个可用镜像；点「候选」切换。已选：%s（%s）"
                            % (len(usable), os.path.basename(usable[0]["path"]),
                               usable[0].get("note") or "可用"), OK))
            elif recent:
                self._take(recent[0], remember=False)
                self.q.put(("status", "src",
                            "扫描没命中，用记忆里的路径：%s" % recent[0], WARN))
            else:
                self.q.put(("status", "src",
                            "本机没找到现成的内核。两条路：把设备 boot 分区的 "
                            "boot.img 拖进来（能现场反解）；或设备已 root 时点"
                            "「从设备读符号」", WARN))

        self._run(job, "自动查找内核镜像")

    def act_disk_scan(self) -> None:
        def job():
            self._disk_stop = False
            self.say("全盘扫描（找第一个可用就停，最多 180 秒）…", "dim")
            usable, others = kimage.scan_disks(
                log=lambda s: self.say(s, "dim"),
                stop=lambda: bool(self._disk_stop), first_hit=True)
            self.cands, self.others = usable, others
            self._cand_i = 0
            if usable:
                self._take(usable[0]["path"], remember=False)
                self.q.put(("status", "src",
                            "全盘命中：%s（%s）" % (usable[0]["path"],
                                                  usable[0].get("note") or ""), OK))
            else:
                self.q.put(("status", "src",
                            "全盘没找到能用的镜像（%d 个不可用）" % len(others), WARN))

        self._run(job, "全盘扫描")

    def act_cand(self) -> None:
        pool = self.cands or self.others
        if not pool:
            self.say("没有候选 —— 先点「自动查找」或「全盘搜」", "warn")
            return
        self._cand_i = (self._cand_i + 1) % len(pool)
        info = pool[self._cand_i]
        ok = bool(info.get("usable", True))
        self._take(info["path"], remember=False)
        self.q.put(("status", "src",
                    "%s%s（%d/%d）%s"
                    % ("可用：" if ok else "不可用：",
                       os.path.basename(info["path"]), self._cand_i + 1, len(pool),
                       info.get("note") or ""), OK if ok else WARN))

    def act_browse(self) -> None:
        p = filedialog.askopenfilename(
            parent=self,
            title="选择 vmlinux.elf、kallsyms 导出文件或 boot.img",
            filetypes=[("符号来源", "*.elf *.txt *.map *.img *.bin *.image"),
                       ("boot.img", "*.img"),
                       ("vmlinux.elf", "*.elf"),
                       ("kallsyms / System.map", "*.txt *.map"),
                       ("全部文件", "*.*")])
        if not p:
            return
        info = kimage.probe_kernel(p, deep=False)
        self._take(p)
        if info and info["usable"]:
            self.q.put(("status", "src",
                        "已选：%s（%s）" % (os.path.basename(p),
                                           info.get("note") or "可用"), OK))
        elif info:
            self.q.put(("status", "src",
                        "已选：%s　%s" % (os.path.basename(p), info.get("note")),
                        WARN))
        else:
            self.q.put(("status", "src",
                        "已选：%s　不是内核镜像（既非 ELF 也非 boot.img / Image）"
                        % os.path.basename(p), ERR))

    def _take(self, path: str, remember: bool = True) -> None:
        self.src_path = path
        self.ent_src.delete(0, "end")
        self.ent_src.insert(0, path)
        if remember:
            kimage.remember_kernel(self.adapter.root, path)

    def act_kallsyms(self) -> None:
        def job():
            out = str(self.adapter.src_root /
                      ("kallsyms-%s.txt" % __import__("time").strftime("%Y%m%d-%H%M%S")))
            self.say("从设备导出 /proc/kallsyms（需要 su，约 6MB）…", "dim")
            path, st = kimage.grab_kallsyms(
                out, adb=self.cfg.adb, serial=self.cfg.serial or "",
                log=lambda s: self.say(s, "dim"))
            self._take(path)
            self.q.put(("status", "src",
                        "已导出 %s：%d 行 / %d 符号 / %s"
                        % (os.path.basename(path), st["lines"], st["syms"],
                           kimage.human_size(st["size"])), OK))

        self._run(job, "导出 kallsyms")

    # ---------------- ② 生成 ----------------
    def act_gen(self) -> None:
        def job():
            src = self.ent_src.get().strip().strip('"')
            if not src or not os.path.isfile(src):
                raise RuntimeError("先选一份存在的 boot.img / Image / ELF / kallsyms")
            self.q.put(("status", "foot", "解析符号表…", MUTED))
            r = self.adapter.gen(src, log=lambda s: self.say(s, "dim"))
            self.q.put(("gen", r))

        self._run(job, "生成 target.h")

    def _on_gen(self, r) -> None:
        self.target = r
        self.q.put(("status", "gen", "已生成 %s" % r.tid, OK))
        self._fill_table(r.diffs)
        # 三档来源：指令扫出来的 > 同源继承的 > 用户自己填的
        tpl = template_manuals(self.adapter.template())
        for macro, e in self.fill_entries.items():
            e.delete(0, "end")
            if macro in r.auto:
                e.insert(0, "0x%X" % r.auto[macro])
            elif macro in tpl and r.manual == "inherited":
                e.insert(0, "0x%X" % tpl[macro])
        if r.auto:
            self.say("自动定标：%s"
                     % "  ".join("%s=0x%X" % (k, v) for k, v in r.auto.items()),
                     "ok")
        if r.unfilled:
            self.say("还差 %d 个手工宏：%s" % (len(r.unfilled), ", ".join(r.unfilled)),
                     "warn")
            self.say("  这几个值指令里读不出来：按提示反汇编或 panic 定标后填进来",
                     "warn")
        else:
            self.say("手工宏%s，可以直接编译" %
                     ("已全部定标" if r.auto else
                      "已沿用模板值（同源构建）" if r.manual == "inherited"
                      else "已填"), "ok")
        if r.symfp:
            self.say("符号指纹已存 %s（以后不用 root 也能认出这台设备）" % r.symfp,
                     "dim")

    def _fill_table(self, diffs) -> None:
        self.tbl.configure(state="normal")
        self.tbl.delete("1.0", "end")
        self.tbl.insert("end", "%-34s%14s%14s%12s\n"
                        % ("宏", "模板值", "本机镜像", "差"), "hdr")
        self.tbl.insert("end", "-" * 76 + "\n", "hdr")
        if not diffs:
            self.tbl.insert("end", "（没有差异 —— 这份镜像就是模板对应的那次构建）\n",
                            "note")
        for macro, o, v in diffs[:6]:
            self.tbl.insert("end", "%-34s%14s%14s%+12d\n"
                            % (macro, hex(o), hex(v), v - o), "diff")
        if len(diffs) > 6:
            self.tbl.insert("end", "… 另 %d 个\n" % (len(diffs) - 6), "note")
        self.tbl.configure(state="disabled")

    def act_inherit(self) -> None:
        tpl = template_manuals(self.adapter.template())
        if not tpl:
            self.say("模板里读不到这 4 个值", "err")
            return
        for macro, e in self.fill_entries.items():
            e.delete(0, "end")
            if macro in tpl:
                e.insert(0, "0x%X" % tpl[macro])
        self.say("已填入模板值 —— 只在「这份镜像与模板同源」时才正确", "warn")

    def act_open_target(self) -> None:
        if not self.target:
            messagebox.showinfo("还没生成", "先点「② 生成 target.h」", parent=self)
            return
        p = Path(self.target.tdir) / "target.h"
        try:
            os.startfile(str(p))          # Windows 用默认编辑器打开
        except Exception as e:
            messagebox.showerror("打不开", "%s\n\n路径：%s" % (e, p), parent=self)

    # ---------------- ④ 编译 ----------------
    def _values(self) -> dict:
        out = {}
        for macro, e in self.fill_entries.items():
            raw = e.get().strip()
            if not raw:
                continue
            try:
                out[macro] = int(raw, 0)
            except ValueError:
                raise RuntimeError("%s 的值不是整数：%s" % (macro, raw))
        return out

    def act_build(self) -> None:
        def job():
            if not self.target:
                raise RuntimeError("先点「② 生成 target.h」")
            tdir = self.target.tdir
            vals = self._values()
            left = fill_manuals(tdir, vals, log=lambda s: self.say(s, "dim"))
            left = left or placeholders(tdir)
            if left:
                raise RuntimeError("还差 %s 没填（符号表里没有，必须自己定标）"
                                   % ", ".join(left))
            self.say("开始编译（首次会下 NDK，约 700MB；编译约 1 分钟）…", "dim")
            so, md5, size = self.adapter.compile(
                tdir, progress=lambda d: self.q.put(
                    ("status", "build", "%s %d%%" % (d.get("phase", ""),
                                                     100 * d.get("done", 0)
                                                     // max(1, d.get("total", 1))),
                     MUTED)),
                log=lambda s: self.say(s, "dim"))
            self.q.put(("so", str(so)))
            self.q.put(("status", "build", "%d 字节  md5=%s" % (size, md5), OK))
            self.say("编译完成：%s" % so, "ok")

        self._run(job, "编译 preload.so")

    # ---------------- ⑤ 装进工具 ----------------
    def act_use(self) -> None:
        if not self.so_path or not Path(self.so_path).is_file():
            messagebox.showinfo("还没编译", "先点「④ 编译 preload.so」", parent=self)
            return

        def job():
            dev = self.adapter.device_meta()
            tid = self.target.tid if self.target else (
                Path(self.so_path).stem)
            meta = {"fingerprint": dev.get("fingerprint", ""),
                    "kernel": dev.get("kernel", ""),
                    "note": "由适配向导生成"}
            t = self.adapter.install(tid, self.so_path, meta)
            self.adapter.use(t.id)
            self.q.put(("status", "use", "已启用 %s" % t.id, OK))
            self.say("已装进 %s" % t.dir, "ok")
            self.say("  kernel = %s" % (t.kernel or "（设备没连，未记录）"), "dim")
            self.say("  之后 root exploit / 一键 root 会按设备自动挑它", "dim")

        self._run(job, "装进工具")


def open_window(master, cfg, adb, log: Logger, serial: str = "") -> AdaptWindow:
    return AdaptWindow(master, cfg, adb, log, serial=serial)
