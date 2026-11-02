"""PyInstaller 的入口脚本。

一个 EXE 两种用法：
  * 双击 / 不带参数  → 开窗口
  * 带参数            → 走 CLI

窗口化（--windowed）的 EXE 没有控制台，`print` 出去的东西用户看不见，
所以带参数跑 CLI 时把输出抓进缓冲，结束后弹一个结果框，同时落一份到 logs/cli-last.txt。
源码运行时直接用真 stdout，不弹框。
"""

from __future__ import annotations

import io
import os
import sys


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _run_cli(argv: list[str]) -> int:
    from l5tk.cli import main as cli_main

    if not _frozen():
        return cli_main(argv)

    buf = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = buf
    try:
        rc = cli_main(argv)
    except SystemExit as e:      # --version / --help 走的是这条路
        rc = e.code if isinstance(e.code, int) else 1
    finally:
        sys.stdout, sys.stderr = old_out, old_err

    text = buf.getvalue().strip() or "(无输出)"
    try:
        from l5tk.config import load as load_cfg
        cfg = load_cfg()
        cfg.ensure_dirs()
        (cfg.log_dir / "cli-last.txt").write_text(text, encoding="utf-8")
    except Exception:
        pass

    # 自动化/脚本调用时不该弹窗挡路：结果已经在 cli-last.txt 里
    if os.environ.get("L5TK_CLI_QUIET") == "1":
        return rc

    try:
        import tkinter as tk
        from tkinter import messagebox, scrolledtext

        root = tk.Tk()
        root.withdraw()
        win = tk.Toplevel(root)
        win.title("l5tk 输出  (rc=%d)" % rc)
        win.geometry("780x460")
        box = scrolledtext.ScrolledText(win, font=("Consolas", 9), wrap="word")
        box.pack(fill="both", expand=True, padx=8, pady=8)
        box.insert("1.0", text)
        box.configure(state="disabled")
        tk.Button(win, text="关闭", command=root.destroy).pack(pady=(0, 8))
        root.mainloop()
    except Exception as e:
        # 连 tk 都起不来时至少别静默退出
        print(text, file=old_err or sys.stderr)
        print("(弹窗失败：%s)" % e, file=old_err or sys.stderr)
    return rc


def main() -> int:
    if len(sys.argv) > 1:
        return _run_cli(sys.argv[1:])
    from l5tk.ui.app import run
    return run()


if __name__ == "__main__":
    sys.exit(main())
