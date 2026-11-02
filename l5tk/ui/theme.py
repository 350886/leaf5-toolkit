"""配色与字体。

单独拎出来是因为现在有两个窗口（主窗口、使用说明、适配向导）要用同一套值，
散在 app.py 里的话，新窗口要么重复写一遍、要么反向 import app（会把整条
ops 依赖链拖进来）。
"""

from __future__ import annotations

# 深色配浅色字，和 V1 一致，避免用户两台工具切来切去不适应
BG = "#1e1f22"
PANEL = "#2b2d30"
FG = "#d7d9dd"
MUTED = "#8b8f96"
# 比 MUTED 再暗一档，专门给「用在什么情况」那类条件说明用：
# 两行说明如果同样亮度，会糊成一整段，用户分不出哪句是使用条件。
DIM = "#7a7f86"
ACCENT = "#4c9aff"
OK = "#4ec9a0"
WARN = "#e2b341"
ERR = "#ef6b6b"
# 说明窗口的正文底色，比 BG 再深一点，和右栏日志保持同一层视觉
TEXT_BG = "#15161a"

FONT_UI = "Segoe UI"
FONT_CN = "Microsoft YaHei UI"     # 中文用雅黑：Segoe UI 的中文回退字重偏细
FONT_MONO = "Consolas"
