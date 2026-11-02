"""GUI 冒烟：一键 Root 旁边的「顺带装 AMS 修复模块」勾选框。

pytest 收不到（文件名不以 test_ 开头），因为要真起一个 Tk 窗口、真跑一遍
按钮回调 —— 这类验证适合手动跑一次，不适合放进 CI：

    python tests/smoke_ui_module.py

验三件事：
  1. 控件存在，且默认勾上（等于 V1「勾了 Magisk 就自动勾上模块」的结果）
  2. 勾着时，AutoRoot.run 收到 with_module=True
  3. 取消勾选后，收到 with_module=False

必须真进 mainloop：Tk 在非主循环线程里调 after() 会抛
`RuntimeError: main thread is not in main loop`，而界面的日志输出正是
子线程 → after() → 主线程这条路。窗口会闪一下，跑完自己 destroy。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import l5tk.ops.autoroot as ar           # noqa: E402
from l5tk.ui.app import App              # noqa: E402

seen: dict = {}
results: dict = {}


class _FakeResult:
    ok = True
    detail = "假结果（未连设备）"
    steps: list = []


def _fake_run(self, **kw):              # noqa: ANN001
    seen.clear()
    seen.update(kw)
    return _FakeResult()


def main() -> int:
    ar.AutoRoot.run = _fake_run
    # 冒烟不连设备：__init__ 里注册的 300ms 自动 probe 会调 adb，
    # 在类上换掉（注册时就取了绑定方法的引用，实例上改已经晚了）
    App.act_probe = lambda self: None    # noqa: E731

    app = App()
    app.update_idletasks()

    if getattr(app, "chk_module", None) is None:
        print("[x] 界面上没有模块勾选框")
        return 1
    print("[1] 控件文案        : %r" % app.chk_module.cget("text"))
    print("[1] 默认值          : %s" % ("勾上" if app.var_module.get() else "没勾"))
    results["default"] = app.var_module.get()

    def phase1() -> None:
        app.act_autoroot()
        app.after(400, phase1_check)

    def phase1_check() -> None:
        results["on"] = seen.get("with_module")
        app._busy = False                # 上一轮跑完，放行下一次
        app.var_module.set(False)
        app.act_autoroot()
        app.after(400, phase2_check)

    def phase2_check() -> None:
        results["off"] = seen.get("with_module")
        app.quit()

    app.after(150, phase1)
    app.mainloop()
    app.destroy()

    print("[2] 勾着时          : with_module=%s" % results.get("on"))
    print("[3] 取消勾选后      : with_module=%s" % results.get("off"))

    ok = (results.get("default") is True
          and results.get("on") is True
          and results.get("off") is False)
    print("\n%s" % ("全部通过：控件在、默认勾、状态传得到 AutoRoot.run"
                    if ok else "[x] 有断言没过"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
