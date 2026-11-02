"""菜单栏与使用说明窗口的冒烟测试。

不进 pytest 主套件：要真起 Tk（主套件是纯逻辑的，不起进程、连设备都不连）。

    "C:/Users/thelu/AppData/Local/Programs/Python/Python312/python.exe" ^
        E:\\BOOX\\leaf5-toolkit\\tests\\smoke_ui_menu.py

验收点：三个顶级菜单都在、每项都接到了真函数、F1 开得出说明、搜索能翻、
连按两次 F1 不叠窗、清空日志只清界面。
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


def walk(menu, depth=0):
    """展开菜单树，返回 [(缩进, 文案, 是否有命令)]。"""
    out = []
    end = menu.index("end")
    if end is None:
        return out
    for i in range(end + 1):
        if menu.type(i) == "separator":
            out.append((depth, "——", True))
            continue
        label = menu.entrycget(i, "label")
        if menu.type(i) == "cascade":
            out.append((depth, label, True))
            out += walk(menu.nametowidget(menu.entrycget(i, "menu")), depth + 1)
        else:
            cmd = ""
            try:
                cmd = menu.entrycget(i, "command")
            except tk.TclError:
                pass
            out.append((depth, label, bool(cmd)))
    return out


def main():
    import l5tk.ui.app as A
    from l5tk.ui import helpdoc
    from l5tk.ui.helpwin import HelpWindow

    A.gp_ui_enabled = lambda: True
    A.App.act_probe = lambda self: None          # 别把真 adb 叫起来

    app = A.App()
    app.geometry("920x800+2400+200")             # 摆到屏幕外，不闪窗
    app.update_idletasks()

    # ---- 1. 菜单栏 ----
    bar = app.menubar
    tops = []
    for i in range(bar.index("end") + 1):
        tops.append(bar.entrycget(i, "label"))
    chk(tops == ["文件", "工具", "帮助"], "三个顶级菜单：%s" % tops)

    rows = walk(bar)
    items = [(d, t, c) for d, t, c in rows if d > 0]
    chk(len(items) >= 10, "菜单项数量 %d" % len(items))
    dead = [t for _d, t, c in items if not c and t != "——"]
    chk(not dead, "没有空壳菜单项（%s）" % (dead or "无"))

    # 关键项必须在
    labels = [t for _d, t, _c in items]
    for need in ("打开日志目录", "导出使用说明…", "退出", "重新检测设备",
                 "清空日志窗口", "使用说明", "关于"):
        chk(need in labels, "菜单里有「%s」" % need)

    # ---- 2. 清空日志 ----
    app.emit("测试行 1")
    app.emit("测试行 2")
    app.update()
    before = app.text.get("1.0", "end").strip()
    app.act_clear_log()
    app.update()
    after = app.text.get("1.0", "end").strip()
    chk(bool(before) and after == "", "清空日志：%r → %r" % (before, after))

    # ---- 3. F1 开说明 ----
    app.act_help()
    app.update()
    wins = [w for w in app.winfo_children() if isinstance(w, HelpWindow)]
    chk(len(wins) == 1, "F1 打开了一个说明窗口")
    hw = wins[0]
    hw.geometry("880x660+2400+200")
    app.update()

    chk(hw.list.size() == len(helpdoc.sections()),
        "左栏章节数 %d == 文档节数 %d" % (hw.list.size(), len(helpdoc.sections())))
    first = hw.text.get("1.0", "end").strip()
    chk(len(first) > 200, "第一节渲染出 %d 字" % len(first))
    chk("界面总览" in first or "三块区域" in first, "第一节是「界面总览」")

    # ---- 4. 换节 ----
    hw.show(3)
    app.update()
    chk(hw.text.get("1.0", "end").strip() != first, "切到第 4 节，正文变了")
    chk(hw.lbl_foot.cget("text").startswith("第 4/"), "底部显示节号：%s"
        % hw.lbl_foot.cget("text"))

    # ---- 5. 搜索 ----
    hw.var_q.set("fastbootd")
    hw.find_next(1)
    app.update()
    chk("处" in hw.lbl_find.cget("text"), "搜索有结果：%s" % hw.lbl_find.cget("text"))
    chk(bool(hw.text.tag_ranges("hit")), "命中处有高亮")
    before_idx = hw._hit_i
    hw.find_next(1)
    chk(hw._hit_i != before_idx, "回车翻到下一处")

    hw.var_q.set("zzz-绝不存在")
    hw.find_next(1)
    chk(hw.lbl_find.cget("text") == "没有匹配", "无结果时明确说没有匹配")

    # ---- 6. 不叠窗 ----
    app.act_help()
    app.update()
    again = [w for w in app.winfo_children() if isinstance(w, HelpWindow)]
    chk(len(again) == 1, "再按一次 F1 没叠出第二个窗口")

    # ---- 7. 关于 ----
    app.act_about()
    app.update()
    abouts = [w for w in app.winfo_children()
              if isinstance(w, tk.Toplevel) and w.title() == "关于 l5tk"]
    chk(len(abouts) == 1, "关于窗口能打开")
    for w in abouts:
        w.destroy()

    # ---- 8. 导出（不弹对话框，直接调底层写文件）----
    import tempfile
    from l5tk.ui import helpdoc as hd
    tmp = os.path.join(tempfile.gettempdir(), "l5tk_help_smoke.txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(hd.plain())
    size = os.path.getsize(tmp)
    chk(size > 3000, "导出文本 %d 字节" % size)
    os.remove(tmp)

    hw.destroy()
    app.update()
    app.destroy()

    print("")
    if FAIL:
        print("失败 %d 项：" % len(FAIL))
        for m in FAIL:
            print("  - " + m)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
