"""GUI 冒烟：「② 装 Google Play」按钮下面那行前置条件说明。

pytest 收不到（要真起 Tk），手动跑：

    "C:/Users/<用户>/AppData/Local/Programs/Python/Python312/python.exe" \
        tests/smoke_ui_gplay_hint.py

验四件事：
  1. 那行说明存在，文案与约定一致
  2. 位置在「② 装 Google Play」按钮之后、「③ 检查 Root」之前（不是挂到列表末尾）
  3. 它是 Label 不是 Button，不会进 btns 列表（否则 _set_busy 会去 configure 它）
  4. gp_ui_enabled() 关掉时不出现（按钮都没有，说明文字也不该挂在那儿）

不连设备：act_probe 在类上换成空函数（__init__ 注册的 300ms 自动 probe
取的是绑定方法引用，改实例已经晚了）。
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import l5tk.ui.app as ui                    # noqa: E402

# 文案会随排版调整，判据只锁「必须提到 Google 账号 + 验证通过」这两件事，
# 不锁具体措辞 —— 否则每次改文案都要改测试，等于测试在管产品文案。
HINT_HEAD = "Google"
HINT_TAIL = "验证"


def collect(app) -> list[tuple[str, str]]:
    """按 pack 顺序列出左栏控件：(类名, 文案)。"""
    left = app.chk_module.master          # 勾选框与按钮同属左栏 Frame
    out = []
    for w in left.pack_slaves():
        try:
            text = w.cget("text")
        except tk.TclError:
            text = ""
        out.append((w.winfo_class(), str(text)))
    return out


def find_hint(rows: list[tuple[str, str]]) -> dict:
    """定位说明 Label 与它前后两个按钮的下标。"""
    idx = {"hint": -1, "gplay": -1, "root": -1}
    for i, (cls, text) in enumerate(rows):
        if cls == "Label" and HINT_HEAD in text and HINT_TAIL in text:
            idx["hint"] = i
        elif cls == "TButton" and "装 Google Play" in text:
            idx["gplay"] = i
        elif cls == "TButton" and "检查 Root" in text:
            idx["root"] = i
    return idx


def run_case(gp_on: bool):
    """建一个真窗口，取完控件顺序就销毁。返回 (开关值, 控件顺序, 下标)。"""
    ui.gp_ui_enabled = lambda: gp_on     # type: ignore[assignment]
    ui.App.act_probe = lambda self: None  # type: ignore[method-assign]

    app = ui.App()
    app.update_idletasks()
    rows = collect(app)
    idx = find_hint(rows)
    app.destroy()
    return gp_on, rows, idx


def main() -> int:
    ok = True

    # ---- 1. 有 Google Play 按钮时：说明必须在它下面、检查 Root 上面 ----
    _, rows, idx = run_case(True)
    print("[1] 左栏控件顺序：")
    for i, (cls, text) in enumerate(rows):
        mark = " <-- 说明" if i == idx["hint"] else ""
        print("    %2d  %-9s %r%s" % (i, cls, text, mark))

    if idx["hint"] < 0:
        print("[x] 没找到说明 Label")
        return 1
    print("[2] 说明文案  : %r" % rows[idx["hint"]][1])
    text_ok = (HINT_HEAD in rows[idx["hint"]][1]
               and HINT_TAIL in rows[idx["hint"]][1])
    print("[2] 文案符合  : %s" % ("是" if text_ok else "否"))

    if idx["gplay"] < 0 or idx["root"] < 0:
        print("[x] 没找到前后按钮（gp=%d root=%d）" % (idx["gplay"], idx["root"]))
        return 1
    pos_ok = idx["gplay"] < idx["hint"] < idx["root"]
    print("[3] 位置正确  : %s（按钮 %d < 说明 %d < 检查Root %d）"
          % ("是" if pos_ok else "否", idx["gplay"], idx["hint"], idx["root"]))

    # ---- 2. 说明是 Label，不能混进 btns ----
    app = ui.App()
    app.update_idletasks()
    n_btns = len(app.btns)
    n_tbuttons = sum(1 for cls, _ in collect(app) if cls == "TButton")
    app.destroy()
    btn_ok = n_btns == n_tbuttons
    print("[4] btns 里全是 TButton : %s（btns=%d，界面上 TButton=%d）"
          % ("是" if btn_ok else "否", n_btns, n_tbuttons))

    # ---- 3. gp_ui_enabled() 关掉时不出现 ----
    _, rows2, idx2 = run_case(False)
    gone = idx2["gplay"] < 0 and idx2["hint"] < 0
    print("[5] 关掉 GPlay 入口后  : 按钮与说明都不在 = %s" % ("是" if gone else "否"))
    if not gone:
        print("    [x] 残留：gplay=%d hint=%d" % (idx2["gplay"], idx2["hint"]))

    ok = text_ok and pos_ok and btn_ok and gone
    print("\n%s" % ("全部通过：说明在按钮下面、文案对、没混进 btns、关掉入口时一起消失"
                    if ok else "[x] 有断言没过"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
