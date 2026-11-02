"""日志：单一出口，脱敏在这里做，调用点不管。

规矩来自被踩过的坑：
  * 脱敏必须集中在一个函数里过一遍，逐个调用点改必漏
  * 必须留逃生阀 —— 打包后没控制台，日志是唯一排障窗口
  * 账号名走动态词表（启动时预载 + 运行中登记），不靠正则猜
"""

from __future__ import annotations

import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable

# ---- 动态敏感词表 ------------------------------------------------------
_secrets: set[str] = set()
_lock = threading.Lock()

# serial 形态：8~20 位大写字母数字，或者带冒号的 IP:port
_SERIAL_RE = re.compile(r"\b([A-Z0-9]{8,20})\b")
_IP_RE = re.compile(r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
                    r"192\.168\.\d{1,3}\.\d{1,3}|"
                    r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")
_MAIL_RE = re.compile(r"\b([A-Za-z0-9._%+\-]{1,64})@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
_USER_PATH_RE = re.compile(r"([A-Za-z]:\\Users\\)([^\\\s]+)", re.I)


def register_secret(*values: str) -> None:
    """运行中登记要打码的串（设备 serial、登录账号……）。空值忽略。"""
    with _lock:
        for v in values:
            v = (v or "").strip()
            if len(v) >= 4:
                _secrets.add(v)


def secrets() -> tuple[str, ...]:
    with _lock:
        return tuple(sorted(_secrets, key=len, reverse=True))


def _keep(head: int = 3, tail: int = 3) -> str:
    def f(m: re.Match) -> str:
        s = m.group(1)
        if len(s) <= head + tail:
            return "*" * len(s)
        return s[:head] + "*" * (len(s) - head - tail) + s[-tail:]
    return f


def mask(text: str) -> str:
    """把已知敏感串打码。词表先用精确替换，再用形态兜底。"""
    if not text:
        return text
    out = text
    for s in secrets():
        out = out.replace(s, s[:3] + "*" * max(3, len(s) - 3))
    out = _MAIL_RE.sub(lambda m: m.group(1)[:2] + "***@" + m.group(2), out)
    out = _IP_RE.sub(_keep(3, 2), out)
    out = _USER_PATH_RE.sub(lambda m: m.group(1) + "***", out)
    # 只对"看起来像 serial"的独立 token 动手，避免把路径/版本号打烂
    out = _SERIAL_RE.sub(lambda m: m.group(0) if len(m.group(0)) < 10
                         else m.group(0)[:3] + "*" * (len(m.group(0)) - 6)
                         + m.group(0)[-3:], out)
    return out


LEVELS = {"dbg": 10, "ok": 20, "info": 30, "warn": 40, "err": 50}


class Logger:
    """同时写 stdout 与按天切分的日志文件。"""

    def __init__(self, log_dir: Path | None = None, plain: bool = False,
                 min_level: str = "info", echo: bool = True):
        self.plain = plain or os.environ.get("L5TK_LOG_PLAIN") == "1"
        self.min_level = LEVELS.get(min_level, 30)
        self.echo = echo
        self._fh = None
        self._path: Path | None = None
        if log_dir is not None:
            log_dir = Path(log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            self._path = log_dir / ("l5tk-%s.log" % datetime.now().strftime("%Y%m%d"))
            # 追加模式：一次运行多次调用都留痕
            self._fh = open(self._path, "a", encoding="utf-8", newline="")

    # --- 生命周期 ---
    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "Logger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def path(self) -> Path | None:
        return self._path

    # --- 写 ---
    def _emit(self, level: str, msg: str) -> None:
        if LEVELS[level] < self.min_level:
            return
        body = msg if self.plain else mask(msg)
        line = "%s [%s] %s" % (
            datetime.now().strftime("%H:%M:%S"), level.upper().ljust(4), body)
        if self.echo:
            stream = sys.stderr if level in ("warn", "err") else sys.stdout
            try:
                print(line, file=stream, flush=True)
            except (UnicodeEncodeError, OSError):
                # 某些 Windows 控制台是 GBK，打不出 "✅" 这类符号时降级
                print(line.encode("ascii", "replace").decode(), file=stream, flush=True)
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()

    def dbg(self, msg: str) -> None: self._emit("dbg", msg)
    def ok(self, msg: str) -> None: self._emit("ok", msg)
    def info(self, msg: str) -> None: self._emit("info", msg)
    def warn(self, msg: str) -> None: self._emit("warn", msg)
    def err(self, msg: str) -> None: self._emit("err", msg)

    def step(self, n: int, total: int, msg: str) -> None:
        self._emit("info", "[%d/%d] %s" % (n, total, msg))


def null_logger() -> Logger:
    """测试用：不写盘、不回声。"""
    return Logger(log_dir=None, plain=True, echo=False)


def tail(path: Path, lines: int = 80) -> list[str]:
    """读日志尾部，GUI 的「查看日志」用。"""
    p = Path(path)
    if not p.is_file():
        return []
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        buf = f.readlines()
    return [l.rstrip("\n") for l in buf[-lines:]]
