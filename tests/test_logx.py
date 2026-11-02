"""日志脱敏。这里错一次，就是把设备串和账号写进了要发给别人的日志。"""

from __future__ import annotations

from pathlib import Path

from l5tk import logx
from l5tk.logx import Logger, mask, register_secret, secrets, tail


def setup_function():
    logx._secrets.clear()


def test_registered_secret_masked():
    register_secret("THELUCKBLUE01")
    out = mask("设备 THELUCKBLUE01 已连接")
    assert "THELUCKBLUE01" not in out
    assert out.startswith("设备 THE")


def test_short_secret_ignored():
    register_secret("ab")
    assert secrets() == ()


def test_email_masked():
    out = mask("账号 theluck.blue@gmail.com 登录成功")
    assert "theluck.blue@gmail.com" not in out
    assert "@gmail.com" in out


def test_private_ip_masked():
    out = mask("adb connect 192.168.31.77:5555")
    assert "192.168.31.77" not in out
    assert ":5555" in out


def test_windows_user_path_masked():
    out = mask(r"日志写在 C:\Users\thelu\AppData\Local\l5tk")
    assert "thelu" not in out
    assert "C:\\Users\\***" in out


def test_public_ip_untouched():
    assert "8.8.8.8" in mask("dns 8.8.8.8")


def test_plain_mode_bypasses(tmp_path, monkeypatch):
    monkeypatch.setenv("L5TK_LOG_PLAIN", "1")
    register_secret("SECRETSERIAL01")
    lg = Logger(tmp_path, echo=False)
    lg.info("串 SECRETSERIAL01")
    lg.close()
    body = tail(lg.path, 5)[0]
    assert "SECRETSERIAL01" in body


def test_file_gets_masked(tmp_path):
    logx._secrets.clear()
    register_secret("SERIALNUMBER9")
    lg = Logger(tmp_path, echo=False)
    lg.info("串 SERIALNUMBER9")
    lg.close()
    body = Path(lg.path).read_text(encoding="utf-8")
    assert "SERIALNUMBER9" not in body


def test_level_filter(tmp_path):
    lg = Logger(tmp_path, echo=False, min_level="warn")
    lg.dbg("调试不该出现")
    lg.info("信息不该出现")
    lg.warn("警告要出现")
    lg.close()
    body = Path(lg.path).read_text(encoding="utf-8")
    assert "警告要出现" in body
    assert "信息不该出现" not in body


def test_tail_reads_last_lines(tmp_path):
    lg = Logger(tmp_path, echo=False)
    for i in range(30):
        lg.info("line %d" % i)
    lg.close()
    last = tail(lg.path, 5)
    assert len(last) == 5 and "line 29" in last[-1]
