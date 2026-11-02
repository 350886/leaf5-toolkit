"""内核适配：符号偏移替换、手工宏、同源继承、target 匹配。

不起 NDK、不碰真内核：用合成的 kallsyms 文本喂进流水线，验的是
「偏移算得对、占位符留得对、匹配优先级对」这三件事。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from l5tk.config import load as load_cfg
from l5tk.core import elfsyms, kimage
from l5tk.ops import adapt


def make_cfg(tmp_path: Path):
    return load_cfg(adapt_root=tmp_path / "adapt",
                    work_dir=tmp_path / "work",
                    log_dir=tmp_path / "logs")


def make_kallsyms(path: Path, template: Path, base: int = 0xFFFFFF8100000000,
                  fixed: dict | None = None) -> dict:
    """按模板里的 *_OFF 宏造一份 kallsyms。返回 {宏: 偏移}。

    fixed 给了就用它当偏移（验「同源继承」），否则每个符号 +0x1000 递推。
    """
    pairs = elfsyms.discover_macros(str(template))
    by_sym = dict(pairs)
    lines = ["%016x T _text" % base]
    offs = {}
    for i, (macro, sym) in enumerate(pairs):
        off = (fixed or {}).get(macro, 0x1000 * (i + 1))
        if fixed and macro not in fixed:
            off = 0x1000 * (i + 1)
        offs[macro] = off
        lines.append("%016x t %s" % (base + off, sym))
    assert by_sym  # 模板里至少要有一个宏
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return offs


def template_offsets(a: adapt.Adapter) -> dict:
    return adapt.template_offsets(a.template())


# ----------------------------------------------------------------------
# 纯函数
# ----------------------------------------------------------------------
def test_build_target_h_replaces_and_marks(tmp_path):
    tpl = Path("assets/exploit_src/exploit/targets/onyx-leaf5/target.h")
    if not tpl.is_file():
        pytest.skip("模板不在仓库里")
    out = tmp_path / "t1" / "target.h"
    unfilled, notfound, diffs = adapt.build_target_h(
        tpl, out, {"TASK_CRED_OFF_X": 0}, "FP", "4.19.157", manual_values={})
    txt = out.read_text(encoding="utf-8")
    assert "由 l5tk 内核适配向导生成" in txt
    assert adapt.FILL_MARK in txt
    assert set(unfilled) == set(m for m, _ in adapt.MANUAL_MACROS)
    assert notfound == ["TASK_CRED_OFF_X"]     # 模板里没这个宏，要报告
    assert diffs == []


def test_build_target_h_keep_mode(tmp_path):
    """keep：模板值与镜像值不同时保留模板值（同版本重编不想动已验证配置）"""
    tpl = tmp_path / "target.h"
    tpl.write_text("#define FOO_OFF 0x100ULL\n", encoding="utf-8")
    out = tmp_path / "t2" / "target.h"
    adapt.build_target_h(tpl, out, {"FOO_OFF": 0x200}, "", "", syms_mode="keep")
    assert "0x100ULL" in out.read_text(encoding="utf-8")     # keep 保留原字面量
    adapt.build_target_h(tpl, out, {"FOO_OFF": 0x200}, "", "", syms_mode="trust")
    assert "0x000000000200" in out.read_text(encoding="utf-8")


def test_banner_match_verdicts():
    dev = "Linux version 4.19.157-perf-g77c132eccfab (builder@host) #1 SMP"
    same = kimage.banner_match("4.19.157-perf-g77c132eccfab #1 SMP", dev)
    assert same[0] == "same"

    dirty = kimage.banner_match("4.19.157-perf-g77c132eccfab-dirty #1 SMP", dev)
    assert dirty[0] == "dirty"          # commit/build 都对，但带 -dirty

    diff = kimage.banner_match("4.19.157-perf-g000000000000 #1", dev)
    assert diff[0] == "different"

    resame = kimage.banner_match("4.19.157-perf-g77c132eccfab #2 SMP", dev)
    assert resame[0] == "resame"

    unknown = kimage.banner_match("4.19.157 nobody", dev)
    assert unknown[0] == "unknown"


# ----------------------------------------------------------------------
# 整条流水线（合成 kallsyms）
# ----------------------------------------------------------------------
def test_gen_reports_missing_manuals(tmp_path):
    cfg = make_cfg(tmp_path)
    a = adapt.Adapter(cfg, log=None)
    tpl = a.template()
    offs = make_kallsyms(tmp_path / "sym.txt", tpl)

    r = a.gen(tmp_path / "sym.txt", fingerprint="FP1",
              kernel="4.19.157-perf-gdeadbeef-dirty")
    assert r.offsets > 50
    assert r.manual == "template"                   # 偏移全不同 → 不继承
    assert set(r.unfilled) == set(m for m, _ in adapt.MANUAL_MACROS)
    txt = (r.tdir / "target.h").read_text(encoding="utf-8")
    macro, sym = elfsyms.discover_macros(str(tpl))[0]
    assert "0x%012X" % offs[macro] in txt

    mf = json.loads((r.tdir / "manifest.json").read_text(encoding="utf-8"))
    assert mf["kernel"].endswith("-dirty")
    assert mf["manual"] == "template"


def test_gen_inherits_manuals_when_same_build(tmp_path):
    """镜像每个宏都等于模板值 → 同源，4 个手工宏直接沿用，可以直接编译"""
    cfg = make_cfg(tmp_path)
    a = adapt.Adapter(cfg, log=None)
    tpl = a.template()
    tpl_offs = template_offsets(a)
    make_kallsyms(tmp_path / "sym.txt", tpl, fixed=tpl_offs)

    r = a.gen(tmp_path / "sym.txt", fingerprint="FP2", kernel="same-kernel")
    assert r.manual == "inherited"
    assert r.unfilled == []
    assert r.diffs == []
    txt = (r.tdir / "target.h").read_text(encoding="utf-8")
    for macro, _ in adapt.MANUAL_MACROS:
        assert adapt.FILL_MARK not in txt.split(macro)[1][:200]


def test_fill_then_compile_gate(tmp_path):
    cfg = make_cfg(tmp_path)
    a = adapt.Adapter(cfg, log=None)
    make_kallsyms(tmp_path / "sym.txt", a.template())
    r = a.gen(tmp_path / "sym.txt", fingerprint="FP3", kernel="k3")
    assert adapt.placeholders(r.tdir) == [m for m, _ in adapt.MANUAL_MACROS]

    left = adapt.fill_manuals(r.tdir, {"TASK_CRED_OFF": 0x7E0})
    assert set(left) == set(m for m, _ in adapt.MANUAL_MACROS) - {"TASK_CRED_OFF"}

    left = adapt.fill_manuals(r.tdir, {m: 0x10 for m, _ in adapt.MANUAL_MACROS})
    assert left == []
    # 编译前的闸：占位符没了才允许进 compile（这里不真编译，只验闸）
    assert adapt.placeholders(r.tdir) == []


def test_manifest_and_pick_priority(tmp_path):
    cfg = make_cfg(tmp_path)
    a = adapt.Adapter(cfg, log=None)
    root = a.targets_root

    for tid, kernel, fp, digest in (
            ("t-kernel", "4.19.157-perf-gdeadbeef-dirty", "FPX", ""),
            ("t-fp", "4.19.157-perf-gaaaa", "FPX", ""),
            ("t-sym", "4.19.157-perf-gbbbb", "FPY", "d1")):
        d = root / tid
        d.mkdir(parents=True)
        (d / "preload.so").write_bytes(b"\x7fELF" + b"\0" * 60)
        adapt.save_manifest(d, {"id": tid, "kernel": kernel,
                                "fingerprint": fp, "sym_digest": digest,
                                "created": "2026-01-01T00:00:00"})

    assert a.pick(sym_digest="d1").id == "t-sym"          # ① 符号序列最硬
    assert a.pick(fingerprint="FPX").id == "t-fp"         # ② 指纹
    assert a.pick(kernel="4.19.157-perf-gdeadbeef-dirty").id == "t-kernel"
    assert a.pick(fingerprint="nope", kernel="nope") is None

    # 没有 preload.so 的 target 不参与挑选
    bare = root / "t-bare"
    bare.mkdir()
    adapt.save_manifest(bare, {"id": "t-bare", "kernel": "4.19.157-perf-gdeadbeef-dirty"})
    assert a.pick(kernel="4.19.157-perf-gdeadbeef-dirty").id == "t-kernel"


def test_use_requires_so(tmp_path):
    cfg = make_cfg(tmp_path)
    a = adapt.Adapter(cfg, log=None)
    d = a.targets_root / "t1"
    d.mkdir(parents=True)
    adapt.save_manifest(d, {"id": "t1", "kernel": "k"})
    with pytest.raises(Exception):
        a.use("t1")                       # 没 so
    (d / "preload.so").write_bytes(b"\x7fELF")
    assert a.use("t1").id == "t1"
    assert a.active() == "t1"


def test_ensure_src_copies_exploit(tmp_path):
    cfg = make_cfg(tmp_path)
    a = adapt.Adapter(cfg, log=None)
    dst = a.ensure_src()
    assert (dst / "src" / "main.c").is_file()
    assert (dst / "targets" / "onyx-leaf5" / "target.h").is_file()
    assert (dst / "src" / "assets" / "wallpaper.webp").is_file()
    # 二次调用不重复拷
    before = (dst / "src" / "main.c").stat().st_mtime_ns
    a.ensure_src()
    assert (dst / "src" / "main.c").stat().st_mtime_ns == before


def test_force_inherit_keeps_template_manuals(tmp_path):
    """--inherit：明知有差异也沿用模板的 4 个值（给「差异全是死宏」的场合）"""
    cfg = make_cfg(tmp_path)
    a = adapt.Adapter(cfg, log=None)
    make_kallsyms(tmp_path / "sym.txt", a.template())     # 全不同 → 默认不继承
    r = a.gen(tmp_path / "sym.txt", fingerprint="F", kernel="K",
              force_inherit=True)
    assert r.manual == "forced"
    assert r.unfilled == []
    assert r.diffs                      # 差异仍然照实报告


def test_force_inherit_keeps_template_manuals(tmp_path):
    """--inherit：明知有差异也沿用模板的 4 个值（给「差异全是死宏」的场合）"""
    cfg = make_cfg(tmp_path)
    a = adapt.Adapter(cfg, log=None)
    make_kallsyms(tmp_path / "sym.txt", a.template())     # 全不同 → 默认不继承
    r = a.gen(tmp_path / "sym.txt", fingerprint="F", kernel="K",
              force_inherit=True)
    assert r.manual == "forced"
    assert r.unfilled == []
    assert r.diffs                      # 差异仍然照实报告


# ---------------- 身份判定：镜像说了算 ----------------

def test_pick_identity_prefers_image_banner():
    """解析别的型号的 ROM 时，不能把「手上这台机器」的内核串记进去。"""
    banner = ("4.19.157-perf-ge2582afab8ec-dirty (onyx@onyxUbuntu) #11 SMP "
              "PREEMPT Sat May 30 11:56:07 CST 2026")
    dev = {"kernel": "4.19.157-perf-g77c132eccfab-dirty",
           "fingerprint": "ONYX/TabBoox/TabBoox:13/TKQ1.230615.001/x:user/release-keys"}
    kernel, fp = adapt.pick_identity(banner, dev)
    assert kernel == "4.19.157-perf-ge2582afab8ec-dirty", \
        "内核串必须来自镜像 banner"
    assert fp == "", "镜像不是这台机器的，就不该继承它的 fingerprint"


def test_pick_identity_keeps_fingerprint_when_same_kernel():
    """镜像就是这台机器时，fingerprint 沿用设备值（匹配靠它更准）。"""
    banner = "4.19.157-perf-g77c132eccfab-dirty (onyx@x) #38 SMP"
    dev = {"kernel": "4.19.157-perf-g77c132eccfab-dirty",
           "fingerprint": "ONYX/TabBoox/TabBoox:13/xxx:user/release-keys"}
    kernel, fp = adapt.pick_identity(banner, dev)
    assert kernel == dev["kernel"] and fp == dev["fingerprint"]


def test_pick_identity_without_banner_uses_device():
    """纯 kallsyms 文本没有 banner，只能信设备。"""
    dev = {"kernel": "4.19.157-perf-g77c132eccfab-dirty",
           "fingerprint": "FP"}
    assert adapt.pick_identity(None, dev) == (dev["kernel"], "FP")


def test_pick_identity_explicit_args_win():
    banner = "4.19.157-perf-ge2582afab8ec-dirty (o) #11 SMP"
    dev = {"kernel": "4.19.157-perf-g77c132eccfab-dirty", "fingerprint": "FP"}
    kernel, fp = adapt.pick_identity(banner, dev, kernel="4.19.157-hand-set",
                                     fingerprint="FP-HAND")
    assert kernel == "4.19.157-hand-set" and fp == "FP-HAND"


def test_pick_identity_no_adb():
    """没连设备：镜像有 banner 就按 banner，指纹留空。"""
    banner = "4.19.157-perf-ge2582afab8ec-dirty (o) #11 SMP"
    assert adapt.pick_identity(banner, {}) == \
        ("4.19.157-perf-ge2582afab8ec-dirty", "")


# ---------------- 身份判定：镜像说了算 ----------------

def test_pick_identity_prefers_image_banner():
    """解析别的型号的 ROM 时，不能把「手上这台机器」的内核串记进去。"""
    banner = ("4.19.157-perf-ge2582afab8ec-dirty (onyx@onyxUbuntu) #11 SMP "
              "PREEMPT Sat May 30 11:56:07 CST 2026")
    dev = {"kernel": "4.19.157-perf-g77c132eccfab-dirty",
           "fingerprint": "ONYX/TabBoox/TabBoox:13/TKQ1.230615.001/x:user/release-keys"}
    kernel, fp = adapt.pick_identity(banner, dev)
    assert kernel == "4.19.157-perf-ge2582afab8ec-dirty", \
        "内核串必须来自镜像 banner"
    assert fp == "", "镜像不是这台机器的，就不该继承它的 fingerprint"


def test_pick_identity_keeps_fingerprint_when_same_kernel():
    """镜像就是这台机器时，fingerprint 沿用设备值（匹配靠它更准）。"""
    banner = "4.19.157-perf-g77c132eccfab-dirty (onyx@x) #38 SMP"
    dev = {"kernel": "4.19.157-perf-g77c132eccfab-dirty",
           "fingerprint": "ONYX/TabBoox/TabBoox:13/xxx:user/release-keys"}
    kernel, fp = adapt.pick_identity(banner, dev)
    assert kernel == dev["kernel"] and fp == dev["fingerprint"]


def test_pick_identity_without_banner_uses_device():
    """纯 kallsyms 文本没有 banner，只能信设备。"""
    dev = {"kernel": "4.19.157-perf-g77c132eccfab-dirty",
           "fingerprint": "FP"}
    assert adapt.pick_identity(None, dev) == (dev["kernel"], "FP")


def test_pick_identity_explicit_args_win():
    banner = "4.19.157-perf-ge2582afab8ec-dirty (o) #11 SMP"
    dev = {"kernel": "4.19.157-perf-g77c132eccfab-dirty", "fingerprint": "FP"}
    kernel, fp = adapt.pick_identity(banner, dev, kernel="4.19.157-hand-set",
                                     fingerprint="FP-HAND")
    assert kernel == "4.19.157-hand-set" and fp == "FP-HAND"


def test_pick_identity_no_adb():
    """没连设备：镜像有 banner 就按 banner，指纹留空。"""
    banner = "4.19.157-perf-ge2582afab8ec-dirty (o) #11 SMP"
    assert adapt.pick_identity(banner, {}) == \
        ("4.19.157-perf-ge2582afab8ec-dirty", "")
