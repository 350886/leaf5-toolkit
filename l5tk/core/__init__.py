"""core 层：不认机型，只认设备与命令。"""

from .adb import Adb, Device, shell_quote, split_args
from .errors import (AdbError, DeviceError, FastbootError, GpError,
                     ImageError, L5Error, Refused)
from .fastboot import Fastboot, list_boot_images
from .result import ExecResult, Outcome, Step

__all__ = [
    "Adb", "Device", "shell_quote", "split_args",
    "Fastboot", "list_boot_images",
    "ExecResult", "Outcome", "Step",
    "L5Error", "AdbError", "FastbootError", "DeviceError", "ImageError",
    "GpError", "Refused",
]
