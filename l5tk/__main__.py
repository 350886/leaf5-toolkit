"""python -m l5tk 的入口：有参数走 CLI，没参数开窗口。"""

from __future__ import annotations

import os
import sys


def attach_console() -> None:
    """把 --windowed 打包的进程接回父控制台。

    没有这一步，从 cmd/PowerShell 跑 `Leaf5Toolkit.exe root edl` 是**一句话都
    看不到**的：进程由 pythonw 派生，stdout/stderr 是空的，print 直接丢弃。
    最容易踩的是 EDL：命令跑完了、设备也刷了，屏幕上什么都没有，看不出成败。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        if k.AttachConsole(-1):
            # 控制台默认 GBK 代码页，直接写 UTF-8 会变乱码
            k.SetConsoleOutputCP(65001)
            k.SetConsoleCP(65001)
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1,
                              errors="replace")
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1,
                              errors="replace")
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    if len(sys.argv) > 1:
        attach_console()
        from .cli import main as cli_main
        return cli_main()
    try:
        from .ui.app import run
        return run()
    except Exception as e:  # tkinter 缺失或没有显示环境
        print("窗口起不来（%s），改用命令行：python -m l5tk probe" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
