# l5tk — 多机型 Android 工具箱

ONYX BOOX 设备的一条龙运维工具。V1 是单文件 GUI，绑死 Leaf5 一台机器；V2 拆成四层，
加机型 = 加一行数据，不再改七处分支。

```
设备画像  →  机型识别  →  Google Play  →  boot 刷入  →  验收
```

## 和 V1 的差别

| | V1（root-tool） | V2（本项目） |
|---|---|---|
| 代码形态 | 2500 行单文件 GUI + 12 个平铺脚本 | 包结构，四层，层间只传数据类 |
| 机型 | `if "Leaf5" in model` 散在校验、偏移、载荷里 | `device/profiles.py` 一张表 |
| 加一台机器 | 改 7 处，漏一处就刷错分区 | 加一条 `Profile` |
| 出错 | 返回 `(ok, bytes, rc)` 三件套，经常忘看 rc | 抛 `L5Error` 子类，带 `hint` |
| 测试 | 靠真机试 | 67 项，假 adb 注入，不起进程 |

V2 不搬 V1 的 9008 写 flash 那条路：那条路依赖与 SoC 匹配的 firehose programmer，
Leaf5 是 LITO(SM6350)，Tab 系列是 BENGAL(SM6115)，V1 里那两份 programmer 在 Tab 上直接不认。
V2 的 root 路径是 `adb root` → `Magisk 补 boot` → `fastboot 刷`，每一步都能验。

## 安装

```bash
python -m pip install -e .
# 开发用
python -m pip install -e ".[dev]"
```

不装也能跑，项目根目录下 `python -m l5tk ...` 即可。

## 命令

```bash
python -m l5tk profiles                 # 机型档案表
python -m l5tk devices                  # 列 adb 设备
python -m l5tk doctor                   # 环境自检
python -m l5tk probe                    # 设备画像
python -m l5tk probe --json --raw       # 全量 prop，便于补档案
python -m l5tk gplay enable             # 装商店 + 拉起 + 判定
python -m l5tk root status              # root 状态
python -m l5tk root adb                 # 试 adb root（userdebug）
python -m l5tk root check boot.img      # 只校验镜像，不碰设备
python -m l5tk root flash boot.img      # 备份 → 刷入 → 失败自动回滚
python -m l5tk verify                   # 逐项验收
```

退出码：`0` 成功 / `1` 动作失败 / `2` 用法或环境问题 / `3` dry-run 被拒。

全局开关：

```bash
--serial XXX       指定设备（多台时必须给）
--log-plain        日志不打码（排障用）
--dry-run          只检查不动手
```

对应环境变量：`L5TK_SERIAL` / `L5TK_LOG_PLAIN=1` / `L5TK_DRY_RUN=1` /
`L5TK_ADB` / `L5TK_FASTBOOT` / `L5TK_TIMEOUT`。

## 目录

```
l5tk/
├── config.py          全部可调值，环境变量覆盖
├── logx.py            脱敏日志（单一出口）
├── flags.py           构建变体判据（assets/gp_ui.on）
├── core/              不认机型
│   ├── adb.py         subprocess 调 adb，超时/编码/引号
│   ├── fastboot.py    列设备、读变量、刷分区、等回 adb
│   ├── result.py      ExecResult / Step / Outcome
│   └── errors.py      异常类型，message 是人话
├── device/            只回答「这是什么机器」
│   ├── profiles.py    档案表：Leaf5 / Tab8C / Tab8 / Tab10 / Tab 家族兜底
│   ├── detect.py      打分识别，歧义即拒绝
│   └── models.py      Profile / Detection
├── ops/               组合 core + device
│   ├── probe.py       一次取全，矛盾标 warning
│   ├── gplay.py       装/拉起/判定，APK 按目录优先级选
│   └── root.py        boot header 校验、备份、刷入、回滚
└── ui/app.py          tkinter，后台线程 + 队列，不卡界面
```

依赖方向是单向的：`core` 与 `device` 不 import `ops`，`ops` 不 import `ui`。

## 机型档案

| key | 机型 | SoC | Android | 构建 | 实测 |
|---|---|---|---|---|---|
| `leaf5` | Leaf5 | LITO + LAGOON (SM6350) | 13 | user | 是 |
| `tab8c` | Tab8C | BENGAL + SCUBA (SM6115) | 11 | userdebug | 否 |
| `tab8` | Tab8 | BENGAL + SCUBA (SM6115) | 11 | userdebug | 否 |
| `tab10` | Tab10 | BENGAL + SCUBA (SM6115) | 11 | userdebug | 否 |
| `tab-unknown` | Tab 系列（型号未定） | 同上 | 11 | userdebug | 兜底 |

**Tab 三台的 `ro.product.model` 上报的都是 `TabBoox`**，不是 Tab8C / Tab8 / Tab10。
所以 model 只能把范围收到家族，具体型号靠 `ro.product.device` / `ro.product.name` 区分。
区分不出来时识别结果是 `tab-unknown` 且 `ambiguous=True`，**刷分区会被拒绝** ——
Tab10 与 Tab8 的 kernel Image 逐字节差 60.33%，刷错就是不开机。

识别规则是打分制，要求证据来自至少两个不同来源（model / device / SoC 各算一个）：

```
model 精确 +40    model 包含 +25    device/name +20    SoC +15    Android +10    构建类型 +5
```

只有 model 一条证据不认（改机型 prop 是常见玩法）。

## 刷 boot 的安全设计

```bash
python -m l5tk root flash magisk_patched.img
```

顺序固定，任何一步不满足就停：

1. **校验镜像**：`ANDROID!` magic、`page_size`、kernel/ramdisk 长度、文件是否截断。
   不是 boot 镜像（比如 recovery、vendor_boot）直接拒，不写设备。
2. **校验机型**：档案 `verified=False` 的机型拒绝（要 `--force`）；识别歧义拒绝。
3. **校验锁**：`ro.boot.flash.locked=1` 拒绝。
4. **备份**：`dd` 出当前 boot 分区并 pull 回来。没 root 就没法备份，没法备份就不刷。
5. **定分区**：读设备的 `/dev/block/by-name/` 与 `ro.boot.slot_suffix`，不靠档案猜。
   实测 Leaf5 是 A/B 双槽（`boot_a` / `boot_b`，当时槽位 `b`），所以真刷会落到 `boot_b`。
   `l5tk root part` 只读，先看这一行再决定刷不刷。
6. **刷入**：`fastboot flash <分区>`。
7. **失败回滚**：刷入失败立刻用备份刷回去，再重启。
8. **等回 adb**：180 秒内没回来就报超时，不假装成功。

备份落在 `work/`，文件名带时间戳。

## Google Play

V1 在真机上踩出来的结论，V2 当硬前提：

- 商店装得上 ≠ 能用。判定标准是「拉起之后几秒，前台是不是它」。
  版本可用性随 ONYX 的拦截策略变：V1 时期 42.0.18-31 可用、44.6.15 装得上但一开就退；
  V2 实测同一台 Leaf5 上已经是 **53.7.23-31** 并在前台正常运行。
  所以工具不锁版本号，只坚持「装完必须验证真的能起来」。
- ONYX 的 `services.jar` 在 `gms_enable=false` 时拦 `com.android.vending` 启动，
  抛 `SecurityException`，异常在 system_server 侧，`am start` 的输出里看不到。
  所以判定要「`am start` 之后等几秒，再看前台 Activity」两条一起。
- 判定结果有四种出口：`launched` / `need_login` / `blocked` / `failed`。
  `need_login` 会自己打开「添加 Google 账号」页面。
- 不写 MMKV 的 `gms_enable`：那是 mmap 文件带锁和 CRC，改坏毁整份配置，
  而且账号验证通过时系统自己会置位。

APK 选取顺序（目录顺序即优先级，目录内取体积最大的）：

```
--apk 指定  >  <EXE同级>/apk  >  <EXE同级>/assets/apk  >  ./apk  >  包内 assets/apk
```

## 打包

```bash
python build.py              # 两个版本
python build.py --no-gp      # 只有普通版
python build.py --gp         # 只有集成版
python build.py --sign       # 只给已有产物签名
```

**必须用带 tkinter 的解释器。** workbuddy 自带的 3.13 没装 tkinter，
用它打包不会报错，但产出的 EXE 双击就闪退、一点提示都没有。
`build.py` 现在会在开始前 `import tkinter` 检查，不满足直接退出并提示换解释器。

本机可用的打包解释器：

```
C:\Users\<你>\AppData\Local\Programs\Python\Python312\python.exe   带 Tk 8.6 + PyInstaller 6.20
```

产物在 `dist/`：

| 文件 | 商店 APK | 界面上有「装 Google Play」 |
|---|---|---|
| `Leaf5Toolkit.exe` | 不带，靠 `apk\` 目录 | 没有这一行，窗口矮 32px |
| `Leaf5Toolkit_gplay.exe` | 内置，启动时解到临时目录 | 有 |

两个版本靠包内空标记 `assets/gp_ui.on` 区分，**不看包里有没有 APK** ——
用户往 `apk\` 丢文件是常规操作，按 APK 判断会让普通版突然长出按钮。

签名用本机证书库里 Subject 为 `CN=SWT` 的证书，按 Subject 精确匹配。
换名字：`set L5TK_SIGN_CN=CN=你要的名字`。

## 测试

```bash
python -m pytest
```

67 项。adb 与 fastboot 都是注入的假 runner，跑的是真解析逻辑，不起进程、不连设备。

## 未验证项

写在这里而不是藏起来：

- Tab8C / Tab8 / Tab10 三台的档案**没在真机跑过**，字段来自固件包分析。
- 三台 Tab 的 `ro.product.device` 实际值没采到（手上没设备），
  所以 `aliases` 是按常见命名推断的。拿到设备后 `probe --raw` 一条命令就能补准。
- A11 的 `services.jar` 补丁点未知（V1 那个 `0x2b2fee` 是 A13 偏移）。
- BENGAL 该配哪个 firehose programmer 没确定，所以 V2 不做 9008。
