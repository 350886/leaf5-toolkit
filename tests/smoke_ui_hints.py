"""左栏每个按钮下面必须有两行：做什么 + 用在什么情况。

不放进 pytest 主套件，因为它要真起一个 Tk 窗口（主套件是纯逻辑的，
跑起来不起进程、不连设备）。和 smoke_ui_module.py 同类。

    "C:/Users/thelu/AppData/Local/Programs/Python/Python312/python.exe" ^
        E:\\BOOX\\leaf5-toolkit\\tests\\smoke_ui_hints.py
"""

import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL = []


def chk(ok, msg):
    print("  [%s] %s" % ("ok" if ok else "XX", msg))
    if not ok:
        FAIL.append(msg)


def main():
    import l5tk.ui.app as A

    # 集成版才画 Google Play 那一组，验的时候全都要在
    A.gp_ui_enabled = lambda: True
    # 实例化后会挂 300ms 自动 probe，会把真 adb 叫起来
    A.App.act_probe = lambda self: None

    app = A.App()
    app.geometry("920x800+2400+200")     # 摆到屏幕外，不闪窗
    app.update_idletasks()

    left = None
    for w in app.winfo_children():
        pass
    # 左栏是 body 的第一个子控件；按 pack 顺序取，不靠名字
    def find_left(w):
        for c in w.winfo_children():
            if isinstance(c, tk.Frame) and c.winfo_width() in (220,) :
                return c
            r = find_left(c)
            if r is not None:
                return r
        return None

    left = find_left(app)
    if left is None:
        # 宽度还没算出来时兜底：找挂了最多 Button 的那个 Frame
        best, n = None, 0
        def scan(w):
            nonlocal best, n
            for c in w.winfo_children():
                if isinstance(c, tk.Frame):
                    k = sum(1 for x in c.winfo_children()
                            if isinstance(x, tk.ttk.Button))
                    if k > n:
                        best, n = c, k
                    scan(c)
        scan(app)
        left = best
    assert left is not None, "找不到左栏容器"

    kids = left.pack_slaves()
    print("[1] 左栏子控件 %d 个" % len(kids))

    btns = [w for w in kids if isinstance(w, tk.ttk.Button)]
    print("[2] 按钮 %d 个，每个后面必须跟「做什么」+「用在·」两行" % len(btns))
    for b in btns:
        i = kids.index(b)
        tail = kids[i + 1:i + 3]
        texts = [str(x.cget("text")) for x in tail if isinstance(x, tk.Label)]
        has_hint = bool(texts) and not texts[0].startswith("用在·")
        has_when = any(t.startswith("用在·") for t in texts)
        chk(has_hint and has_when,
            "「%s」→ 做什么=%r 用在=%r"
            % (b.cget("text"), texts[0] if texts else None,
               next((t for t in texts if t.startswith("用在·")), None)))

    print("[3] 选项勾选框也必须带「用在·」")
    chks = [w for w in kids if isinstance(w, tk.Checkbutton)]
    for c in chks:
        i = kids.index(c)
        tail = kids[i + 1:i + 3]
        has_when = any(str(x.cget("text")).startswith("用在·")
                       for x in tail if isinstance(x, tk.Label))
        chk(has_when, "「%s」有使用条件" % c.cget("text"))

    print("[4] 文案不是空壳")
    whens = [str(w.cget("text")) for w in kids
             if isinstance(w, tk.Label) and str(w.cget("text")).startswith("用在·")]
    chk(len(whens) >= len(btns) + len(chks) - 1,
        "条件行 %d 条，覆盖 %d 个按钮 + %d 个选项" % (len(whens), len(btns), len(chks)))
    chk(all(len(t) > len("用在·") + 2 for t in whens), "没有空的「用在」行")

    print("[5] 左栏内容没被窗口底部切掉")
    # 窗口摆到屏幕外时不进主循环，winfo_rooty() 全是 0，量不了绝对位置；
    # 改比需求高度：左栏所有子控件的 reqheight 累加 vs 窗口高。
    # 没进主循环时 Frame 自己的 reqheight 是 1，只能按子控件累加，
    # 每个再加 8px 估它的上下间距（按钮 4+说明 2+2）。
    need = sum(k.winfo_reqheight() for k in kids)
    chk(need <= app.winfo_height(),
        "左栏内容需 %d px ≤ 窗口高 %d" % (need, app.winfo_height()))

    app.destroy()
    print()
    if FAIL:
        print("通过 %d / 失败 %d" % (0, len(FAIL)))
        for f in FAIL:
            print("  XX", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
