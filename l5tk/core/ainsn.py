"""AArch64 指令扫描：从裸 Image 里把结构体偏移「读」出来。

target.h 里有 4 个手工宏（不在 kallsyms 里，符号比对拿不到），
旧流程要人拿 IDA/x64dbg 反汇编去对。它们其实是**指令里的立即数**，
扫几条指令就能定：

  TASK_REAL_CRED_OFF / TASK_CRED_OFF
      commit_creds 开头：
          mrs  x20, SP_EL0          ; current
          ldr  x19, [x20, #0x7d8]   ; task->real_cred
          ldr  x8,  [x20, #0x7e0]   ; task->cred
      一对相邻、同基址、差 8 的 64 位 LDR 就是这两个。

  PRCTL_FRAME_TOTAL
      __arm64_sys_prctl 的开头一定是
          stp  x29, x30, [sp, #-0x60]!
          stp  ...      [sp, #0x50]     ; 寄存器保存区
          sub  sp, sp, #0x1a0
      总帧 = 0x60 + 0x1a0 = 0x200。把「前索引降栈」与后续所有
      sub sp, sp, #imm 相加即可。

  sizeof(struct rt_mutex_waiter)
      4.19 主线的 waiter 是 0x40；带 prio/deadline 的定制版是 0x50。
      判据：rt_mutex 相关函数里有没有对 +0x48 的写（deadline 字段）。
      有 → 0x50，没有 → 0x40。这一项只作提示，不自动覆盖。

只解这几种编码，不做通用反汇编器 —— 认不出来的指令直接跳过，
算不出就返回 None，让调用方保留占位符。宁可少填，不能填错。
"""

import struct

# ---- 编码掩码 / 基值（ARM64 A64） ----
_LDR_X_UIMM = (0xFFC00000, 0xF9400000)   # LDR Xt, [Xn, #imm12*8]
_STR_X_UIMM = (0xFFC00000, 0xF9000000)   # STR Xt, [Xn, #imm12*8]
_LDR_W_UIMM = (0xFFC00000, 0xB9400000)   # LDR Wt, [Xn, #imm12*4]
_STR_W_UIMM = (0xFFC00000, 0xB9000000)   # STR Wt, [Xn, #imm12*4]
_SUB_SP_IMM = (0xFF800000, 0xD1000000)   # SUB sp, sp, #imm12 (sh=0)
_STP_X_PRE = (0xFFC00000, 0xA9800000)   # STP Xt, Xt2, [Xn, #-imm7*8]!
_STP_X_OFF = (0xFFC00000, 0xA9000000)   # STP Xt, Xt2, [Xn, #imm7*8]
_LDP_X_OFF = (0xFFC00000, 0xA9400000)

_SP = 31


def _u32(buf, off):
    if off < 0 or off + 4 > len(buf):
        return None
    return struct.unpack_from("<I", buf, off)[0]


def _sx(v, bits):
    """符号扩展：imm7 / imm12 这类字段。"""
    sign = 1 << (bits - 1)
    return (v & (sign - 1)) - (v & sign)


def kind(w):
    """这条指令属于哪一类；不认识的返回 None。"""
    if w is None:
        return None
    for name, (mask, base) in (
        ("ldr_x", _LDR_X_UIMM), ("str_x", _STR_X_UIMM),
        ("ldr_w", _LDR_W_UIMM), ("str_w", _STR_W_UIMM),
        ("sub_sp", _SUB_SP_IMM),
    ):
        if w & mask == base:
            return name
    if w & 0xFFC00000 == _STP_X_PRE[1]:
        return "stp_pre"
    if w & 0xFFC00000 == _STP_X_OFF[1]:
        return "stp_off"
    if w & 0xFFC00000 == _LDP_X_OFF[1]:
        return "ldp_off"
    return None


def decode(w):
    """解成 dict：{'k':类别, 'rt':, 'rn':, 'off':字节偏移}。

    off 已按访问宽度放大（ldr_x/str_x ×8，ldr_w/str_w ×4，stp/ldp ×8）。
    sub_sp 的 off 是 imm12 原值。
    """
    k = kind(w)
    if k is None:
        return None
    rt = w & 0x1F
    rn = (w >> 5) & 0x1F
    if k in ("ldr_x", "str_x"):
        off = ((w >> 10) & 0xFFF) * 8
    elif k in ("ldr_w", "str_w"):
        off = ((w >> 10) & 0xFFF) * 4
    elif k == "sub_sp":
        off = (w >> 10) & 0xFFF
    elif k == "stp_pre":
        off = _sx((w >> 15) & 0x7F, 7) * 8      # 前索引：负数
    else:                                        # stp_off / ldp_off
        off = _sx((w >> 15) & 0x7F, 7) * 8
    return {"k": k, "rt": rt, "rn": rn, "off": off,
            "rt2": (w >> 10) & 0x1F if k in ("stp_pre", "stp_off", "ldp_off") else None}


def scan(buf, start, n_insn):
    """从 start 起解 n_insn 条，返回解出来的列表（含下标）。"""
    out = []
    for i in range(n_insn):
        w = _u32(buf, start + i * 4)
        if w is None:
            break
        d = decode(w)
        if d:
            d["i"] = i
            d["pc"] = start + i * 4
            d["w"] = w
            out.append(d)
    return out


# ---------------- 定标 ----------------

def task_cred_offsets(buf, commit_creds_off, window=0x60):
    """从 commit_creds 开头找 task->real_cred / task->cred。

    判据：两条 64 位 LDR，同基址、偏移相差 8、位置相邻（中间最多隔一条
    无关指令），且偏移落在 0x300~0x1000（task_struct 尾部区域）。
    返回 (real_cred_off, cred_off)，找不到返回 None。

    为什么认「相邻差 8」而不是认固定值：两个字段在源码里就是
    `const struct cred __rcu *real_cred; const struct cred __rcu *cred;`
    连着定义的，编译器一定会连着读；一旦内核动了布局，这里跟着变。
    """
    ins = scan(buf, commit_creds_off, window // 4)
    ldr = [d for d in ins if d["k"] == "ldr_x"]
    for a, b in zip(ldr, ldr[1:]):
        if a["rn"] != b["rn"]:
            continue
        if b["off"] - a["off"] != 8:
            continue
        if b["i"] - a["i"] > 3:          # 中间夹太多指令就不是同一对
            continue
        if not (0x300 <= a["off"] <= 0x1000):
            continue
        return a["off"], b["off"]
    return None


def prctl_frame(buf, prctl_off, window=0x40):
    """__arm64_sys_prctl 的总栈帧。

    开头 stp x29,x30,[sp,#-N]! 先把 N 降下去，后面用 sub sp, sp, #M
    再要一块；两者相加就是函数的总帧。中途多次 sub 也累加 ——
    4.19 只有一次，但不写死。
    """
    ins = scan(buf, prctl_off, window // 4)
    first = ins[0] if ins else None
    if not first or first["k"] != "stp_pre" or first["rn"] != _SP:
        return None
    total = -first["off"]
    for d in ins[1:]:
        if d["k"] == "sub_sp" and d["rn"] == _SP and d["rt"] == _SP:
            total += d["off"]
    return total if total else None


def waiter_size(buf, probe_syms):
    """判 sizeof(struct rt_mutex_waiter)：0x50（有 prio/deadline）或 0x40。

    判据：rt_mutex 的持锁路径里对 waiter 写 deadline（+0x48）。
    只要有一个函数里出现 [reg, #0x48] 的 64 位写，就认定带扩展字段。
    probe_syms: [(符号名, 偏移), ...]
    返回 (size, 证据符号名) 或 (None, None)。
    """
    for name, off in probe_syms:
        if off is None:
            continue
        ins = scan(buf, off, 0x200 // 4)
        # 找 0x40 与 0x48 的写：两处都出现才认（+0x40 是 wprio，+0x48 是 deadline）
        has40 = any(d["k"] in ("str_w", "str_x") and d["off"] == 0x40 for d in ins)
        has48 = any(d["k"] in ("str_w", "str_x") and d["off"] == 0x48 for d in ins)
        if has40 and has48:
            return 0x50, name
        if has40:
            return 0x40, name
    return None, None


# ---------------- 一次算全 ----------------

MANUAL_AUTO = (
    "TASK_REAL_CRED_OFF", "TASK_CRED_OFF", "PRCTL_FRAME_TOTAL",
)


def calibrate(buf, syms, log=None):
    """把能自动定的手工宏算出来。

    buf  : 裸 Image 全文
    syms : {"commit_creds": off, ...}（偏移 = 地址 - _text，即文件偏移）
    返回 dict{宏名: 值}（只含算出来的），以及证据说明。
    """
    say = log or (lambda *_: None)
    got, ev = {}, {}

    cc = syms.get("commit_creds")
    if cc is not None:
        # 自检：commit_creds 的第一条指令必是 stp x29,x30,[sp,#-N]!。
        # 对不上说明「符号偏移 ≠ 文件偏移」（Image 前面多了头、或源是 ELF），
        # 这时候硬扫会算出看着合理但错的数 —— 直接放弃，退回手工。
        head = scan(buf, cc, 1)
        if not head or head[0]["k"] != "stp_pre" or head[0]["rn"] != _SP:
            say("  [!] 符号偏移与文件内容对不上（%s 处不是函数开头），跳过自动定标"
                % hex(cc))
            return {}, {}
        r = task_cred_offsets(buf, cc)
        if r:
            got["TASK_REAL_CRED_OFF"], got["TASK_CRED_OFF"] = r
            ev["TASK_CRED_OFF"] = "commit_creds+0x%X 两条相邻 LDR" % (cc & 0xFFF)
            say("  [自动] TASK_REAL_CRED_OFF = 0x%X   TASK_CRED_OFF = 0x%X"
                % r)
        else:
            say("  [!] commit_creds 里没认出 real_cred/cred 那一对 LDR")

    pr = syms.get("__arm64_sys_prctl")
    if pr is not None:
        f = prctl_frame(buf, pr)
        if f:
            got["PRCTL_FRAME_TOTAL"] = f
            ev["PRCTL_FRAME_TOTAL"] = "__arm64_sys_prctl 降栈总量"
            say("  [自动] PRCTL_FRAME_TOTAL = 0x%X" % f)
        else:
            say("  [!] __arm64_sys_prctl 的开头不是预期形态")

    probes = [(n, syms.get(n)) for n in
              ("rt_mutex_slowlock", "task_blocks_on_rt_mutex",
               "rt_mutex_adjust_prio_chain")]
    probes = [p for p in probes if p[1] is not None]
    size, who = waiter_size(buf, probes)
    if size:
        # 0x40 / 0x50 是 hard 事实（waiter 有没有 prio+deadline 两个字段），
        # 探测到就直接定；但把证据符号写出来，让人能自己复核。
        got["WAITER_LOCAL_OFF"] = size
        ev["WAITER_LOCAL_OFF"] = "%s 里 waiter+0x40/0x48 的写点" % who
        say("  [自动] WAITER_LOCAL_OFF = 0x%X（证据：%s 对 +0x40%s 的写）"
            % (size, who, "/+0x48" if size == 0x50 else ""))

    return got, ev
