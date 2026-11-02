"""测试用的假 adb。

不起进程、不联网：把 Adb(cfg, runner=fake_runner) 注进来，
runner 收到 (argv, timeout) 返回 ExecResult。这样测的是真解析逻辑。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from l5tk.config import load as load_cfg          # noqa: E402
from l5tk.core.result import ExecResult            # noqa: E402

LEAF5_PROPS = {
    "ro.product.model": "Leaf5",
    "ro.product.device": "Leaf5",
    "ro.product.name": "Leaf5",
    "ro.product.brand": "Onyx",
    "ro.build.version.release": "13",
    "ro.build.version.sdk": "33",
    "ro.build.type": "user",
    "ro.build.display.id": "2026-09-28_15-24_4.2.3-rel_0928_656b72278",
    "ro.board.platform": "lito",
    "ro.boot.flash.locked": "0",
    "ro.boot.verifiedbootstate": "orange",
    "ro.boot.slot_suffix": "_b",       # 实测：Leaf5 是 A/B，当前槽 b
    "ro.serialno": "LEAF5TEST01",
}

TAB8C_PROPS = {
    "ro.product.model": "Tab8C",
    "ro.product.device": "tab8c",
    "ro.product.name": "BOOX_Tab8C",
    "ro.build.version.release": "11",
    "ro.build.version.sdk": "30",
    "ro.build.type": "userdebug",
    "ro.build.display.id": "3.5.4-rel_0512",
    "ro.board.platform": "bengal",
    "ro.boot.flash.locked": "0",
    "ro.boot.verifiedbootstate": "orange",
    "ro.boot.slot_suffix": "",
    "ro.serialno": "TAB8C0001XY",
}


def props_text(props: dict[str, str]) -> str:
    return "\n".join("[%s]: [%s]" % (k, v) for k, v in props.items())


def ok(argv, out="", err="", rc=0) -> ExecResult:
    return ExecResult(tuple(argv), rc, out, err, 5)


class FakeAdb:
    """按子命令分发。任何没预设的命令返回空成功，方便逐步补。"""

    def __init__(self, props=None, serial="ABCD1234EFGH", state="device",
                 packages=None, top="", su=False, accounts="",
                 su_forms=None, su_timed_out=False, magisk="",
                 partitions=("boot_a", "boot_b", "vbmeta_a", "vbmeta_b")):
        self.props = dict(props or LEAF5_PROPS)
        self.serial = serial
        self.state = state
        self.packages = dict(packages or {})
        self.top = top
        self.su = su
        # su=True 时默认只认 Magisk 的 `su -c`；传 su_forms 可模拟只认老式 `su 0`
        self.su_forms = su_forms if su_forms is not None else {("su", "-c")}
        self.su_timed_out = su_timed_out
        self.magisk = magisk
        self.accounts = accounts
        self.partitions = tuple(partitions)
        self.calls: list[tuple[str, ...]] = []
        self.fail_on: set[str] = set()

    def __call__(self, argv, timeout):
        argv = tuple(str(a) for a in argv)
        self.calls.append(argv)
        args = list(argv[1:])
        # 去掉 -s serial
        if args[:1] == ["-s"]:
            args = args[2:]
        head = args[0] if args else ""
        joined = " ".join(args)

        if head in self.fail_on:
            return ok(argv, "", "injected failure", 1)
        if head == "devices":
            if not self.state:
                return ok(argv, "List of devices attached\n")
            return ok(argv, "List of devices attached\n%s\t%s\n" % (self.serial, self.state))
        if head == "get-state":
            return ok(argv, "device\n")
        if head == "getprop":
            if len(args) == 1:
                return ok(argv, props_text(self.props) + "\n")
            return ok(argv, self.props.get(args[1], "") + "\n")
        if head == "shell":
            # 全量 getprop 必须在分发到 _shell 之前拦掉，否则会被当成单键查询
            if args[1:] == ["getprop"]:
                return ok(argv, props_text(self.props) + "\n")
            return self._shell(argv, args[1:])
        if head == "install":
            self.packages["com.android.vending"] = "/data/app/~~x/vending/base.apk"
            return ok(argv, "Success\n")
        if head == "pull":
            Path(args[2]).write_bytes(b"\x00" * (3 * 1024 * 1024))
            return ok(argv, "[100%] /data/local/tmp/boot_backup.img\n")
        if head == "root":
            return ok(argv, "restarting adbd as root\n")
        return ok(argv, "")

    def _shell(self, argv, rest):
        if not rest:
            return ok(argv, "")
        if rest[0] == "ls" and "/dev/block/by-name" in "".join(rest):
            return ok(argv, " ".join(self.partitions) + "\n")
        if rest[0] == "getprop":
            return ok(argv, self.props.get(rest[1] if len(rest) > 1 else "", "") + "\n")
        if rest[:2] == ["pm", "path"]:
            p = self.packages.get(rest[2], "")
            return ok(argv, ("package:%s\n" % p) if p else "", rc=0 if p else 1)
        if rest[:2] == ["dumpsys", "package"]:
            pkg = rest[2] if len(rest) > 2 else ""
            if pkg in self.packages:
                return ok(argv, "Packages:\n  versionName=42.0.18-31\n")
            return ok(argv, "")
        if rest[:2] == ["dumpsys", "activity"]:
            body = ("ACTIVITY MANAGER ACTIVITIES\n"
                    "  topResumedActivity=ActivityRecord{1 u0 %s t1}\n" % self.top
                    if self.top else "ACTIVITY MANAGER ACTIVITIES\n")
            return ok(argv, body)
        if rest[:2] == ["dumpsys", "account"]:
            return ok(argv, self.accounts)
        if rest[:2] == ["am", "start"] or rest[0] == "monkey":
            return ok(argv, "Starting: Intent\nStatus: ok\n" if self.top else "Starting: Intent\n")
        if rest[:2] == ["cat", "/proc/version"]:
            return ok(argv, "Linux version 5.10.66 (build@host) #38 SMP PREEMPT\n")
        if rest and rest[0].split("/")[-1] == "magisk" and rest[1:2] == ["-v"]:
            if self.magisk:
                return ok(argv, self.magisk + "\n")
            return ok(argv, "inaccessible or not found\n", rc=127)
        if rest and rest[0] == "su":
            # 只比前两段：`su -c id` 和 `su -c "dd if=..."` 是同一形式
            form = tuple(rest[:2])
            if self.su_timed_out:
                return ExecResult(argv, -1, "", "", 15000, True)
            if self.su and form in self.su_forms:
                return ok(argv, "uid=0(root) gid=0(root) groups=0(root) context=u:r:magisk:s0\n")
            return ok(argv, "su: permission denied\n", rc=1)
        if rest[:2] == ["stat", "-c"]:
            return ok(argv, "67108864\n")
        return ok(argv, "")


@pytest.fixture()
def cfg(tmp_path):
    c = load_cfg()
    c.adb = "adb"
    c.fastboot = "fastboot"
    c.work_dir = tmp_path / "work"
    c.log_dir = tmp_path / "logs"
    c.ensure_dirs()
    return c


@pytest.fixture()
def fake():
    return FakeAdb()
