"""ainsn：从指令里定标手工宏。用真实编码的指令字做回归。"""

import struct

import pytest

from l5tk.core import ainsn

SP = 31
SPAN = 0x4000


# ---------------- 编码助手（与 AArch64 手册一致） ----------------
def stp_pre(rt, rt2, rn, imm):
    """STP Xt, Xt2, [Xn, #imm]!  —— imm 必须是负的 8 的倍数"""
    assert imm < 0 and imm % 8 == 0
    imm7 = (imm // 8) & 0x7F
    return 0xA9800000 | (imm7 << 15) | (rt2 << 10) | (rn << 5) | rt


def stp_off(rt, rt2, rn, imm):
    imm7 = (imm // 8) & 0x7F
    return 0xA9000000 | (imm7 << 15) | (rt2 << 10) | (rn << 5) | rt


def ldr_x(rt, rn, off):
    return 0xF9400000 | ((off // 8) << 10) | (rn << 5) | rt


def str_x(rt, rn, off):
    return 0xF9000000 | ((off // 8) << 10) | (rn << 5) | rt


def str_w(rt, rn, off):
    return 0xB9000000 | ((off // 4) << 10) | (rn << 5) | rt


def sub_sp(imm):
    return 0xD1000000 | (imm << 10) | (SP << 5) | SP


def mrs_sp_el0(rt):
    """mrs Xt, SP_EL0 —— 只为让假函数看起来更像真的"""
    return 0xD5384200 | rt


def nop():
    return 0xD503201F


def image(code_at):
    """code_at: {文件偏移: [指令字, ...]} → 一段可扫描的 buffer"""
    buf = bytearray(SPAN)
    for at, words in code_at.items():
        for i, w in enumerate(words):
            struct.pack_into("<I", buf, at + i * 4, w)
    return bytes(buf)


# ---------------- 解码 ----------------
def test_decode_ldr_str_imm():
    assert ainsn.decode(ldr_x(8, 20, 0x7E0)) == {
        "k": "ldr_x", "rt": 8, "rn": 20, "off": 0x7E0, "rt2": None}
    assert ainsn.decode(str_w(3, 19, 0x48))["off"] == 0x48
    assert ainsn.decode(str_x(0, 19, 0x40))["k"] == "str_x"


def test_decode_stp_pre_signed():
    """前索引的 imm7 是负数，解出来必须是负值 —— 写错会让帧大小算成 0xFxxxx"""
    d = ainsn.decode(stp_pre(29, 30, SP, -0x30))
    assert d["k"] == "stp_pre" and d["rn"] == SP and d["off"] == -0x30
    assert d["rt"] == 29 and d["rt2"] == 30
    d = ainsn.decode(stp_off(20, 19, SP, 0x50))
    assert d["k"] == "stp_off" and d["off"] == 0x50


def test_unknown_instruction_is_none():
    assert ainsn.decode(nop()) is None
    assert ainsn.kind(0xDEADBEEF) is None


def test_real_leaf5_prologue():
    """Leaf5 镜像里 commit_creds 的原样开头（取自镜像，非手算）"""
    assert ainsn.decode(0xA9BD7BFD)["off"] == -0x30      # stp x29,x30,[sp,#-0x30]!
    d = ainsn.decode(0xF9000BF5)                          # str x21, [sp, #0x10]
    assert d["k"] == "str_x" and d["rn"] == SP and d["off"] == 0x10
    d = ainsn.decode(0xF947E68C & 0xFFFFFFFF)             # ldr x12, [x20, #0xfc8]
    assert d["k"] == "ldr_x" and d["rn"] == 20


# ---------------- 定标 ----------------
def test_task_cred_offsets_from_commit_creds():
    cc = 0x1000
    buf = image({cc: [
        stp_pre(29, 30, SP, -0x30),
        str_x(21, SP, 0x10),
        mrs_sp_el0(20),
        ldr_x(19, 20, 0x7D8),      # real_cred
        ldr_x(8, 20, 0x7E0),       # cred
    ]})
    assert ainsn.task_cred_offsets(buf, cc) == (0x7D8, 0x7E0)


def test_task_cred_offsets_needs_same_base_and_adjacent():
    cc = 0x1000
    # 基址不同 → 不是同一对象的两字段
    buf = image({cc: [stp_pre(29, 30, SP, -0x30),
                      ldr_x(19, 20, 0x7D8), ldr_x(8, 21, 0x7E0)]})
    assert ainsn.task_cred_offsets(buf, cc) is None
    # 差不是 8
    buf = image({cc: [stp_pre(29, 30, SP, -0x30),
                      ldr_x(19, 20, 0x7D8), ldr_x(8, 20, 0x7F0)]})
    assert ainsn.task_cred_offsets(buf, cc) is None


def test_prctl_frame_adds_preindex_and_sub():
    pr = 0x2000
    buf = image({pr: [
        stp_pre(29, 30, SP, -0x60),
        stp_off(28, 27, SP, 0x10),
        nop(),
        stp_off(20, 19, SP, 0x50),
        sub_sp(0x1A0),
    ]})
    assert ainsn.prctl_frame(buf, pr) == 0x200


def test_prctl_frame_accumulates_multiple_sub():
    pr = 0x2000
    buf = image({pr: [stp_pre(29, 30, SP, -0x40), sub_sp(0x80), sub_sp(0x40)]})
    assert ainsn.prctl_frame(buf, pr) == 0x100


def test_prctl_frame_rejects_bad_prologue():
    buf = image({0x2000: [nop(), sub_sp(0x100)]})
    assert ainsn.prctl_frame(buf, 0x2000) is None


def test_waiter_size_detects_deadline():
    off = 0x3000
    with_deadline = image({off: [str_w(8, 19, 0x40), str_x(9, 19, 0x48)]})
    assert ainsn.waiter_size(with_deadline, [("task_blocks_on_rt_mutex", off)]) \
        == (0x50, "task_blocks_on_rt_mutex")

    only_prio = image({off: [str_w(8, 19, 0x40), str_x(9, 19, 0x30)]})
    assert ainsn.waiter_size(only_prio, [("x", off)]) == (0x40, "x")


def test_waiter_size_unknown_when_no_write():
    assert ainsn.waiter_size(image({}), [("x", 0x3000)]) == (None, None)


# ---------------- 整体 ----------------
def _syms(**kw):
    base = {"commit_creds": 0x1000, "__arm64_sys_prctl": 0x2000,
            "task_blocks_on_rt_mutex": 0x3000}
    base.update(kw)
    return base


def test_calibrate_all_four():
    buf = image({
        0x1000: [stp_pre(29, 30, SP, -0x30), ldr_x(19, 20, 0x7D8),
                 ldr_x(8, 20, 0x7E0)],
        0x2000: [stp_pre(29, 30, SP, -0x60), sub_sp(0x1A0)],
        0x3000: [str_w(8, 19, 0x40), str_x(9, 19, 0x48)],
    })
    got, ev = ainsn.calibrate(buf, _syms())
    assert got == {"TASK_REAL_CRED_OFF": 0x7D8, "TASK_CRED_OFF": 0x7E0,
                   "PRCTL_FRAME_TOTAL": 0x200, "WAITER_LOCAL_OFF": 0x50}
    assert "task_blocks_on_rt_mutex" in ev["WAITER_LOCAL_OFF"]


def test_calibrate_gives_up_on_misaligned_offsets():
    """符号偏移与文件内容不符时（Image 前面多了头），必须整体放弃而不是硬算。"""
    buf = image({0x1000: [nop(), nop()]})
    got, ev = ainsn.calibrate(buf, _syms())
    assert got == {} and ev == {}


def test_calibrate_without_symbols_returns_empty():
    assert ainsn.calibrate(image({}), {}) == ({}, {})
