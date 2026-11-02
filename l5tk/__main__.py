"""python -m l5tk 的入口：有参数走 CLI，没参数开窗口。"""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1:
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
