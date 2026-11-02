"""异常类型。所有对外失败都从这里出，调用方按类型分流。"""

from __future__ import annotations


class L5Error(Exception):
    """基类。message 直接给人看，所以写成整句中文。"""

    def __init__(self, message: str, *, hint: str = "", code: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code or type(self).__name__

    def render(self) -> str:
        return self.hint and "%s\n  → %s" % (self.message, self.hint) or self.message


class AdbError(L5Error):
    """adb 不可用、无设备、命令非零退出。"""


class FastbootError(L5Error):
    """fastboot 不可用或设备不在 bootloader。"""


class DeviceError(L5Error):
    """设备状态不满足操作前提（未解锁、未 root、槽位不对）。"""


class ImageError(L5Error):
    """镜像文件不合法（magic 不对、截断、目标机型不匹配）。"""


class GpError(L5Error):
    """Google Play 安装/启用失败。"""


class Refused(L5Error):
    """dry-run 下拒绝真动作。不是错误用法，是设计行为。"""
