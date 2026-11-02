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
python -m l5tk root magisk              # 装内置的 Magisk App
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

`--dry-run` 在**校验完镜像之后、任何真动作之前**就退出：不备份、不 reboot、不刷。
镜像合法性照样能验，但设备一点不动。

## root 判定

`python -m l5tk root status` 与界面上的「读取设备信息」「检查 Root」用的是同一套判定
（`l5tk/ops/probe.py:detect_root`），不会再出现「检查设备说没有、检查 Root 说有」这种互相矛盾的结果。

「读取设备信息」曾经用 `deep=False`，根本不查 root 和账号，界面上那两行是默认值「无」，
跟旁边「检查 Root」的结论直接打架。现在它走 `deep=True`，但 `allow_adb_root=False` ——
`adb root` 会让 adbd 重启、当前连接断掉，不该藏在「看一眼」里。

顺手加了一条契约：`deep=False` 时报告写的是 **「未探测」**，不是「无」。
没查过的事不能给结论。

两条实测踩到的坑，都改成显式处理了：

- **首次 `su` 可能被 Magisk 的授权框拦住**。15 秒内没在手机上点「允许」，探测就超时。
  这时报的是「无（su 调用超时：Magisk 可能在手机上弹了授权框，点「允许」后重新检测）」
  并进「注意」行，不再是干巴巴一个「无」——点一次允许再检测就正常。
- **su 的参数形式不止一种**。`su -c id` 是 Magisk 的写法，老一点的 ROM 只认 `su 0 id`。
  按顺序试三种，任一条拿到 `uid=0` 就算有 root。只试第一种会在真机上误报。
- Magisk 可执行文件的路径也不止一处：`/product/bin/magisk`（ONYX 把 `su` 做成了它的软链）、
  `/system/bin/magisk`、`/debug_ramdisk/magisk`、`/sbin/magisk` 依次试。

Google 账号的检测同理：`dumpsys account` 里多个账号挤在同一段、中间没有空行，
按空行切块再 `re.search` 只会取到第一个（比如「番茄免费小说」），Google 账号就被吞了。
现在全文 `findall` 再按 `type` 过滤。

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
python build.py --sign       # 只给已有产物签名，不打包
```

`--sign` 是「只签名」，和 `--no-gp` / `--gp` 一起写会跳过打包、只把旧产物重签一遍，
看起来也报「已签名」。这种组合现在直接报错退出，不会静默跳过。

**必须用带 tkinter 的解释器。** workbuddy 自带的 3.13 没装 tkinter，
用它打包不会报错，但产出的 EXE 双击就闪退、一点提示都没有。
`build.py` 现在会在开始前 `import tkinter` 检查，不满足直接退出并提示换解释器。

本机可用的打包解释器：

```
C:\Users\<你>\AppData\Local\Programs\Python\Python312\python.exe   带 Tk 8.6 + PyInstaller 6.20
```

产物在 `dist/`：

| 文件 | 体积 | 商店 APK | 界面上有「装 Google Play」 |
|---|---|---|---|
| `Leaf5Toolkit.exe` | 25.5 MB | 不带，靠 `apk\` 目录 | 没有这一行，窗口矮 32px |
| `Leaf5Toolkit_gplay.exe` | 64.2 MB | 内置，启动时解到临时目录 | 有 |

两个版本靠包内空标记 `assets/gp_ui.on` 区分，**不看包里有没有 APK** ——
用户往 `apk\` 丢文件是常规操作，按 APK 判断会让普通版突然长出按钮。

## 内置件

普通版就把这三样打进包里，双击即用，机器上不用装过 platform-tools：

| 件 | 源码位置 | 未压缩 | 作用 |
|---|---|---|---|
| `adb.exe` + `AdbWinApi.dll` + `AdbWinUsbApi.dll` | `assets/bin/` | 8.7 MB | 与设备通信；两个 dll 必须和 adb.exe 同目录 |
| `fastboot.exe` | `assets/bin/` | 2.2 MB | 刷 boot、读 bootloader 状态 |
| `Magisk-v31.1.apk` | `assets/magisk/` | 10.2 MB | `root magisk` 直接装到设备 |

查找顺序：**EXE 同级 `assets\` > 包内解出的 `assets\` > `PATH`**。
同级优先是有意的：换 adb 二进制或换 Magisk 版本时，丢文件进同级目录就生效，不用重新打包。

这三样二进制**不进 git**（`.gitignore` 里排掉了），clone 下来要打包时自己放进去：

```bash
cp /path/to/platform-tools/{adb.exe,fastboot.exe,AdbWinApi.dll,AdbWinUsbApi.dll} assets/bin/
cp /path/to/Magisk-v31.1.apk assets/magisk/
```

缺了也不会打错包 —— `build.py` 只把真有文件的目录打进去，`l5tk doctor` 会报「缺失」。

`l5tk doctor` 的第二列会标 `[内嵌]` 还是 `[PATH]`：
内嵌 adb 和系统 adb 同时跑会抢 5037 端口，连不上设备时先看这一行。

`root magisk` 只装 App；`root magisk --full` 走全自动，会真写 boot 分区：

```bash
python -m l5tk root magisk --full              # 备份→打补丁→写回→回读→重启
python -m l5tk root magisk --full --module     # 顺带铺 ONYX 的 AMS 修复模块
python -m l5tk root magisk --full --no-reboot  # 写回后不重启，自己选时机
python -m l5tk --dry-run root magisk --full    # 只列计划，设备一个字节不动
```

**为什么不走 `fastboot flash`**：ONYX 的 abl 把 flash 子命令裁了，
`fastboot flash boot_b` 实测返回 `remote: 'unknown command'`。
所以写回只能 `dd` —— 而有 dd 权限的前提是已经 root，这条路线必须排在拿到 root 之后。

四个「立刻停、分区一个字节没动」的点：

1. boot 备份没拉回 PC 并校验 md5 一致 → 不 patch
2. 备份不是合法 boot 镜像（无 `ANDROID!`）→ 不 patch
3. 打出来的 new-boot.img 的 **kernel 段长度变了** → 不写回
4. 写回后回读 md5 与镜像不符 → 不重启，把备份路径直接打出来

第 3 条最容易被忽略：md5 只证明「写进去的和生成的一样」，
证明不了「生成的是对的」。kernel 段变了说明分区拿错或补丁工具版本不对，写进去必砖。

签名用本机证书库里 Subject 为 `CN=SWT` 的证书，按 Subject 精确匹配。
换名字：`set L5TK_SIGN_CN=CN=你要的名字`。

## 测试

```bash
python -m pytest
```

107 项。adb 与 fastboot 都是注入的假 runner，跑的是真解析逻辑，不起进程、不连设备。

## 未验证项

写在这里而不是藏起来：

- Tab8C / Tab8 / Tab10 三台的档案**没在真机跑过**，字段来自固件包分析。
- 三台 Tab 的 `ro.product.device` 实际值没采到（手上没设备），
  所以 `aliases` 是按常见命名推断的。拿到设备后 `probe --raw` 一条命令就能补准。
- A11 的 `services.jar` 补丁点未知（V1 那个 `0x2b2fee` 是 A13 偏移）。
- BENGAL 该配哪个 firehose programmer 没确定，所以 V2 不做 9008。
