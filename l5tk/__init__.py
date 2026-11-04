"""l5tk —— 多机型 Android 工具箱（V2）。

V1 是一个 2500 行的单文件 GUI，能力绑死在 ONYX Leaf5 一台机器上。
V2 把能力拆成四层，层与层之间只有数据类往来：

    core/    adb、fastboot、结果结构、错误类型（不认机型）
    device/  机型档案与识别（只认 ro.* 属性，不碰设备）
    ops/     画像、Root、Google Play 这些动作（组合 core + device）
    ui/cli   入口，只负责参数与展示

硬约束：
  * core 与 device 不 import ops，ops 不 import ui —— 拿 import 图就能验
  * 所有可调值走 config.Config，环境变量可覆盖，不写死在函数里
  * 失败一律抛 core.errors 里的异常，不返回魔法字符串
"""

__version__ = "2.4.8"
__all__ = ["__version__"]
