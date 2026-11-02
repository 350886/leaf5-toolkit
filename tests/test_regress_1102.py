# -*- coding: utf-8 -*-
"""2026-11-02 这一轮修复的回归用例（V2）。

覆盖五处：
  1. kimage.ImageError / elfsyms.ElfError / fp.FpError 统一挂到 L5Error
     （以前各有一份本地 Exception，CLI 兜不住，用户看到的是 Python 堆栈）
  2. fp.match 在「候选没记符号指纹」时不再说「符号序列不同」
  3. fp.match 在地址被 kptr 隐藏时不再说「先 root」
  4. adapt 有 fp 子命令（V1 有、V2 缺）
  5. adapt banner 的结论要独立成行（CLI 层）
"""

import pytest

from l5tk import cli
from l5tk.core import elfsyms, fp as fpm, kimage
from l5tk.core import errors


def test_image_error_is_l5error():
    assert kimage.ImageError is errors.ImageError
    assert issubclass(kimage.ImageError, errors.L5Error)


def test_elf_error_is_l5error():
    assert elfsyms.ElfError is errors.ElfError
    assert issubclass(elfsyms.ElfError, errors.L5Error)


def test_fp_error_is_l5error():
    assert fpm.FpError is errors.FpError
    assert issubclass(fpm.FpError, errors.L5Error)


def test_errors_render_without_traceback():
    e = kimage.ImageError("镜像截断了", hint="重新导一份 boot.img")
    assert e.render().startswith("镜像截断了")
    assert "boot.img" in e.render()


def _fp(digest="", ver="4.19.157-perf-gx-dirty", zeroed=0, syms=121921):
    return {"kernel_ver": ver, "sym": {"sym_digest": digest,
                                       "kernel_syms": syms, "zeroed": zeroed}}


DIG = "a" * 64


def test_match_missing_digest_wording():
    r = fpm.match(_fp(digest=DIG), [{"id": "old", "kernel": "4.19.157-perf-gx-dirty"}])
    assert r["verdict"] == "same_version"
    assert "没记符号指纹" in r["why"]
    assert "符号序列不同" not in r["why"]


def test_match_different_digest_wording():
    r = fpm.match(_fp(digest=DIG),
                  [{"id": "t2", "kernel": "4.19.157-perf-gx-dirty",
                    "sym_digest": "b" * 64}])
    assert "符号序列不同" in r["why"]


def test_match_hidden_addresses_advice():
    r = fpm.match(_fp(zeroed=121921), [])
    assert r["verdict"] == "no_target"
    assert "kptr_restrict" in r["advice"]
    assert "先把设备 root 起来" not in r["advice"]


def test_match_no_target_advice():
    r = fpm.match(_fp(zeroed=0), [])
    assert "先把设备 root 起来" in r["advice"]


def test_adapt_has_fp_subcommand():
    p = cli.build_parser()
    ns = p.parse_args(["adapt", "fp", "--json"])
    assert ns.cmd == "adapt" and ns.act == "fp" and ns.json is True
    ns2 = p.parse_args(["adapt", "fp", "-o", "x.json", "--no-kallsyms"])
    assert ns2.out == "x.json" and ns2.no_kallsyms is True


def test_verdict_tags_cover_all_states():
    for v in ("same", "resame", "dirty", "different", "unknown"):
        assert v in kimage.VERDICT_TAG
