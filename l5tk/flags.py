"""构建变体的判据。

双版本只差一件事：界面里有没有「装 Google Play」这一行。
判据用包里一个空标记文件 `assets/gp_ui.on`，**不看包里有没有 APK** ——
用户把 APK 丢进同目录 / apk\\ 是常规操作，用「有没有 APK」判断会让
普通版在用户放包之后突然长出按钮。

源码运行恒为 True：开发时要能看到完整界面。
"""

from __future__ import annotations

import os
import sys


def gp_ui_enabled() -> bool:
    if not getattr(sys, "frozen", False):
        return True
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
    return os.path.isfile(os.path.join(base, "assets", "gp_ui.on"))
