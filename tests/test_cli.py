"""CLI 契约：退出码与子命令输出。命令行行为一旦变了，脚本就会静默失败。"""

from __future__ import annotations

import json

from l5tk.cli import RC_FAIL, RC_OK, RC_USAGE, build_parser, main
from tests.test_root import make_boot


def test_parser_has_all_commands():
    p = build_parser()
    actions = [a for a in p._actions if a.dest == "cmd"]
    assert actions
    assert set(actions[0].choices) == {
        "profiles", "devices", "doctor", "probe", "root", "gplay", "verify"}


def test_profiles(capsys):
    assert main(["profiles"]) == RC_OK
    out = capsys.readouterr().out
    for key in ("leaf5", "tab8c", "tab8", "tab10"):
        assert key in out


def test_root_check_ok(tmp_path, capsys):
    p = make_boot(tmp_path / "ok.img")
    assert main(["root", "check", str(p)]) == RC_OK
    assert "合法的 boot 镜像" in capsys.readouterr().out


def test_root_check_bad(tmp_path, capsys):
    p = tmp_path / "bad.img"
    p.write_bytes(b"nope" * 100)
    assert main(["root", "check", str(p)]) == RC_FAIL
    captured = capsys.readouterr()
    assert "ANDROID" in captured.out + captured.err


def test_root_check_without_image(capsys):
    assert main(["root", "check"]) == RC_USAGE
    assert "需要给镜像路径" in capsys.readouterr().out


def test_dry_run_refuses_flash(tmp_path, capsys):
    p = make_boot(tmp_path / "ok.img")
    # 没设备 → adb 层报错，退出码 2；有设备才轮到 dry-run 的 3
    rc = main(["--dry-run", "root", "flash", str(p)])
    assert rc in (2, 3)


def test_version_flag(capsys):
    import pytest
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "l5tk" in capsys.readouterr().out
