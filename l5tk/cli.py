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
import time
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
    p.add_argument("--port", type=int, default=0,
                   help="adb server 端口（默认 5037；内嵌与系统 adb 打架时换一个）")
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
                               "magisk", "exploit", "auto"),
                      help="status=看状态 adb=试 adb root flash=刷 boot "
                           "check=只校验镜像 part=看会刷哪个分区（只读） "
                           "magisk=装内置 Magisk App "
                           "exploit=跑内核载荷拿临时 root "
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
    root.add_argument("--force-kernel", action="store_true",
                      help="exploit/auto：内核版号不匹配也跑（偏移不对会 kernel panic）")
    root.add_argument("--sysadmin", action="store_true",
                      help="exploit：额外要求 CAP_SYS_ADMIN（改只读挂载需要它）")
    root.add_argument("--force-rerun", action="store_true",
                      help="exploit：系统已有 su 也照样跑一轮（默认跳过）")
    root.add_argument("--force", action="store_true",
                      help="档案未验证/已上锁时仍然执行")

    gp = sub.add_parser("gplay", help="Google Play")
    gp.add_argument("action", choices=("state", "install", "enable"),
                    help="state=看状态 install=只装 enable=装+拉起+判定")
    gp.add_argument("--apk", help="指定 APK 路径")
    gp.add_argument("--no-reinstall", action="store_true", help="已装时不重装")

    vf = sub.add_parser("verify", help="验收：root / 商店 / GMS 逐项过")
    vf.add_argument("--json", action="store_true")

    # ---------------- 内核适配 ----------------
    ad = sub.add_parser(
        "adapt", help="内核适配向导：换固件后重新生成 preload.so")
    asub = ad.add_subparsers(dest="act", required=True)

    a1 = asub.add_parser("scan", help="在本机找现成的内核镜像")
    a1.add_argument("--disk", action="store_true", help="全盘扫（默认只扫常见目录）")
    a1.add_argument("--seconds", type=float, default=0, help="扫描预算（秒）")
    a1.add_argument("--depth", type=int, default=0, help="目录深度")

    a2 = asub.add_parser("banner", help="读镜像里的 linux_banner，跟设备对一下")
    a2.add_argument("path", help="boot.img / 裸 Image / vmlinux.elf 都行")
    a2.add_argument("--device", default="",
                    help="设备不在时手工贴 /proc/version 的内容")

    a3 = asub.add_parser("kallsyms", help="从设备导出 /proc/kallsyms（需要 su）")
    a3.add_argument("-o", "--out", default="")

    a4 = asub.add_parser("gen", help="镜像/kallsyms → targets/<id>/target.h")
    a4.add_argument("src", help="boot.img / Image / vmlinux.elf / kallsyms.txt")
    a4.add_argument("--id", default="", help="target id（默认按内核串自动起名）")
    a4.add_argument("--fingerprint", default="")
    a4.add_argument("--kernel", default="")
    a4.add_argument("--keep", action="store_true",
                    help="保留模板原值，只报告差异（同版本重编用）")
    a4.add_argument("--no-inherit", action="store_true",
                    help="同源也强制留 4 个手工占位符")
    a4.add_argument("--inherit", action="store_true",
                    help="明知与模板有差异也沿用模板的 4 个手工宏"
                         "（你确认这台设备的结构体布局与模板一致时才用）")

    a5 = asub.add_parser("fill", help="手填 target.h 里剩下的手工宏")
    a5.add_argument("tid")
    a5.add_argument("--set", action="append", default=[], metavar="MACRO=VALUE",
                    help="可重复；VALUE 收 0x 或十进制")

    a6 = asub.add_parser("build", help="编译 preload.so（首次会下 NDK）")
    a6.add_argument("tid")
    a6.add_argument("-o", "--out", default="")
    a6.add_argument("--cc", default="", help="clang 路径（默认自动找/下载 NDK）")

    asub.add_parser("list", help="列出已适配的 target")

    a7 = asub.add_parser("install", help="把编好的 so 收进 target 仓库")
    a7.add_argument("so")
    a7.add_argument("--id", required=True)
    a7.add_argument("--fingerprint", default="")
    a7.add_argument("--kernel", default="")

    a8 = asub.add_parser("use", help="指定 root 时用哪份 preload.so")
    a8.add_argument("tid")

    asub.add_parser("status", help="当前设备命中哪个 target")

    return p


def make_logger(args) -> Logger:
    cfg = load_cfg(serial=args.serial, adb=args.adb, fastboot=args.fastboot,
                   plain_log=True if args.log_plain else None,
                   dry_run=True if args.dry_run else None)
    if getattr(args, "port", 0):
        cfg.adb_port = args.port
    cfg.ensure_dirs()
    return Logger(cfg.log_dir, plain=cfg.plain_log, echo=not args.quiet)


def _ctx(args, log: Logger):
    cfg = load_cfg(serial=args.serial, adb=args.adb, fastboot=args.fastboot,
                   plain_log=True if args.log_plain else None,
                   dry_run=True if args.dry_run else None)
    cfg.ensure_dirs()
    if getattr(args, "port", 0):
        cfg.adb_port = args.port
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

    # 适配过的 target：换固件的机器靠它跑内核路径，没适配就只有出厂那一版能用
    try:
        from .ops.adapt import Adapter
        ts = Adapter(cfg, log, adb, serial=args.serial or "").list_targets()
        withso = [t for t in ts if t.has_so()]
        print("%-10s %-6s %s" % ("适配", "OK" if withso else "无",
                                 "%d 个 target（%d 个已编译）%s"
                                 % (len(ts), len(withso),
                                    "：" + ", ".join(t.id for t in withso[:3])
                                    if withso else " —— 换过固件就跑 adapt")))
    except L5Error:
        pass

    print("日志目录：%s" % cfg.log_dir)
    print("工作目录：%s" % cfg.work_dir)
    print("适配目录：%s" % cfg.adapt_root)
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
                allow_fastboot=not args.no_fastboot,
                force_kernel=args.force_kernel)
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

    if args.action == "exploit":
        # 单独跑一轮：给「想自己盯着日志」的人，也是一键流程里那一步的入口
        from .ops.exploit import ExploitOps
        try:
            o = ExploitOps(adb, log).run_once(
                force=args.force_kernel, require_sysadmin=args.sysadmin,
                force_rerun=args.force_rerun)
        except L5Error as e:
            print(e.render())
            return RC_FAIL
        print(("成功：" if o.ok else "失败：") + o.detail)
        for r in o.reasons:
            print("  " + r)
        for k in ("kernel", "capeff", "bnd", "id", "log"):
            if o.data.get(k):
                print("  %-8s %s" % (k + ":", o.data[k]))
        return RC_OK if o.ok else RC_FAIL

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


def cmd_adapt(args, log) -> int:
    """内核适配。动作之间互相独立，中间产物都落在 cfg.adapt_root 里。"""
    from .core import kimage
    from .ops.adapt import Adapter, MANUAL_MACROS, fill_manuals, placeholders

    cfg, adb, _fb = _ctx(args, log)
    a = Adapter(cfg, log, adb, serial=args.serial or "")
    act = args.act

    if act == "scan":
        kw = {}
        if args.seconds:
            kw["budget"] = args.seconds
        if args.depth:
            kw["max_depth"] = args.depth
        if args.disk:
            usable, others = kimage.scan_disks(log=log.info, **kw)
        else:
            usable, others = kimage.scan_kernels(
                roots=kimage.scan_roots([str(a.root)]), log=log.info, **kw)
        if usable:
            print("%-8s %-10s %s" % ("大小", "类型", "路径"))
            for i in usable:
                print("%-8s %-10s %s"
                      % (kimage.human_size(i["size"]), i["kind"], i["path"]))
                if i.get("note"):
                    print("         %s" % i["note"])
        else:
            print("没找到能直接用的内核镜像。")
        if others:
            print("\n找到了但用不上（strip 过 / 解不开）：")
            for i in others[:10]:
                print("  %-8s %-6s %s\n         %s"
                      % (kimage.human_size(i["size"]), i["kind"],
                         i["path"], i["note"]))
        if usable:
            print("\n下一步：l5tk adapt gen \"<路径>\"")
        else:
            print("\n两条路：把设备 boot 分区的镜像拖进来（boot.img 也能现场反解），")
            print("        或设备已 root 时 l5tk adapt kallsyms")
        return RC_OK if usable else RC_FAIL

    if act == "banner":
        res = kimage.banner_check(args.path, adb=cfg.adb,
                                  serial=args.serial or "", log=log.info)
        if not res:
            print("[X] 这个文件里找不到 linux_banner —— 不是完整内核镜像")
            return RC_FAIL
        if args.device:
            v, why, adv = kimage.banner_match(res["image"], args.device)
            res.update(device=args.device.strip(), verdict=v, why=why, advice=adv)
            print("\n结论：%s\n      %s" % (why, adv))
        elif res["verdict"] == "unknown":
            print("\n" + res["advice"])
        print("\n镜像 banner：%s" % res["image"])
        if res.get("device"):
            print("设备 version：%s" % res["device"])
        if res["verdict"] == "different":
            print("\n[!] 这份镜像不是设备上正在跑的内核，别用它生成 target.h")
            return RC_FAIL
        return RC_OK

    if act == "kallsyms":
        out = args.out or str(a.src_root / ("kallsyms-%s.txt"
                                            % time.strftime("%Y%m%d-%H%M%S")))
        try:
            path, st = kimage.grab_kallsyms(out, adb=cfg.adb,
                                            serial=args.serial or "", log=log.info)
        except L5Error as e:
            print(e.render())
            return RC_FAIL
        print("\n%s" % path)
        print("  行数 %d   符号 %d   %s   _text 0x%016X"
              % (st["lines"], st["syms"], kimage.human_size(st["size"]), st["text"]))
        print("下一步：l5tk adapt gen \"%s\"" % path)
        return RC_OK

    if act == "gen":
        if not Path(args.src).is_file():
            print("文件不存在：%s" % args.src)
            return RC_USAGE
        try:
            r = a.gen(args.src, fingerprint=args.fingerprint,
                      kernel=args.kernel, tid=args.id or None,
                      inherit_manual=not args.no_inherit,
                      force_inherit=args.inherit, keep=args.keep,
                      log=log.info)
        except L5Error as e:
            print(e.render())
            return RC_FAIL
        print("\ntarget      : %s" % r.tid)
        print("目录        : %s" % r.tdir)
        print("符号偏移    : %d 个（%d 个宏无同名符号）" % (r.offsets, len(r.missing)))
        print("与模板差异  : %d 个宏" % len(r.diffs))
        print("手工宏      : %s"
              % {"inherited": "沿用模板值（同源构建）",
                 "forced": "沿用模板值（--inherit 强制）",
                 "template": "待填"}.get(r.manual, r.manual))
        if r.auto:
            print("自动定标    : %s"
                  % "   ".join("%s=0x%X" % (k, v) for k, v in r.auto.items()))
        if r.banner:
            print("镜像 banner : %s" % r.banner)
        if r.unfilled:
            print("\n还差 %d 个手工宏（指令里读不出来，得人工定标）："
                  % len(r.unfilled))
            for m, hint in MANUAL_MACROS:
                if m in r.unfilled:
                    print("  %-28s %s" % (m, hint))
            print("\n填法：l5tk adapt fill %s --set TASK_CRED_OFF=0x7e0 ..." % r.tid)
        else:
            print("\n下一步：l5tk adapt build %s" % r.tid)
        return RC_OK

    if act == "fill":
        vals = {}
        for item in args.set:
            if "=" not in item:
                print("格式是 MACRO=VALUE：%s" % item)
                return RC_USAGE
            k, v = item.split("=", 1)
            try:
                vals[k.strip()] = int(v.strip(), 0)
            except ValueError:
                print("值不是整数：%s" % item)
                return RC_USAGE
        tdir = a.targets_root / args.tid
        if not (tdir / "target.h").is_file():
            print("没有这个 target：%s" % args.tid)
            return RC_USAGE
        left = fill_manuals(tdir, vals, log=log.info)
        if left:
            print("还差：%s" % ", ".join(left))
            return RC_FAIL
        print("填完了。下一步：l5tk adapt build %s" % args.tid)
        return RC_OK

    if act == "build":
        tdir = a.targets_root / args.tid
        if not (tdir / "target.h").is_file():
            print("没有这个 target：%s" % args.tid)
            return RC_USAGE
        left = placeholders(tdir)
        if left:
            print("target.h 还差 %d 个手工宏：%s" % (len(left), ", ".join(left)))
            return RC_FAIL
        try:
            so, md5, size = a.compile(tdir, out_so=args.out or None,
                                      cc=args.cc or None, log=log.info)
        except L5Error as e:
            print(e.render())
            return RC_FAIL
        t = a.install(args.tid, so, a.device_meta())
        print("\n%s  %d 字节  md5=%s" % (so, size, md5))
        print("已收进 %s" % t.dir)
        print("下一步：l5tk adapt use %s（或直接 root exploit，按设备自动匹配）" % t.id)
        return RC_OK

    if act == "list":
        ts = a.list_targets()
        if not ts:
            print("还没有适配过的 target。")
            print("  换过固件的机器：l5tk adapt gen <boot.img 或 kallsyms.txt>")
            return RC_OK
        act_id = a.active()
        print("%-34s %-8s %-34s %s"
              % ("id", "so", "kernel", "来源"))
        for t in ts:
            mark = "*" if t.id == act_id else " "
            print("%s%-33s %-8s %-34s %s"
                  % (mark, t.id, "有" if t.has_so() else "缺",
                     (t.kernel or "?")[:34],
                     "V1" if t.legacy else "V2"))
        print("\n* = adapt use 指定的；没标 * 时按设备指纹/内核串自动挑")
        return RC_OK

    if act == "install":
        if not Path(args.so).is_file():
            print("文件不存在：%s" % args.so)
            return RC_USAGE
        meta = {"fingerprint": args.fingerprint, "kernel": args.kernel,
                "note": "adapt install 收进来的"}
        t = a.install(args.id, args.so, meta)
        print("已装进 %s（md5=%s）" % (t.dir, t.meta.get("so_md5")))
        return RC_OK

    if act == "use":
        try:
            t = a.use(args.tid)
        except L5Error as e:
            print(e.render())
            return RC_FAIL
        print("root exploit 会用 %s（%s）" % (t.id, t.kernel or "内核串未记录"))
        return RC_OK

    if act == "status":
        dev = a.device_meta()
        if not dev:
            print("读不到设备信息（没插设备或 adb 不通）")
            return RC_USAGE
        print("设备指纹：%s" % (dev.get("fingerprint") or "?"))
        print("内核    ：%s" % (dev.get("kernel") or "?"))
        t = a.pick(fingerprint=dev.get("fingerprint", ""),
                   kernel=dev.get("kernel", ""))
        if t:
            print("命中    ：%s（%s）" % (t.id, "V1 遗产" if t.legacy else "V2"))
            print("          %s" % t.so)
        else:
            print("命中    ：无 —— 这台设备的内核还没有适配过")
            print("          先 adapt gen / adapt kallsyms，再 build")
        act_id = a.active()
        if act_id:
            print("手动指定：%s（优先级高于自动匹配）" % act_id)
        return RC_OK

    print("未知动作：%s" % act)
    return RC_USAGE


HANDLERS = {
    "profiles": cmd_profiles, "devices": cmd_devices, "doctor": cmd_doctor,
    "probe": cmd_probe, "root": cmd_root, "gplay": cmd_gplay,
    "verify": cmd_verify, "adapt": cmd_adapt,
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
