"""使用说明窗口 + 关于窗口。

为什么不塞进主界面右栏：说明有两万字量级，塞进去会把日志挤没；日志是这个工具
唯一的事后证据，不能给说明让位。所以单独一个 Toplevel，可以一边开着说明一边操作。

渲染只认 helpdoc 里那几种行首标记，不引 markdown 库 —— 打包体积和依赖都不值当。
"""

from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import filedialog, ttk

from .. import __version__
from . import helpdoc
from .theme import (ACCENT, BG, DIM, FG, FONT_CN, FONT_MONO, FONT_UI, MUTED,
                    OK, PANEL, TEXT_BG, WARN)

HOME = "https://github.com/350886/leaf5-toolkit"

# 正文样式。spacing1 是段前、spacing3 是段后 —— 全部靠 tag 控制，
# 不在正文里插空行（插了以后按行号定位搜索会错位）。
TAGS = {
    "h": dict(font=(FONT_CN, 11, "bold"), foreground=ACCENT,
              spacing1=12, spacing3=4),
    "p": dict(font=(FONT_CN, 10), foreground=FG, spacing3=2),
    "li": dict(font=(FONT_CN, 10), foreground=FG, lmargin1=18, lmargin2=32,
               spacing3=1),
    "note": dict(font=(FONT_CN, 9), foreground=MUTED, lmargin1=34, lmargin2=34,
                 spacing1=2, spacing3=3),
    "warn": dict(font=(FONT_CN, 9), foreground=WARN, lmargin1=34, lmargin2=34,
                 spacing1=2, spacing3=3),
    "code": dict(font=(FONT_MONO, 9), foreground=OK, lmargin1=34, lmargin2=34,
                 spacing3=1),
    "hit": dict(background="#4a3b12", foreground="#ffd479"),
}


def render(text: tk.Text, body: str) -> list[int]:
    """把一段正文画进 Text，返回**每行在 Text 里的字符偏移**。

    偏移是搜索定位用的：`1.0 + N chars` 就能跳到某一行，比按行号数更可靠
    （Text 的逻辑行和源文件行不是一回事，长行会自动软换行）。
    """
    offsets: list[int] = []
    pos = 0
    for raw in body.splitlines():
        if raw.startswith("## "):
            line, tag = raw[3:], "h"
        elif raw.startswith("- "):
            line, tag = "• " + raw[2:], "li"
        elif raw.startswith("· "):
            line, tag = raw[2:], "li"
        elif raw.startswith("! "):
            line, tag = "⚠ " + raw[2:], "warn"
        elif raw.startswith("> "):
            line, tag = "› " + raw[2:], "note"
        elif raw.startswith("|"):
            line, tag = raw[1:], "code"
        else:
            line, tag = raw, "p"

        offsets.append(pos)
        text.insert("end", line + "\n", tag)
        pos += len(line) + 1
    return offsets


class HelpWindow(tk.Toplevel):
    def __init__(self, master, cfg=None) -> None:
        super().__init__(master)
        self.cfg = cfg
        self.title("使用说明 — l5tk %s" % __version__)
        self.configure(bg=BG)
        self.geometry("880x660")
        self.minsize(720, 460)
        try:
            self.transient(master)
        except Exception:
            pass

        self._offsets: list[int] = []
        self._hits: list[tuple[int, int]] = []
        self._hits_word = ""      # 搜索词变了就重算命中表，否则「下一处」会跳错
        self._hit_i = -1
        self._cur = 0

        head = tk.Frame(self, bg=PANEL)
        head.pack(fill="x")
        tk.Label(head, text="使用说明", bg=PANEL, fg=FG,
                 font=(FONT_CN, 12, "bold")).pack(side="left", padx=(14, 10), pady=8)
        tk.Label(head, text="F1 打开 · Ctrl+F 搜索 · Esc 关闭", bg=PANEL, fg=MUTED,
                 font=(FONT_UI, 9)).pack(side="left")

        self.var_q = tk.StringVar()
        ent = tk.Entry(head, textvariable=self.var_q, width=22, bg="#3a3d41",
                       fg=FG, insertbackground=FG, relief="flat",
                       font=(FONT_UI, 9))
        ent.pack(side="right", padx=(6, 14), pady=8)
        self.entry = ent
        ent.bind("<Return>", lambda _e: self.find_next(1))
        ent.bind("<Shift-Return>", lambda _e: self.find_next(-1))
        ent.bind("<Escape>", lambda _e: self._clear_find())
        tk.Label(head, text="搜索", bg=PANEL, fg=MUTED,
                 font=(FONT_UI, 9)).pack(side="right")
        self.lbl_find = tk.Label(head, text="", bg=PANEL, fg=DIM,
                                 font=(FONT_UI, 9))
        self.lbl_find.pack(side="right", padx=(0, 8))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(10, 0))

        left = tk.Frame(body, bg=BG, width=168)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        self.list = tk.Listbox(
            left, bg=PANEL, fg=FG, selectbackground=ACCENT, selectforeground="#fff",
            relief="flat", bd=0, highlightthickness=0, activestyle="none",
            font=(FONT_CN, 10), exportselection=False, width=16)
        self.list.pack(fill="both", expand=True)
        for name, _ in helpdoc.sections():
            self.list.insert("end", "  " + name)
        self.list.bind("<<ListboxSelect>>", self._on_pick)

        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.text = tk.Text(right, bg=TEXT_BG, fg=FG, insertbackground=FG,
                            relief="flat", wrap="word", bd=0,
                            padx=14, pady=10, spacing2=2, state="disabled",
                            cursor="arrow")
        sb = ttk.Scrollbar(right, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        for tag, spec in TAGS.items():
            self.text.tag_config(tag, **spec)

        foot = tk.Frame(self, bg=PANEL)
        foot.pack(fill="x", pady=(8, 0))
        tk.Button(foot, text="关闭", command=self.destroy, bg="#3a3d41", fg=FG,
                  activebackground="#4a4d51", activeforeground=FG, relief="flat",
                  bd=0, width=8, cursor="hand2",
                  font=(FONT_CN, 9)).pack(side="right", padx=(6, 14), pady=6)
        tk.Button(foot, text="导出为 txt", command=self.export, bg="#3a3d41", fg=FG,
                  activebackground="#4a4d51", activeforeground=FG, relief="flat",
                  bd=0, width=10, cursor="hand2",
                  font=(FONT_CN, 9)).pack(side="right", padx=6, pady=6)
        tk.Button(foot, text="打开项目主页", command=lambda: webbrowser.open(HOME),
                  bg="#3a3d41", fg=FG, activebackground="#4a4d51",
                  activeforeground=FG, relief="flat", bd=0, width=12,
                  cursor="hand2", font=(FONT_CN, 9)).pack(side="right", padx=6, pady=6)
        self.lbl_foot = tk.Label(foot, text="", bg=PANEL, fg=DIM,
                                 font=(FONT_UI, 9))
        self.lbl_foot.pack(side="left", padx=14)

        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Control-f>", lambda _e: self._focus_find())
        self.bind("<F1>", lambda _e: self.destroy())
        self.show(0)

    # ---------------- 章节 ----------------
    def show(self, idx: int) -> None:
        secs = helpdoc.sections()
        idx = max(0, min(idx, len(secs) - 1))
        self._cur = idx
        name, body = secs[idx]
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self._offsets = render(self.text, body)
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)
        self.list.selection_clear(0, "end")
        self.list.selection_set(idx)
        self.list.see(idx)
        self.lbl_foot.configure(text="第 %d/%d 节 · %s" % (idx + 1, len(secs), name))

    def _on_pick(self, _e=None) -> None:
        sel = self.list.curselection()
        if sel and sel[0] != self._cur:
            self.show(sel[0])

    # ---------------- 搜索 ----------------
    def _focus_find(self) -> str:
        self.entry.focus_set()
        self.entry.selection_range(0, "end")
        return "break"

    def _clear_find(self) -> str:
        self.text.tag_remove("hit", "1.0", "end")
        self.var_q.set("")
        self.lbl_find.configure(text="")
        self._hits, self._hit_i = [], -1
        return "break"

    def find_next(self, step: int = 1) -> None:
        word = self.var_q.get().strip()
        if not word:
            self._clear_find()
            return
        if not self._hits or self._hits_word != word:
            self._hits = helpdoc.search(word)
            self._hits_word = word
            self._hit_i = -1
        if not self._hits:
            self.lbl_find.configure(text="没有匹配")
            self.text.tag_remove("hit", "1.0", "end")
            return

        self._hit_i = (self._hit_i + step) % len(self._hits)
        sec, line_no = self._hits[self._hit_i]
        if sec != self._cur:
            self.show(sec)
        self._highlight(word, line_no)
        self.lbl_find.configure(
            text="第 %d/%d 处" % (self._hit_i + 1, len(self._hits)))

    def _highlight(self, word: str, line_no: int) -> None:
        self.text.tag_remove("hit", "1.0", "end")
        if line_no >= len(self._offsets):
            return
        start = "1.0 + %d chars" % self._offsets[line_no]
        pos = self.text.search(word, start, stopindex="end", nocase=True)
        if not pos:
            return
        end = "%s + %d chars" % (pos, len(word))
        self.text.tag_add("hit", pos, end)
        # 让命中行停在可视区中间偏上，而不是贴最上边 —— 上下文一起看见才有用
        self.text.see(pos)
        self.text.yview_scroll(-2, "units")

    # ---------------- 导出 ----------------
    def export(self) -> None:
        try:
            path = export_plain(self)
        except OSError as e:
            self.lbl_foot.configure(text="导出失败：%s" % e)
            return
        if path:
            self.lbl_foot.configure(text="已导出：%s" % path)


def export_plain(master) -> str | None:
    """把整份说明存成 txt。取消返回 None，写失败抛 OSError 由调用方决定怎么显示。

    菜单里也能直接调，不必先开说明窗口 —— 「导出」不依赖窗口存在。
    """
    path = filedialog.asksaveasfilename(
        parent=master, title="导出使用说明", defaultextension=".txt",
        initialfile="l5tk-使用说明.txt",
        filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
    if not path:
        return None
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(helpdoc.plain())
    return path


def open_help(master, cfg=None) -> HelpWindow:
    """同一个窗口只开一个：连按两次 F1 不该叠出两个。"""
    for w in master.winfo_children():
        if isinstance(w, HelpWindow) and w.winfo_exists():
            w.show(w._cur)
            w.lift()
            w.focus_set()
            return w
    return HelpWindow(master, cfg)


class AboutWindow(tk.Toplevel):
    def __init__(self, master, cfg=None) -> None:
        super().__init__(master)
        self.title("关于 l5tk")
        self.configure(bg=BG)
        self.resizable(False, False)
        try:
            self.transient(master)
        except Exception:
            pass

        pad = tk.Frame(self, bg=BG)
        pad.pack(fill="both", expand=True, padx=22, pady=18)
        tk.Label(pad, text="l5tk", bg=BG, fg=FG,
                 font=(FONT_UI, 18, "bold")).pack(anchor="w")
        tk.Label(pad, text="多机型 Android 工具箱  v%s" % __version__, bg=BG,
                 fg=MUTED, font=(FONT_UI, 10)).pack(anchor="w", pady=(0, 10))

        body = ("ONYX BOOX 设备的运维工具：设备画像 / Google Play / root / boot 刷入。\n"
                "V2 把 V1 的单文件 GUI 拆成四层，加机型 = 加一条 Profile。\n\n"
                "写 boot 有三条通路：dd（有 root）、fastbootd（无 root，走 userspace）、\n"
                "EDL/9008（兜底）。ONYX 的 abl 裁掉了 bootloader 的 flash 命令，\n"
                "所以 fastbootd 是没 root 时的主通路。")
        tk.Label(pad, text=body, bg=BG, fg=FG, justify="left",
                 font=(FONT_CN, 9)).pack(anchor="w")

        ttk.Separator(pad, orient="horizontal").pack(fill="x", pady=12)
        tk.Label(pad, text="日志：%s" % (getattr(cfg, "log_dir", "logs")), bg=BG,
                 fg=DIM, font=(FONT_UI, 8)).pack(anchor="w")
        tk.Label(pad, text="许可证：MIT", bg=BG, fg=DIM,
                 font=(FONT_UI, 8)).pack(anchor="w")

        bar = tk.Frame(pad, bg=BG)
        bar.pack(fill="x", pady=(14, 0))
        tk.Button(bar, text="打开项目主页", command=lambda: webbrowser.open(HOME),
                  bg="#3a3d41", fg=FG, activebackground="#4a4d51",
                  activeforeground=FG, relief="flat", bd=0, width=13,
                  cursor="hand2", font=(FONT_CN, 9)).pack(side="left")
        tk.Button(bar, text="关闭", command=self.destroy, bg=ACCENT, fg="#fff",
                  activebackground="#3d82e0", activeforeground="#fff", relief="flat",
                  bd=0, width=8, cursor="hand2",
                  font=(FONT_CN, 9)).pack(side="right")
        self.bind("<Escape>", lambda _e: self.destroy())


def open_about(master, cfg=None) -> None:
    AboutWindow(master, cfg).focus_set()
