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
from .core.payload import Payload, find_ota
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
                               "magisk", "exploit", "auto", "edl", "payload"),
                      help="status=看状态 adb=试 adb root flash=刷 boot "
                           "check=只校验镜像 part=看会刷哪个分区（只读） "
                           "magisk=装内置 Magisk App "
                           "exploit=跑内核载荷拿临时 root "
                           "auto=一键 root（按代价从低到高依次试） "
                           "edl=9008 直写分区（fastboot 被厂商裁掉时用这个） "
                           "payload=从官方 update.zip 里抽原厂 boot 镜像")
    root.add_argument("image", nargs="?", help="flash / edl 用的 boot 镜像")
    root.add_argument("--part", help="分区名（默认按机型档案）")
    root.add_argument("--via", default="auto",
                      choices=("auto", "dd", "fastbootd", "fastboot"),
                      help="flash 的写入通路。auto = 按 dd → fastbootd → "
                           "fastboot 依次选（ONYX 的 abl 裁了 bootloader 的 "
                           "flash，fastbootd 是另一套实现，能过）")
    root.add_argument("--ota", help="payload：官方 update.zip 路径")
    root.add_argument("--out", help="payload：抽出来的镜像放哪（默认 work\\boot）")
    root.add_argument("--gpt", action="store_true", help="edl：只读分区表")
    root.add_argument("--loader", help="edl：firehose programmer 路径（*_fhprg.bin）")
    root.add_argument("--no-backup", action="store_true",
                      help="（已停用）刷前备份无法关闭；传了会直接拒绝并退出")
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

    # ---------------- 固件备份 / 恢复 ----------------
    bk = sub.add_parser("backup", help="固件备份：逐分区 dd + 双端 md5 自证")
    bk.add_argument("--out", default=None,
                    help="放到哪（默认 work\\firmware，每次一个 l5fw_机型_时间 目录）")
    bk.add_argument("--part", default="critical",
                    help="boot / critical / all，或逗号分隔的分区名（boot_b,super）。"
                         "默认 critical = 启动链 + 系统链，不含 userdata")
    bk.add_argument("--plain", action="store_true",
                    help="不压缩，直接留 .img（fastboot 能直刷，占空间大）")

    rs = sub.add_parser("restore", help="从备份目录逐分区写回")
    rs.add_argument("--src", required=True, help="备份目录（含 manifest.json）")
    rs.add_argument("--part", default=None, help="只恢复这些分区（默认全部）")
    rs.add_argument("--no-safety", action="store_true",
                    help="写前不备份分区当前内容（不推荐：写错了没有退路）")
    rs.add_argument("--force", action="store_true",
                    help="镜像比分区小时仍然写（默认会提示但仍写，这个开关只压掉提示）")
    rs.add_argument("--reboot", action="store_true", help="写完自动重启")

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

    a3b = asub.add_parser(
        "fp", help="无 root 采集设备指纹，判断有没有现成可用的 preload.so")
    a3b.add_argument("-o", "--out", default="", help="指纹包保存路径（.json）")
    a3b.add_argument("--no-kallsyms", action="store_true",
                     help="跳过 kallsyms（只取版本串，快但判断力弱）")
    a3b.add_argument("--json", action="store_true", help="输出机器可读的 JSON")

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
    # --no-backup 保留参数名只为不让老脚本报「unrecognized arguments」，
    # 但它不再能关掉备份。写分区没有退路这件事不该做成开关：
    # 跳过的那次记得，写进脚本之后就不记得了。
    if getattr(args, "no_backup", False):
        print("刷前备份无法跳过：这个参数已停用，命令已取消（未动设备）。")
        print("  boot 分区刷坏只能整机重刷，备份是唯一的退路。")
        log.err("--no-backup 已停用，拒绝执行")
        return RC_USAGE

    # payload 只解本地文件，不需要设备 —— 必须在 _ctx 之前分流，
    # 否则没插设备会被判成环境错误，而这件事本来跟设备无关。
    if args.action == "payload":
        return cmd_payload(args, log)

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

    # edl：9008 直写。fastboot 被 ONYX 的 abl 裁掉了，这是替代通道。
    if args.action == "edl":
        from .ops.edl import EdlOps
        eo = EdlOps(adb, log, loader=args.loader)
        if args.gpt:
            o = eo.gpt()
            head = ("成功：" if o.ok else "失败：") + o.detail
            print(head)
            log.info(head)
            for r in o.reasons:
                print("  " + r)
                log.info("  " + r)
            for n, v in (o.data.get("parts") or {}).items():
                line = ("  %-22s off=0x%09X len=0x%09X %s"
                        % (n, v["offset"], v["length"],
                           "ACTIVE" if v.get("active") else ""))
                print(line)
            return RC_OK if o.ok else RC_FAIL
        if not args.image:
            o = eo.status()
            head = ("就绪：" if o.ok else "未就绪：") + o.detail
            print(head)
            log.info(head)
            for r in o.reasons:
                print("  " + r)
                log.info("  " + r)
            print("\n写 boot：l5tk root edl --image <magisk_patched.img> [--part boot_b]")
            print("读分区表：l5tk root edl --gpt")
            return RC_OK if o.ok else RC_FAIL
        o = eo.flash(args.image, part=args.part, backup=True)
        head = ("成功：" if o.ok else "失败：") + o.detail
        print(head)
        # 结论同时落日志：--windowed 的 EXE 在部分终端接不上控制台，
        # 日志是唯一能事后确认「到底写了没有、校验过没过」的地方
        log.info(head)
        for r in o.reasons:
            print("  " + r)
            log.info("  " + r)
        if o.data.get("backup"):
            print("  备份：" + o.data["backup"])
            log.info("  备份：" + o.data["backup"])
        if o.data.get("readback"):
            print("  读回：" + o.data["readback"])
            log.info("  读回：" + o.data["readback"])
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
                       backup=True, force=args.force,
                       via=args.via)
    print(("成功：" if o.ok else "失败：") + o.detail)
    for r in o.reasons:
        print("  " + r)
    if o.data.get("via"):
        print("  通路：%s" % o.data["via"])
    if o.data.get("partition"):
        print("  分区：%s" % o.data["partition"])
    if o.data.get("backup"):
        print("  备份：" + o.data["backup"])
    return RC_OK if o.ok else RC_FAIL


def cmd_payload(args, log) -> int:
    """从官方 update.zip 里抽出原厂 boot。

    这是「刷 boot 的镜像从哪来」的答案：不用自己备份，官方包里就有。
    纯本地解包，不碰设备、不需要 root、不会改任何东西。
    """
    cfg, _adb, _fb = _ctx(args, log)
    ota = args.ota
    if not ota:
        cands = find_ota(cfg.base_dir / "firmware") or find_ota(Path.cwd())
        if not cands:
            print("要给固件包路径：l5tk root payload --ota update.zip")
            print("（把 update.zip 放进 EXE 同级的 firmware\\ 也能自动找到）")
            return RC_USAGE
        ota = str(cands[0])
        print("自动选用：%s" % ota)
    if not Path(ota).is_file():
        print("找不到固件包：%s" % ota)
        return RC_FAIL

    out = Path(args.out) if args.out else (cfg.work_dir / "boot")
    try:
        with Payload(ota) as p:
            print("payload v%d，%d 个分区，块 %d 字节"
                  % (p.version, len(p.manifest.partitions),
                     p.manifest.block_size))
            for part in p.manifest.partitions:
                print("  " + part.describe())

            boot = p.find_boot()
            if boot is None:
                print("\n包里既没有 boot 也没有 init_boot")
                return RC_FAIL
            print("\n打补丁目标：%s（%.1f MB）"
                  % (boot.name, boot.size / 1048576.0))

            # 重定向到文件/管道时 \r 会堆成一大片，只在真终端里画进度条。
            tty = sys.stdout.isatty()

            def prog(done, total, label):
                if tty and total:
                    print("\r  解包 %5.1f%%  %-12s"
                          % (done * 100.0 / total, label), end="", flush=True)

            info = p.extract(boot.name,
                             out / ("%s_stock.img" % boot.name), progress=prog)
            if tty:
                sys.stdout.write("\r" + " " * 40 + "\r")
                sys.stdout.flush()
    except L5Error as e:
        print(e.render())
        return RC_FAIL

    print("\n已写出：%s（%.1f MB）"
          % (info["path"], info["bytes"] / 1048576.0))
    print("这是原厂镜像：Magisk 打补丁要的就是它，刷坏了也用它刷回来。")
    print("下一步：l5tk root flash %s --via fastbootd" % info["path"])
    return RC_OK


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
            # --device 给了就按它重算，覆盖自动读到的那台
            v, why, adv = kimage.banner_match(res["image"], args.device)
            res.update(device=args.device.strip(), verdict=v, why=why, advice=adv)
        # 结论必须是独立一行、不带日志时间戳：这一个词决定「能不能直接用
        # 这份镜像生成 target.h」，藏在 INFO 日志里等于没给。
        tag = kimage.VERDICT_TAG.get(res["verdict"], "[?]")
        print("\n结论：%s %s" % (tag, res["why"]))
        if res["verdict"] != "same":
            print("      %s" % res["advice"])
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

    if act == "fp":
        # 只读：读几个 /proc 文件 + /proc/kallsyms 的符号名，不碰设备状态。
        # 能回答的问题只有一个：「这台机器的内核，是不是我们已经有 target 的那个」。
        from .core import fp as fpm

        try:
            dev = fpm.collect(serial=args.serial or "", adb=cfg.adb,
                              log=log.info,
                              want_kallsyms=not args.no_kallsyms)
        except L5Error as e:
            print(e.render())
            return RC_FAIL
        targets = [dict(t.meta, id=t.id) for t in a.list_targets()]
        res = fpm.match(dev, targets)

        if args.json:
            print(json.dumps({"fp": dev, "match": res}, ensure_ascii=False,
                             indent=2))
        else:
            print("\n" + fpm.summary(dev))
            tag = {"same_build": "[OK]", "same_version": "[!]",
                   "different": "[X]", "no_target": "[?]"}.get(
                       res["verdict"], "[?]")
            print("\n结论：%s %s" % (tag, res["why"]))
            print("      %s" % res["advice"])
            if res.get("id"):
                print("命中 target：%s" % res["id"])
        if args.out:
            p = fpm.save(dev, args.out)
            print("\n指纹包：%s" % p)
        # 只有「同一次构建」才算可以零成本直接用；其余都要人再确认
        return RC_OK if res["verdict"] == "same_build" else RC_FAIL

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
            if not r.auto:
                # 纯 kallsyms 文本只有地址，没有机器码 —— 那 4 个值就写在
                # 指令立即数上，没有字节可扫。这句话是省掉一次白折腾。
                print("\n来源里没有机器码（纯符号表），所以自动定标没跑。"
                      "同一份内核给 boot.img 或裸 Image，这 4 个值能自动扫出来。")
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


def cmd_backup(args, log) -> int:
    from .ops.firmware import backup

    cfg, adb, _fb = _ctx(args, log)
    out = Path(args.out) if args.out else (cfg.work_dir / "firmware")
    dest = backup(adb, log, out, args.part, gz=not args.plain,
                  dry_run=cfg.dry_run)
    if cfg.dry_run:
        return RC_DRYRUN
    print("备份目录：%s" % dest)
    print("恢复：l5tk restore --src \"%s\"" % dest)
    return RC_OK


def cmd_restore(args, log) -> int:
    from .ops.firmware import restore

    cfg, adb, _fb = _ctx(args, log)
    rows = restore(adb, log, args.src, only=args.part,
                   safety=not args.no_safety, force=args.force,
                   dry_run=cfg.dry_run)
    if cfg.dry_run:
        return RC_DRYRUN
    bad = [r for r in rows if r[1] != "ok"]
    if bad:
        for name, _st, why in bad:
            log.err("%s：%s" % (name, why))
        return RC_FAIL
    if args.reboot:
        adb.sh("reboot", timeout=cfg.timeout_short, root=True)
        log.info("已下发重启")
    else:
        print("已恢复。写过分区的必须重启才生效：adb reboot")
    return RC_OK


HANDLERS = {
    "profiles": cmd_profiles, "devices": cmd_devices, "doctor": cmd_doctor,
    "probe": cmd_probe, "root": cmd_root, "gplay": cmd_gplay,
    "verify": cmd_verify, "adapt": cmd_adapt,
    "backup": cmd_backup, "restore": cmd_restore,
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
