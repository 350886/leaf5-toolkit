"""命令行入口。

退出码约定（写进 README，脚本可依赖）：
    0  成功
    1  动作失败（有 error 详情）
    2  用法/环境问题（参数错、adb 不在、设备没插）
    3  dry-run 下被拒绝执行
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import Config, load as load_cfg
from .core.adb import Adb
from .core.errors import AdbError, L5Error, Refused
from .core.fastboot import Fastboot
from .device import all_profiles, by_key, detect, table
from .flags import gp_ui_enabled
from .logx import Logger, register_secret
from .ops.gplay import GPlay, find_apk
from .ops.probe import Prober
from .ops.root import RootOps, parse_boot

RC_OK, RC_FAIL, RC_USAGE, RC_DRYRUN = 0, 1, 2, 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="l5tk",
        description="l5tk %s —— 多机型 Android 工具箱" % __version__,
        epilog="设备串、账号在日志里自动打码；要看原文设 L5TK_LOG_PLAIN=1",
    )
    p.add_argument("-v", "--version", action="version",
                   version="l5tk %s" % __version__)
    p.add_argument("--serial", help="指定设备 serial（默认取唯一在线设备）")
    p.add_argument("--adb", help="adb 路径（默认内嵌 assets/bin/adb）")
    p.add_argument("--fastboot", help="fastboot 路径")
    p.add_argument("--log-plain", action="store_true", help="日志不打码（排障用）")
    p.add_argument("--dry-run", action="store_true", help="只检查不动手")
    p.add_argument("-q", "--quiet", action="store_true", help="不输出到控制台，只写日志")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("profiles", help="列出机型档案表")
    sub.add_parser("devices", help="列出 adb 设备")
    sub.add_parser("doctor", help="环境自检：adb/fastboot 是否可用")

    sp = sub.add_parser("probe", help="设备画像")
    sp.add_argument("--json", action="store_true", help="输出 JSON")
    sp.add_argument("--raw", action="store_true", help="附上完整 prop")
    sp.add_argument("--shallow", action="store_true",
                    help="跳过 root 与账号探测（快，未 root 的设备不卡超时）")

    root = sub.add_parser("root", help="root 相关")
    root.add_argument("action",
                      choices=("status", "adb", "flash", "check", "part",
                               "magisk", "auto"),
                      help="status=看状态 adb=试 adb root flash=刷 boot "
                           "check=只校验镜像 part=看会刷哪个分区（只读） "
                           "magisk=装内置 Magisk App "
                           "auto=一键 root（按代价从低到高依次试）")
    root.add_argument("image", nargs="?", help="flash 用的 boot 镜像")
    root.add_argument("--part", help="分区名（默认按机型档案）")
    root.add_argument("--no-backup", action="store_true", help="跳过刷前备份")
    root.add_argument("--no-reinstall", action="store_true",
                      help="magisk：已装时不覆盖")
    root.add_argument("--full", action="store_true",
                      help="magisk：全自动（备份 → 设备内打补丁 → dd 写回 → 回读校验）")
    root.add_argument("--module", action="store_true",
                      help="magisk --full：顺带铺 AMS 修复模块（ONYX 的 Magisk App 崩溃）")
    root.add_argument("--no-reboot", action="store_true",
                      help="magisk --full / auto：写回后不重启，由你自己选时机")
    root.add_argument("--no-fastboot", action="store_true",
                      help="auto：不试 fastboot 兜底（只走 adb root / dd）")
    root.add_argument("--force", action="store_true",
                      help="档案未验证/已上锁时仍然执行")

    gp = sub.add_parser("gplay", help="Google Play")
    gp.add_argument("action", choices=("state", "install", "enable"),
                    help="state=看状态 install=只装 enable=装+拉起+判定")
    gp.add_argument("--apk", help="指定 APK 路径")
    gp.add_argument("--no-reinstall", action="store_true", help="已装时不重装")

    vf = sub.add_parser("verify", help="验收：root / 商店 / GMS 逐项过")
    vf.add_argument("--json", action="store_true")

    return p


def make_logger(args) -> Logger:
    cfg = load_cfg(serial=args.serial, adb=args.adb, fastboot=args.fastboot,
                   plain_log=True if args.log_plain else None,
                   dry_run=True if args.dry_run else None)
    cfg.ensure_dirs()
    return Logger(cfg.log_dir, plain=cfg.plain_log, echo=not args.quiet)


def _ctx(args, log: Logger):
    cfg = load_cfg(serial=args.serial, adb=args.adb, fastboot=args.fastboot,
                   plain_log=True if args.log_plain else None,
                   dry_run=True if args.dry_run else None)
    cfg.ensure_dirs()
    if args.serial:
        register_secret(args.serial)
    adb = Adb(cfg, args.serial or "")
    fb = Fastboot(cfg, args.serial or "")
    return cfg, adb, fb


# ---------------- 子命令 ----------------
def cmd_profiles(_args, _log) -> int:
    print(table())
    print("\n档案未在真机验证的机型，刷分区默认会被拒绝（需要 --force）。")
    return RC_OK


def cmd_devices(_args, log) -> int:
    cfg = load_cfg()
    adb = Adb(cfg)
    devs = adb.devices()
    if not devs:
        print("没有设备。插 USB、开 USB 调试、手机上点允许。")
        return RC_USAGE
    for d in devs:
        print("%-24s %-14s %s" % (d.serial, d.state, d.model))
    return RC_OK


def _bundled(cfg: Config, exe: str) -> bool:
    """这个路径是不是从 assets/ 里解出来的（内嵌）。"""
    try:
        p = Path(exe).resolve()
    except OSError:
        return False
    for base in (cfg.base_dir, cfg.bundle_dir):
        try:
            p.relative_to(Path(base).resolve() / "assets")
            return True
        except (ValueError, OSError):
            continue
    return False


def cmd_doctor(args, log) -> int:
    cfg, adb, fb = _ctx(args, log)
    rows = []
    ok_all = True
    # fastboot 的版本开关是 --version，`fastboot version` 会被当成镜像名去等设备。
    for name, exe, vflag in (("adb", cfg.adb, "version"),
                             ("fastboot", cfg.fastboot, "--version")):
        exists = Path(exe).is_file() or __import__("shutil").which(exe)
        ver = ""
        if exists:
            try:
                r = adb._run([exe, vflag], 10)
                ver = (r.out or r.err).splitlines()[0] if r.text else ""
            except L5Error as e:
                ver = e.message
        tag = "[内嵌] " if _bundled(cfg, exe) else "[PATH] "
        rows.append((name, "OK" if exists else "缺失",
                     (tag + (ver or "")) if exists else exe))
        ok_all = ok_all and bool(exists)

    print("%-10s %-6s %s" % ("组件", "状态", "版本/路径"))
    for r in rows:
        print("%-10s %-6s %s" % r)

    # 内嵌件单独一行：内置 adb 和系统 adb 同时跑会抢 5037 端口，
    # 用户得先知道现在用的是哪一个。
    apk = RootOps(adb, fb, log).magisk_apk()
    print("%-10s %-6s %s" % (
        "Magisk",
        "OK" if apk else "缺失",
        ("%s（%.1f MB）" % (apk, apk.stat().st_size / 1048576.0)) if apk
        else "包里没带 APK，root magisk 不可用"))

    try:
        devs = adb.devices()
        print("\n设备：%s" % (", ".join("%s(%s)" % (d.serial, d.state) for d in devs) or "无"))
        if not devs:
            ok_all = False
    except L5Error as e:
        print("\n设备：%s" % e.render())
        ok_all = False

    print("日志目录：%s" % cfg.log_dir)
    print("工作目录：%s" % cfg.work_dir)
    return RC_OK if ok_all else RC_USAGE


def cmd_probe(args, log) -> int:
    _cfg, adb, _fb = _ctx(args, log)
    rep = Prober(adb, log).run(deep=not args.shallow)
    if args.json:
        data = rep.as_dict()
        if args.raw:
            data["props"] = adb.props()
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(rep.summary())
        if args.raw:
            print("\n--- 完整 prop ---")
            for k, v in sorted(adb.props().items()):
                print("[%s]: [%s]" % (k, v))
    return RC_OK


def cmd_root(args, log) -> int:
    _cfg, adb, fb = _ctx(args, log)
    ops = RootOps(adb, fb, log)

    if args.action == "check":
        if not args.image:
            print("check 需要给镜像路径：l5tk root check magisk_patched.img")
            return RC_USAGE
        try:
            img = parse_boot(args.image)
        except L5Error as e:
            print(e.render())
            return RC_FAIL
        print("是合法的 boot 镜像：")
        print("  " + img.describe())
        return RC_OK

    if args.action == "part":
        # 只读：告诉用户真刷的时候会落到哪个分区。刷之前先看这一行。
        det = detect(adb.props())
        print("机型：%s（匹配分 %d）" % (det.profile.label, det.score))
        part, why = ops.resolve_partition(det.profile, args.part)
        print("目标分区：%s（%s）" % (part, why))
        print("分区表：%s" % (", ".join(sorted(ops.by_name())) or "读不到"))
        return RC_OK

    if args.action == "status":
        o = ops.status()
        print(o.detail)
        print("  flash.locked=%s  unlocked=%s"
              % (o.data.get("flash_locked") or "?",
                 "是" if o.data.get("unlocked") else "否"))
        return RC_OK if o.ok else RC_FAIL

    if args.action == "auto":
        # 一键：already → adb root → Magisk dd → fastboot flash，第一个成功就停
        from .ops.autoroot import AutoRoot
        try:
            r = AutoRoot(adb, fb, log).run(
                part=args.part, with_module=args.module,
                reboot=not args.no_reboot, image=args.image,
                allow_fastboot=not args.no_fastboot)
        except L5Error as e:
            print(e.render())
            return RC_FAIL
        print(("成功：" if r.ok else "失败：") + r.detail)
        for s in r.steps:
            mark = "✓" if s.ok else ("✗" if s.tried else "·")
            print("  %s %s" % (mark, s.detail.replace("\n", "\n    ")))
        if r.stage == "dry_run":
            return RC_DRYRUN
        return RC_OK if r.ok else RC_FAIL

    if args.action == "magisk":
        # 不带 --full 只装 App；带 --full 走 V1 那条「设备内打补丁 + dd 写回」。
        # 两条路差的是「要不要动 boot 分区」，所以拆成显式开关而不是默认行为。
        if args.full:
            from .ops.magisk import MagiskOps
            try:
                o = MagiskOps(adb, log).install_full(
                    part=args.part, with_module=args.module,
                    reboot=not args.no_reboot)
            except L5Error as e:
                print(e.render())
                return RC_FAIL
            print(("成功：" if o.ok else "失败：") + o.detail)
            for r in o.reasons:
                print("  " + r)
            for k in ("stock", "patched", "module"):
                if o.data.get(k):
                    print("  %-8s %s" % (k + ":", o.data[k]))
            return RC_OK if o.ok else RC_FAIL

        o = ops.install_magisk(reinstall=not args.no_reinstall)
        print(("成功：" if o.ok else "失败：") + o.detail)
        for r in o.reasons:
            print("  " + r)
        return RC_OK if o.ok else RC_FAIL

    if args.action == "adb":
        o = ops.try_adb_root()
        print(o.detail)
        for r in o.reasons:
            print("  " + r)
        return RC_OK if o.ok else RC_FAIL

    # flash
    if not args.image:
        cands = ops.candidates()
        print("要给镜像路径。工作目录 boot\\ 下的候选：")
        for c in cands[:5]:
            print("  " + c.name)
        return RC_USAGE

    det = detect(adb.props())
    profile = det.profile
    log.info("机型：%s（分 %d）" % (profile.label, det.score))
    o = ops.flash_boot(args.image, profile, part=args.part,
                       backup=not args.no_backup, force=args.force)
    print(("成功：" if o.ok else "失败：") + o.detail)
    for r in o.reasons:
        print("  " + r)
    if o.data.get("backup"):
        print("  备份：" + o.data["backup"])
    return RC_OK if o.ok else RC_FAIL


def cmd_gplay(args, log) -> int:
    if not gp_ui_enabled():
        print("这个构建没有集成 Google Play。")
        print("要装商店请用集成版（build.py --gp 产出的 Leaf5Toolkit_gplay.exe），"
              "或自己 python build.py --gp")
        return RC_USAGE
    cfg, adb, _fb = _ctx(args, log)
    gp = GPlay(adb, log)

    if args.action == "state":
        st = gp.state()
        print("商店      : %s" % (st["version"] or "未安装"))
        print("GMS       : %s" % ("有" if st["gms"] else "缺"))
        print("GSF       : %s" % ("有" if st["gsf"] else "缺"))
        print("前台      : %s" % (st["foreground"] or "-"))
        return RC_OK

    if args.action == "install":
        apk = Path(args.apk) if args.apk else find_apk(cfg.base_dir)
        log.info("用 %s" % apk)
        gp.install(apk, reinstall=not args.no_reinstall)
        print("装好了：" + (adb.version_name("com.android.vending") or "版本读不到"))
        return RC_OK

    o = gp.enable(apk=args.apk, base_dir=cfg.base_dir,
                  reinstall_if_blocked=not args.no_reinstall)
    print(("成功：" if o.ok else "未完成：") + o.detail)
    for r in o.reasons:
        print("  " + r)
    if o.stage == "need_login":
        print("\n在设备上：设置 → 账号 → 添加 Google 账号 → 登录并完成验证，")
        print("然后重跑 `l5tk gplay enable`。验证通过后系统会自己放行商店。")
    return RC_OK if o.ok else RC_FAIL


def cmd_verify(args, log) -> int:
    _cfg, adb, fb = _ctx(args, log)
    ops = RootOps(adb, fb, log)
    checks = []

    st = ops.status()
    checks.append(("root", st.ok, st.detail))

    gp = GPlay(adb, log)
    g = gp.state()
    checks.append(("商店已安装", bool(g["store"]), g["version"] or "未安装"))
    checks.append(("GMS", bool(g["gms"]), "在" if g["gms"] else "缺"))
    checks.append(("GSF", bool(g["gsf"]), "在" if g["gsf"] else "缺"))

    ok, detail = gp.launch_store(adb.cfg.store_settle)
    checks.append(("商店可启动", ok, detail))

    if args.json:
        print(json.dumps([{"name": n, "ok": o, "detail": d} for n, o, d in checks],
                         ensure_ascii=False, indent=2))
    else:
        print("%-14s %-6s %s" % ("检查项", "结果", "详情"))
        for n, o, d in checks:
            print("%-14s %-6s %s" % (n, "通过" if o else "失败", d))
    return RC_OK if all(o for _, o, _ in checks) else RC_FAIL


HANDLERS = {
    "profiles": cmd_profiles, "devices": cmd_devices, "doctor": cmd_doctor,
    "probe": cmd_probe, "root": cmd_root, "gplay": cmd_gplay,
    "verify": cmd_verify,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = make_logger(args)
    try:
        return HANDLERS[args.cmd](args, log)
    except Refused as e:
        log.warn(e.render())
        return RC_DRYRUN
    except AdbError as e:
        # adb 不在/没设备属于环境问题，脚本应当能与「动作失败」区分开
        log.err(e.render())
        return RC_USAGE
    except L5Error as e:
        log.err(e.render())
        return RC_FAIL
    except KeyboardInterrupt:
        log.warn("用户中断")
        return 130
    finally:
        if log.path:
            log.info("日志：%s" % log.path)
        log.close()


if __name__ == "__main__":
    sys.exit(main())
