"""打包。

    python build.py            出两个版本（普通 + 集成商店）
    python build.py --no-gp    只出普通版
    python build.py --gp       只出集成版
    python build.py --sign     只给已有产物签名
    python build.py --clean    打包前删 build/ dist/

两个版本的唯一差别是一个空标记文件 `assets/gp_ui.on`：
集成版把它打进包里，`l5tk/flags.py` 读到就在界面上多一行「装 Google Play」。
用标记文件而不是「包里有没有 APK」，是因为用户把 APK 丢进同目录是常规操作，
按有没有 APK 判断会让普通版突然长出按钮。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "Leaf5Toolkit"
GP_NAME = "Leaf5Toolkit_gplay"
SIGN_CN = "SWT"          # 证书显示名；签名时按 Subject 精确匹配
VERSION = (2, 0, 0, 0)
COMPANY = "local"
PRODUCT = "Leaf5 Toolkit"
ENTRY = ROOT / "run_app.py"


# ---------------------------------------------------------------- 版本资源
def _vstr(v: tuple[int, ...]) -> str:
    return "%d.%d.%d.%d" % v


def ver_file(out_dir: Path) -> Path:
    """PyInstaller 的 --version-file。少了这个，文件属性里什么信息都没有。"""
    v = _vstr(VERSION)
    body = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={tup}, prodvers={tup}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', '{company}'),
      StringStruct('FileDescription', '{product}'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', '{name}'),
      StringStruct('LegalCopyright', ''),
      StringStruct('OriginalFilename', '{name}.exe'),
      StringStruct('ProductName', '{product}'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)""".format(tup=VERSION, company=COMPANY, product=PRODUCT, v=v, name=NAME)
    p = out_dir / "version_info.txt"
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------- 资产
def gp_flag_file(out_dir: Path) -> Path:
    """空标记。只有集成版会 --add-data 它。"""
    p = out_dir / "gp_ui.on"
    p.write_bytes(b"")
    return p


def _data_args(gp: bool, work: Path) -> list[str]:
    args: list[str] = []
    for sub in ("bin",):
        d = ROOT / "assets" / sub
        if d.is_dir() and any(d.iterdir()):
            args += ["--add-data", "%s;assets/%s" % (d, sub)]
    if gp:
        apk_dir = ROOT / "assets" / "apk"
        if apk_dir.is_dir() and any(apk_dir.glob("*.apk")):
            args += ["--add-data", "%s;assets/apk" % apk_dir]
        args += ["--add-data", "%s;assets" % gp_flag_file(work)]
    return args


# ---------------------------------------------------------------- 打包
def check_tkinter() -> None:
    """打包前先确认解释器带 tkinter。

    不带也能打成功 —— PyInstaller 不会因为 import 不到 tkinter 而失败，
    于是得到一个「双击闪退、无任何提示」的 EXE。这种坑必须在这里提前挡住。
    """
    try:
        import tkinter  # noqa: F401
    except ImportError:
        raise SystemExit(
            "当前解释器没有 tkinter，打出来的 EXE 无法开窗：\n  %s\n"
            "换成官方 Python（带 tkinter）再打，例如：\n"
            r"  C:\Users\<你>\AppData\Local\Programs\Python\Python312\python.exe build.py"
            % sys.executable)


def pyinstaller() -> list[str]:
    """固定用「当前解释器 + -m PyInstaller」。

    不能用 PATH 里的 pyinstaller：那可能属于另一个 Python，
    打出来的包会缺当前解释器才有的东西（tkinter 就是这么丢的）。
    """
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit("当前解释器没装 PyInstaller：\n  %s -m pip install pyinstaller"
                         % sys.executable)
    return [sys.executable, "-m", "PyInstaller"]


def build_one(name: str, gp: bool, clean: bool) -> Path | None:
    work = ROOT / "build"
    work.mkdir(exist_ok=True)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)

    tool = pyinstaller()
    cmd = list(tool)

    cmd += [
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name", name,
        "--distpath", str(dist),
        "--workpath", str(work),
        "--specpath", str(work),
        "--version-file", str(ver_file(work)),
        "--paths", str(ROOT),
    ]
    if clean:
        cmd.append("--clean")
    icon = ROOT / "assets" / "icon.ico"
    if icon.is_file():
        cmd += ["--icon", str(icon)]
    cmd += _data_args(gp, work)
    cmd.append(str(ENTRY))

    print("[*] 打包 %s%s" % (name, "（内置商店 APK）" if gp else "（不带商店）"))
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print("[!] %s 打包失败（rc=%d）" % (name, r.returncode))
        return None
    exe = dist / (name + ".exe")
    if not exe.is_file():
        print("[!] 没找到产物 %s" % exe)
        return None
    print("[+] %s  %.1f MB" % (exe.name, exe.stat().st_size / 1048576))
    return exe


# ---------------------------------------------------------------- 签名
def _signtool() -> str | None:
    cands = sorted(Path(r"C:\Program Files (x86)\Windows Kits\10\bin").glob(
        "*/x64/signtool.exe")) if Path(r"C:\Program Files (x86)\Windows Kits\10\bin").is_dir() else []
    if cands:
        return str(cands[-1])
    return shutil.which("signtool")


def sign(exe: Path | str) -> int:
    exe = Path(exe)
    tool = _signtool()
    if not tool:
        print("[!] 没找到 signtool，跳过签名")
        return 1

    want = os.environ.get("L5TK_SIGN_CN", "CN=" + SIGN_CN).strip()
    if not want.upper().startswith("CN="):
        want = "CN=" + want
    lit = want.replace("'", "''")
    # 证书库里通常不止一张证书。Select -First 1 抓到哪张取决枚举顺序，
    # 会把发布者签成别人，所以必须按 Subject 点名，找不到才回落。
    ps = (
        "$all = Get-ChildItem Cert:\\CurrentUser\\My -CodeSigningCert "
        "| Where-Object { $_.HasPrivateKey }; "
        f"$t = $all | Where-Object {{ $_.Subject -eq '{lit}' }} | Select-Object -First 1; "
        "if (-not $t) { $t = $all | Select-Object -First 1 }; "
        "if ($t) { $t.Subject + '|' + $t.Thumbprint }"
    )
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
        out = (p.stdout or "").strip()
    except Exception:
        out = ""
    if not out:
        print("[!] 当前用户证书库没有代码签名证书，跳过签名")
        return 1
    used, _, thumb = out.partition("|")
    if used.strip() != want:
        print("[!] 没找到 %s，回落到 %s" % (want, used.strip()))
    print("[*] 签名证书：%s" % used.strip())

    # 经 PowerShell 调，避开 Git Bash 把 /sha1 /fd 当路径改写
    cmd = ("& '%s' sign /sha1 %s /fd SHA256 /tr http://timestamp.digicert.com "
           "/td SHA256 '%s'" % (tool, thumb.strip(), exe))
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, timeout=180)
    print((r.stdout or r.stderr).strip()[:400])
    if r.returncode == 0:
        print("[+] 已签名")
    return r.returncode


# ---------------------------------------------------------------- 入口
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-gp", action="store_true", help="只出普通版")
    ap.add_argument("--gp", action="store_true", help="只出集成版")
    ap.add_argument("--all", action="store_true", help="两个都出（默认）")
    ap.add_argument("--sign", action="store_true", help="只给 dist/ 里现有产物签名")
    ap.add_argument("--no-sign", action="store_true", help="打包后不签名")
    ap.add_argument("--clean", action="store_true", help="打包前清 build/")
    a = ap.parse_args()

    if not ENTRY.is_file():
        ENTRY.write_text(
            "import sys\nfrom l5tk.ui.app import run\nsys.exit(run())\n",
            encoding="utf-8")

    check_tkinter()

    if a.sign:
        rc = 0
        for exe in sorted((ROOT / "dist").glob("*.exe")):
            print("[*] " + exe.name)
            rc |= sign(exe)
        return rc

    if a.clean:
        for d in (ROOT / "build", ROOT / "dist"):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                print("[*] 清掉 %s" % d)

    if a.gp and not a.no_gp:
        do_plain, do_gp = False, True
    elif a.no_gp and not a.gp:
        do_plain, do_gp = True, False
    else:
        do_plain = do_gp = True

    built: list[Path] = []
    if do_plain:
        exe = build_one(NAME, gp=False, clean=a.clean)
        if exe:
            built.append(exe)
    if do_gp:
        exe = build_one(GP_NAME, gp=True, clean=a.clean)
        if exe:
            built.append(exe)

    if not built:
        return 1

    rc = 0
    if not a.no_sign:
        for exe in built:
            if sign(exe) != 0:
                print("[!] %s 签名失败，EXE 仍可用（只是没有数字签名）" % exe.name)
                rc = 3

    print("\n[*] 产物：")
    for exe in built:
        print("    %-28s %6.1f MB" % (exe.name, exe.stat().st_size / 1048576))
    return rc


if __name__ == "__main__":
    sys.exit(main())
