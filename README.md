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
| 测试 | 靠真机试 | 137 项，假 adb 注入，不起进程 |

V2 不搬 V1 的 9008 写 flash 那条路：那条路依赖与 SoC 匹配的 firehose programmer，
Leaf5 是 LITO(SM6350)，Tab 系列是 BENGAL(SM6115)，V1 里那两份 programmer 在 Tab 上直接不认。
V2 的 root 路径是 `adb root` → `Magisk 补 boot` → `dd` / `fastbootd` / `fastboot` 三条写入通路择一，
每一步都能验。ONYX 裁了 bootloader 的 `flash`，所以 `fastbootd` 是没 root 时的主通路，见「三条写入通路」。

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
python -m l5tk root auto                # 一键：已root→adb root→exploit→Magisk dd→fastbootd
python -m l5tk root exploit             # 只跑内核载荷（拿临时 root）
python -m l5tk root adb                 # 试 adb root（userdebug）
python -m l5tk root check boot.img      # 只校验镜像，不碰设备
python -m l5tk root magisk              # 装内置的 Magisk App
python -m l5tk root flash boot.img      # 备份 → 刷入 → 失败自动回滚（--via auto/dd/fastbootd/fastboot）
python -m l5tk root payload --ota update.zip   # 从官方 OTA 抽原厂 boot.img，不需要设备
python -m l5tk verify                   # 逐项验收
python -m l5tk adapt scan               # 找本机现成的内核镜像
python -m l5tk adapt gen boot.img       # 换固件：生成 target.h
python -m l5tk adapt build <id>         # 编译 preload.so（首次下 NDK）
python -m l5tk adapt use <id>           # 指定 root 时用这份
```

退出码：`0` 成功 / `1` 动作失败 / `2` 用法或环境问题 / `3` dry-run 被拒。

全局开关：

```bash
--serial XXX       指定设备（多台时必须给）
--port 5053        adb server 换端口（内嵌 adb 与系统 adb 抢 5037 时用）
--log-plain        日志不打码（排障用）
--dry-run          只检查不动手
```

对应环境变量：`L5TK_SERIAL` / `L5TK_LOG_PLAIN=1` / `L5TK_DRY_RUN=1` /
`L5TK_ADB` / `L5TK_FASTBOOT` / `L5TK_TIMEOUT`。

## 界面与使用说明

窗口顶部是菜单栏，三组：

| 文件 | 工具 | 帮助 |
|---|---|---|
| 打开日志目录、打开工作目录（备份在这）、导出使用说明…、退出 | 重新检测设备、清空日志窗口 | 使用说明、导出为 txt…、项目主页、关于 |

菜单项**没有空壳**：每一项都接到真实函数。「清空日志窗口」只清界面，不动盘上的
日志 —— 日志是事后唯一证据，不会因为看着乱就被删掉。

### 使用说明（F1）

左栏 13 节，右栏正文，一次只渲染一节：

```
界面总览 / ① 检测设备 / ③ 检查 Root / 一键 Root / 装 Magisk App /
④ 取原厂 boot / ⑤ 刷入 boot 镜像 / EDL 刷 boot（9008）/ ② 装 Google Play /
内核适配（换固件）/ 日志与排障 / 命令行速查 / 快捷键
```

- **按节渲染**，不是把两万字一次塞进 Text —— 一次插入整本会让滚动和搜索都变卡。
- **搜索**（Ctrl+F）：跨节找，回车翻下一处、Shift+回车翻上一处，命中的词高亮，
  底部显示「第 3/12 处」。找的是章节内的相对行号，不是全文行号 —— 每节是单独
  渲染的，全文行号对不上。
- **导出为 txt**：整本说明落成 UTF-8 文件，方便贴给别人。
- **连按两次 F1 不会叠出两个窗口**，第二次只是把已开的那个提到前面。

快捷键：

| `F1` | 使用说明 |
|---|---|
| `Ctrl+F` | 在说明里搜索 |
| `Esc` | 关闭说明窗口 |
| `Ctrl+L` | 清空日志窗口（盘上的日志不动） |
| `Ctrl+Q` | 退出 |

文档和界面是**用测试绑住的**：`tests/test_help.py` 从 `app.py` 里刮出左栏每个按钮的
文案，逐个检查说明里有对应章节 —— 加了按钮忘记写文档，测试会红。

### 窗口高度与滚动

窗口固定 900×760；屏幕不够时按 `屏高 − 120` 收缩，最小 620（再小日志区就没了）。

左栏（11 个按钮 × 三行说明 ≈ 850px）装在 Canvas 里滚，右侧日志区固定不动 ——
日志是唯一的事后证据，不能跟着选项一起滚出视野。

- **滚动条是自绘的 8px 细条**（`ui/thinbar.py`）。`ttk.Scrollbar` 在 vista 主题下
  `width` 被主题接管，实际画出来是 17px 的浅灰块，深色窗口边上镶一道白边；
  `tk.Scrollbar` 能设 `width`，但 trough 颜色同样只在经典主题生效。
- **内容一屏装得下时只画轨道、不画滑块**。画一个满格滑块会让人以为还能往下滚，
  白拖两下才知道到底了。
- **滚轮只在指针落在左栏时生效**（含右边那 8px 滑条）。绑到全局不判位置的话，
  在日志区滚轮会同时滚左栏和日志，两处一起跳。
- **滑块最短 18px**。内容长十倍时按比例只剩几像素，鼠标根本拖不中。
- **点轨道是跳转不是翻页**，滑块中心对到指针上，接着就能拖。

`tests/smoke_ui_bar.py`（13 项）量的是：滑块比例、拖动到顶/到底的夹取、
点轨道跳转位置、滚轮方向、极长内容时的最小滑块、没绑目标时不抛异常。

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
│   ├── errors.py      异常类型，message 是人话
│   ├── kimage.py      boot 切段/解压/banner/找镜像/导 kallsyms
│   ├── elfsyms.py     ELF / kallsyms / Image 三种符号来源统一入口
│   ├── imageksym.py   裸 ARM64 Image 里现场反解 kallsyms
│   ├── ndk.py         找/下/裁 NDK，给出 clang 路径
│   ├── fp.py          无 root 的内核指纹采集与匹配
│   └── payload.py     官方 OTA 的 payload.bin 解析与抽取
├── device/            只回答「这是什么机器」
│   ├── profiles.py    档案表：Leaf5 / Tab8C / Tab8 / Tab10 / Tab 家族兜底
│   ├── detect.py      打分识别，歧义即拒绝
│   └── models.py      Profile / Detection
├── ops/               组合 core + device
│   ├── probe.py       一次取全，矛盾标 warning
│   ├── gplay.py       装/拉起/判定，APK 按目录优先级选
│   ├── root.py        boot header 校验、备份、刷入、回滚
│   ├── fastbootd.py   userspace 写入通路（绕开 abl 的 flash handler）
│   ├── exploit.py     内核载荷：闸 / 部署 / 起跑 / 判定
│   ├── magisk.py      设备内打补丁 + dd 写回 + AMS 模块
│   ├── autoroot.py    六条 root 路径按代价排序
│   └── adapt.py       内核适配：target.h、三段编译、target 仓库
└── ui/
    ├── app.py         主窗口，后台线程 + 队列，菜单栏
    ├── theme.py       配色与字体（主窗口、说明、向导共用）
    ├── helpdoc.py     使用说明正文（纯数据，不 import tkinter）
    ├── helpwin.py     说明窗口 / 关于窗口（左栏分节 + 搜索 + 导出）
    ├── thinbar.py     自绘 8px 滚动条（拖动 / 点轨道 / 滚轮）
    └── adapt_win.py   内核适配向导（独立 Toplevel）
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
6. **刷入**：按 `--via` 选通路（`dd` / `fastbootd` / `fastboot`），见下节。
7. **失败回滚**：刷入失败立刻用备份刷回去，再重启。
8. **等回 adb**：180 秒内没回来就报超时，不假装成功。

备份落在 `work/`，文件名带时间戳。

`--dry-run` 在**校验完镜像之后、任何真动作之前**就退出：不备份、不 reboot、不刷。
镜像合法性照样能验，但设备一点不动。

## 三条写入通路

ONYX 4.2 的 abl 把 `fastboot flash` 这条 handler 裁了 —— bootloader 模式下执行
`fastboot flash boot x.img` 一律回 `remote: 'unknown command'`，Leaf5 与三台 Tab 实测一致。
`fastboot reboot-bootloader`、`fastboot getvar` 都还在，只删了写入。

但 `fastboot reboot fastboot` 是**另一条 handler**，裁 flash 不连带裁它。它拉起的是
**fastbootd** —— 跑在 Android 里的 userspace 守护进程（`system/core/fastbootd` + `libfastboot`），
写入走 `OpenPartition → write(blockdev)`，**完全不经过 abl 的命令分发表**。
Android 13 + 动态分区的设备必然带它，所以 ONYX 4.2 一定有。

判定 fastbootd 只认一条：`fastboot getvar is-userspace` 回 `yes`。bootloader 模式回 `no`。
product 名两边会重样，不能拿来判。

`root flash` 的 `--via` 按代价排序：

| 值 | 前提 | 代价 | 写入方式 |
|---|---|---|---|
| `dd` | 有 root | 不重启，写完能回读 md5 | `dd bs=1M conv=fsync` 到块设备 |
| `fastbootd` | 无 root | `adb reboot fastboot`，10–30 秒 | fastbootd 内部 `write(blockdev)` |
| `fastboot` | bootloader 支持 | 重启进 bootloader | `fastboot flash`（ONYX 上必失败） |
| `auto`（默认） | — | 先探 su，探到走 `dd`，否则有设备就走 `fastbootd` | — |

`dd` 那条会 `blockdev --getsize64` 比容量、写完回读 md5，不符自动回滚。
`fastbootd` 那条进不去时，报错里列三条退路：`root exploit` + `root magisk --full`、
`root edl`、换机型。

## 取原厂 boot（不需要 root）

刷 boot 要先有 boot 镜像。手上没镜像、或者刷坏了要原厂底片，用官方 OTA 抽：

```bash
python -m l5tk root payload --ota update.zip --out work/
```

`update.zip` 是 ONYX 官网的整包 OTA。包里的 `payload.bin` 是 chromiumos 的
`update_metadata.proto` 格式（`CrAU` + 版本 + manifest 长度 + manifest + 签名 + blobs），
工具手解 protobuf 三层，只取 `boot` / `init_boot` 一个分区。

- **不整个解压**。`payload.bin` 在 zip 里是 STORED，按 zip local header 算出的绝对偏移
  直接切片读，1.8 GB 的包只读用到的那几 MB。
- **`init_boot` 优先**。Android 13 起 ramdisk 从 `boot` 搬到 `init_boot`，两个都在时必须打
  `init_boot`，打 `boot` 会得到没有 ramdisk 的镜像 —— 刷上去不进系统。
- **支持 `REPLACE` / `REPLACE_BZ` / `REPLACE_XZ` / `ZERO` / `DISCARD`**。
  `MOVE` / `BSDIFF` / `SOURCE_COPY` / `PUFFDIFF` / `BROTLI_BSDIFF` / `ZUCCHINI` 是增量操作，
  full OTA 里不该出现，遇到直接报错不猜。
- **`.upx` 不能直接读**。那是 ONYX 的加密容器（随机字节开头，无魔数），要先解成 `update.zip`。

四份实测（Leaf5 / Tab10 / Tab8C / Tab8）：分区数 13/12/12/12，`block_size=4096`，
boot 全部 `100663296` 字节（96 MB），抽出后头 8 字节是 `ANDROID!`，`parse_boot` 认得。
Leaf5 的 boot 由 48 段 `REPLACE`/`REPLACE_BZ`/`REPLACE_XZ` 拼成，1.0 秒抽完。

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
| `Magisk-v31.0.apk` | `assets/magisk/` | 10.7 MB | `root magisk` 直接装到设备 |
| `preload.so` + `device_root.sh` | `assets/payload/` | 2.4 MB | `root exploit` 的内核载荷 |
| exploit 源码（`src/` + `targets/onyx-leaf5/target.h`） | `assets/exploit_src/` | 740 KB | 适配向导现场重编 preload.so 的原料 |
| `lito_op_n10_fhprg.bin` | `assets/` | 748 KB | firehose programmer，EDL 刷 boot 用它进 9008 |
| `zadig.exe` | `assets/` | 5.0 MB | 缺 WinUSB 驱动时给 05C6:9008 绑驱动 |

查找顺序：**EXE 同级 `assets\` > 包内解出的 `assets\` > `PATH`**。
同级优先是有意的：换 adb 二进制或换 Magisk 版本时，丢文件进同级目录就生效，不用重新打包。

### Magisk 钉在 31.0，不是随手选的

31.1 把 `su` 的**授权请求**整个删了：daemon 侧只剩 `app_notify()`，没有
`app_request()`，`policies` 表里查不到的 uid 一律按 `SuPolicy::default() = Deny`
直接回 `EACCES`。客户端看到的就是 `Permission denied` / `exit=13` —— 跟
「root 没刷好」在界面上完全分不出来。`SuRequestActivity` 在 **26.3 ~ 31.0**
之间都在，所以默认装 31.0：

- 没写过策略的 app 请求 `su` → 设备上弹「授予本装置使用 Root 权限」，
  点「授予：永久」→ `policies` 表新增 `uid=<app>|policy=2`，之后 `su` 返回 0。
- 不想点弹框，也可以直接写库（需要设备已 root）：
  ```bash
  adb shell su -c 'magisk --sqlite "insert or replace into policies values(10122,2,0,1,1)"'
  adb shell su -c 'magisk --sqlite "select * from policies"'   # 读回自证
  ```
  `magisk --sqlite` 的输出带列名（`uid=2000|policy=2|...`），取值要按 `=` 后面提。

`find_apk()` 两轮扫描：先把 `PREFERRED_APK`（`Magisk-v31.0.apk`）在所有候选目录里
找遍，找不到才退回「文件名最大的那个」。原来那句 `sorted(...)[0]` 是撞运气 ——
谁把文件重命名成 `Magisk-31.1-official.apk`，就会静默换成没弹框的版本。

打补丁时还有两个只有真跑过才知道的坑，代码里都补了：

| 现象 | 原因 | 处理 |
|---|---|---|
| `boot_patch.sh` 刷完，Magisk App 在但 `su` 一直拒绝 | 脚本第 178 行无条件跑 `compress=xz magisk magisk.xz`，而 APK 里那份叫 `magisk64`。缺这个别名不报错，只是打出来的 ramdisk 里没有 magisk 二进制 | `extract_tools()` 复制一份 `magisk64` 为 `magisk` |
| 31.0 打补丁失败 / App 看不到超级用户 | 第 179、197 行要 `stub.apk`（App 存根），31.1 把这个文件从 APK 里删了 | `APK_EXTRA` 按「有就抠」处理，31.1 的布局不会因此报错 |
| `adb push … /data/local/tmp/mgk/kits: Permission denied` | `mkdir` 是 root 跑的（建出来 755），`adb push` 走 shell 身份 | 建完目录立刻 `chmod 777` |

这些二进制**不进 git**（`.gitignore` 里排掉了），clone 下来要打包时自己放进去：

```bash
cp /path/to/platform-tools/{adb.exe,fastboot.exe,AdbWinApi.dll,AdbWinUsbApi.dll} assets/bin/
cp /path/to/Magisk-v31.0.apk assets/magisk/
cp /path/to/lito_op_n10_fhprg.bin assets/      # 高通 firehose programmer
cp /path/to/zadig.exe assets/                  # WinUSB 驱动安装器
```

缺了不会打错包 —— `build.py` 只把真有文件的目录打进去，并在缺 EDL 那三样时
打印 `[!]` 警告；`l5tk doctor` 会报「缺失」。缺 programmer 时 `root edl` 报
`no_loader`，其余功能不受影响。

`l5tk doctor` 的第二列会标 `[内嵌]` 还是 `[PATH]`：
内嵌 adb 和系统 adb 同时跑会抢 5037 端口，连不上设备时先看这一行。

`root magisk` 只装 App；`root magisk --full` 走全自动，会真写 boot 分区：

```bash
python -m l5tk root magisk --full              # 备份→打补丁→写回→回读→重启
python -m l5tk root magisk --full --module     # 顺带铺 ONYX 的 AMS 修复模块
python -m l5tk root magisk --full --no-reboot  # 写回后不重启，自己选时机
python -m l5tk --dry-run root magisk --full    # 只列计划，设备一个字节不动
```

AMS 修复模块在界面上是「一键 Root」下面那个勾选框，**默认勾上** —— 和 V1
「勾了 Magisk 就自动勾上模块」的结果一致。不装的话 Magisk App 会卡在启动画面
（ONYX 把 `addPackageDependency` 的 `proc == null` 分支改成了自己的追踪代码，
一进去就 `iget-object v0, v1, ProcessRecord;->info`，`v1` 为 null）。root 本身正常，
只是 App 打不开。

补丁点 `0x2b2fee` 只对 Leaf5 的 `4.2.3-rel_0928` 成立。Tab 三台是 Android 11
framework，同一偏移的字节必然不符 —— 那时模块会自动降级成「跳过并说明原因」，
**不会把已经写回并校验过的 boot 判成失败**。

**写回走什么**：拿到 root 之后用 `dd`（不重启、能回读 md5）。没 root 时
`dd` 走不通、`fastboot flash` 又被 ONYX 的 abl 裁了（实测 `remote: 'unknown command'`），
这时走 `fastbootd` —— 它跑在 Android 里，写入不经过 abl 的命令分发表。
见「三条写入通路」与「取原厂 boot」。

四个「立刻停、分区一个字节没动」的点：

1. boot 备份没拉回 PC 并校验 md5 一致 → 不 patch
2. 备份不是合法 boot 镜像（无 `ANDROID!`）→ 不 patch
3. 打出来的 new-boot.img 的 **kernel 段长度变了** → 不写回
4. 写回后回读 md5 与镜像不符 → 不重启，把备份路径直接打出来

第 3 条最容易被忽略：md5 只证明「写进去的和生成的一样」，
证明不了「生成的是对的」。kernel 段变了说明分区拿错或补丁工具版本不对，写进去必砖。

### 一键 root

```bash
python -m l5tk root auto                      # 已root → adb root → Magisk dd → fastboot
python -m l5tk root auto --image p.img        # 前三步都不通时，用你给的镜像走 fastboot
python -m l5tk root auto --no-fastboot        # 只走 adb root / dd，不进 bootloader
python -m l5tk --dry-run root auto            # 只列会依次试哪几条，设备不动
```

四条路径按**代价从低到高**排，第一个成功就停：

| 顺序 | 路径 | 前提 | 代价 |
|---|---|---|---|
| 0 | 已 root | 有 `su` 或 Magisk | 什么都不做（这一步就是防重复写 boot） |
| 1 | `adb root` | `ro.debuggable=1` 且非 user 版 | adbd 重启，不碰分区 |
| 2 | **内核 exploit** | 内核 = `4.19.157-perf-g77c132eccfab-dirty` | 推 2.5 MB 载荷跑一轮，约 96 秒 |
| 3 | Magisk 设备内补丁 | 已有 `su`（含第 2 步给的临时 root） | 备份 → patch → dd 写回（见上一节四个闸） |
| 4 | `fastboot flash` | 给了镜像、bootloader 已解锁、abl 没裁 flash | 直接写分区 |

第 4 条在 Leaf5 上大概率不通 —— 它的 abl 裁了 `flash` 子命令。
「裸机（没 root、user 版、abl 也裁了 flash）」这一格靠第 2 步打破：
**没 root 装不了 Magisk，装 Magisk 又要先有 root**，这个循环只能由内核载荷撬开。

如果连内核载荷那条也不成立（换过内核、版本对不上），还有一条独立的腿：

### EDL 刷 boot（9008 直写）

fastboot 跑在 abl 里，厂商可以改、可以裁；9008 是 SoC 自己的 PBL/SBL 下载模式，
走高通 Firehose 协议 —— **裁 fastboot 命令表裁不掉它**。这是「没 root 又要写 boot」
时唯一剩下的通道。

```bash
python -m l5tk root edl                          # 只看状态：9008 在不在、驱动、programmer
python -m l5tk root edl --gpt                    # 读分区表（只读）
python -m l5tk root edl --image magisk_patched.img          # 写 boot_b
python -m l5tk root edl --image p.img --part boot_a         # 指定槽位
```

界面里是「EDL 刷 boot（9008）」。顺序固定，每步不成立就停、分区一个字节不动：

```
1. 分区必须在 GPT 里，容量必须装得下镜像
2. 先读一份原分区到 PC，长度必须等于 GPT 里记的 length
3. 写
4. 读回镜像长度那一段，sha256 与源文件逐字节比对
5. 第 4 步不符 → 用第 2 步的备份写回去，报 readback_mismatch_rolled_back
6. reset 让设备离开下载模式（屏幕全黑时只能这么退出来）
```

第 2 步的长度校验不是形式：备份读不满说明 USB 在掉包，这时写进去出事就没有退路。

**programmer 必须与 SoC 匹配**，这是硬门槛：

| 机型 | SoC | 需要的 programmer | 仓库里有吗 |
|---|---|---|---|
| Leaf5 | LITO (SM6350) | `lito_op_n10_fhprg.bin` | ✅ 已内置，实测通过 |
| Tab8 / Tab8C / Tab10 | BENGAL (SM6115) | bengal 的 `*_fhprg.bin` | ❌ 没有，握手就超时 |

拿错 programmer 的表现不是「友好报错」，而是 Sahara 握手阶段设备不应答、最后超时。
所以 `root edl` 会把「缺 programmer」和「握手失败」分成两个 stage，后者直接提示
SoC 可能不是 LITO。给 Tab 三台补一份 bengal programmer（放 `assets/`，或 `--loader` 指定）
就能走同一条路，代码不用改。

分区按 `ro.boot.slot_suffix` 自动判（这四台都是 A/B，当前槽位 b ⇒ `boot_b`）。
备份与读回副本落在 `work/stock_boot_b_<时间戳>.img` 和 `readback_boot_b_<时间戳>.img`，
两个都留 —— 只留一个的话，出问题时分不清是「写错了」还是「读错了」。

### 内核 exploit

```bash
python -m l5tk root exploit                  # 跑一轮，约 96 秒成功 / 210 秒失败
python -m l5tk root exploit --force-kernel   # 内核版号不匹配也跑（偏移不对会 panic）
python -m l5tk --dry-run root exploit        # 只过闸，不推载荷
```

一道硬闸：**内核必须是 `4.19.157-perf-g77c132eccfab-dirty`**。
`preload.so` 里 62 个符号偏移全部绑死在那一次构建上，同 `4.19.157` 的别的构建号
也一律拒绝 —— 靠近没有意义，偏移是按构建号对的。

它给的是**临时 root**：`/data/local/tmp/su` + daemon，**重启失效**
（SELinux 回 Enforcing）。要持久化接 `root magisk --full`，`root auto` 就是这么串的。

两条实测踩过的坑已经写进代码：

1. **设备上留着上一轮的 `/data/local/tmp/su`** 时，新一轮刚起 10 秒
   `su -c id` 就返回 `uid=0` —— 看起来「本轮成功」，实际上这一轮才刚开始 patch 内核。
   现在推载荷前先 `rm -f` 旧客户端与 `exec_probe.txt`。
2. **系统已经有 su 时不跑载荷**。一轮 exploit 是在 patch 内核结构体，
   在已 root 的机器上重复 patch 没有收益，只有 panic 的风险。

`root auto` 不会假装成功：哪条不通就打哪条的原因，最后一段写清下一步。
例如没 `su` 时会直接说「dd 读写 boot 分区的前提不成立」并提示 `--image`。

### 内核适配（换固件）

上面那道闸挡住了「换了固件的机器」。适配就是把 62 个偏移按**设备上正在跑的内核**
重算一遍，编出新的 `preload.so`：

```bash
python -m l5tk adapt scan                    # 本机有没有现成 vmlinux / boot.img
python -m l5tk adapt scan --disk             # 全盘找（先扫数据盘，180 秒上限）
python -m l5tk adapt banner boot.img         # 读 linux_banner，跟设备 /proc/version 对一下
python -m l5tk adapt kallsyms                # 设备已 root：直接导 /proc/kallsyms
python -m l5tk adapt gen boot.img            # → targets/<id>/target.h
python -m l5tk adapt fill <id> --set TASK_CRED_OFF=0x7e0 ...
python -m l5tk adapt build <id>              # 编译（首次自动下 NDK，约 700MB）
python -m l5tk adapt use <id>                # 指定用它；不给就按设备自动匹配
python -m l5tk adapt list                    # 已适配清单（* 是 use 指定的）
python -m l5tk adapt status                  # 当前设备命中哪个 target
```

符号来源四种都吃，**调用方不用区分**：`vmlinux.elf`、裸 ARM64 `Image`、
`boot.img`（切 kernel 段再解 gzip/xz/lzma/lz4/zstd）、`/proc/kallsyms` 文本。
裸 Image 的符号表是**现场反解**的（kallsyms 的 token 表就编在 `.rodata` 里），
所以设备没 root、手上又只有 boot 分区镜像时这条路照样走得通。

能自动的：符号偏移、`target.h` 骨架、编译、设备↔target 匹配。

另有 4 个宏不在符号表里（结构体布局与栈帧大小），但**它们就写在指令立即数上**，
所以也是自动的 —— `core/ainsn.py` 直接扫机器码：

| 宏 | 从哪读出来 |
|---|---|
| `TASK_REAL_CRED_OFF` / `TASK_CRED_OFF` | `commit_creds` 头两条相邻 64 位 LDR（同基址、差 8） |
| `PRCTL_FRAME_TOTAL` | `__arm64_sys_prctl` 的 `stp …[sp,#-N]!` 加后续 `sub sp,sp,#M` |
| `WAITER_LOCAL_OFF` | `task_blocks_on_rt_mutex` 有没有写 `+0x48`（有 → 0x50，无 → 0x40） |
| `PRCTL_WAITER_WORD_DEFAULT` | 只在「waiter 与帧大小都与模板一致」时推断沿用，否则留占位符 |

扫不出来（指令形态不在预期内、源是 ELF、符号偏移与文件对不上）就**整体放弃**，
退回留占位符让人 `fill` —— 宁可少填，不能填错。四个型号的 ROM 实测全部自动定标，
值也全部与出厂模板相同。

**同源构建直接继承**：镜像提出来每个宏都等于模板原值，就说明是同一份内核，
这 4 个值自动沿用模板，`gen` 完可以直接 `build`。（`--no-inherit` 强制留占位符。）

运行时匹配的优先级：符号序列 sha256 → `ro.build.fingerprint` → `uname -r`。
命中就用适配出来的 so，没命中退回内置载荷并照旧过内核闸。
V1 的 `%LOCALAPPDATA%\Leaf5RootTool\targets` 也读（`adapt list` 里标 `V1`），
之前用 V1 适配过的机器不用重做。

界面上是「内核适配（换固件）」那个按钮：来源自动找、候选可切、差异表显示
哪些宏跟模板不一样、手工宏能自动的已经填好，点编译，最后一步装进工具。

#### 四个型号 ROM 实测（2026-11-02）

素材是 `firmware/_img/*.Image`：从 `.upx` 固件包里解出来的裸 ARM64 kernel，
不接设备、不需要 root，正好能把「解析 → 生成 → 编译」整条链跑穿。

| 镜像 | 内核 | 解出符号 | 独立工具对照 | 符号宏 | 与模板差异 | 手工宏 | 编出 so |
|---|---|---|---|---|---|---|---|
| Leaf5 4.2.3-rel_0928 | `g77c132eccfab-dirty` #38 | 117,009 | 117,009（同集合） | 55 | 10 | 4/4 自动 | 2,495,432 |
| Tab8 4.2-rel_0603 | `ge2582afab8ec-dirty` #11 | 110,455 | — | 55 | 55 | 4/4 自动 | 2,495,352 |
| Tab8C 4.2-rel_0605 | 同上 commit #12 | 110,455 | — | 55 | 55 | 4/4 自动 | 2,495,352 |
| Tab10 4.2-rel_0630 | `g45e1596201e0-dirty` #41 | 110,457 | 110,457（同集合） | 55 | 55 | 4/4 自动 | 2,495,432 |

「手工宏 4/4 自动」= 以前要人拿 IDA 对的那 4 个，现在全由指令扫描定：
`TASK_REAL_CRED_OFF` / `TASK_CRED_OFF` 取 `commit_creds` 头两条相邻 LDR，
`PRCTL_FRAME_TOTAL` 取 `__arm64_sys_prctl` 的降栈总量，
`WAITER_LOCAL_OFF` 看 `task_blocks_on_rt_mutex` 有没有写 `+0x48`，
`PRCTL_WAITER_WORD_DEFAULT` 在「waiter 与帧大小都与模板一致」时推断沿用。
四份镜像的结论**全部与出厂模板相同**（0x7d8 / 0x7e0 / 0x200 / 0x50 / 3）——
同一棵 ONYX 4.19 树，结构体布局没动，只有符号地址随构建漂移。

同一份镜像跑两遍，`preload.so` 的 md5 逐字节相同（编译无随机性）。

两组交叉验证的结论：

- 符号**名字集合完全一致**（0 个多、0 个少），结构定位（token 表 / markers /
  names / offsets）没有错位；逐条比 114,776 / 108,320 条地址相同，剩下 1.9%
  全是**重名符号**（`.compoundliteral.N`、`status_change_work` 这类同一名字
  出现 2~6 次），本文取最小地址、对照工具取另一条 —— 62 个宏里没有重名，不影响。
- 7 个宏本来就没有同名符号：`bss_zero_lock` / `bss_scratch` /
  `bss_lowzero_scratch` 和 4 个 `slide_*`，都是 exploit 自己挑的 .bss 槽位，
  不是内核符号，属预期。

**一条实测发现**：出厂 `target.h` 里有 10 个宏与这台机器的 ROM 内核不一致，
其中 9 个源码一次都没引用（`RB_*` / `PLIST_*` / `KIMAGE_*` 这些备用路径），
但 `SECURITY_HOOK_HEADS_OFF` 被 `common.h` 的 `SECURITY_CAPABLE_HEAD` 用到：
模板写 `0x03137470`，本机固件实际是 `0x03137AB0`（差 0x640）。
设备固件同样是 `4.2.3-rel_0928`，说明模板留的是更早一次构建的值。
主路径不受影响（实测能拿 uid=0），而走一遍适配会把它按镜像重算成真值。

编译链路：用 Leaf5 这份 ROM 生成 target 后编出 2,495,432 字节的 `preload.so`；
与同源 target 的产物比反汇编，**只差一条 `mov x22, #-0x8b50` / `#-0x8510`**
（就是上面那个 0x640），其余指令逐条相同 —— 既证明其余 9 个差异宏确实没进代码，
也证明适配后的偏移真的落到了代码里。

**boot.img 与裸 Image 等价**：拿设备上捞出来的 `out_dump/boot_b.img`（100 MB，
kernel 段 60,735,504 字节）走同一条流程 —— 自动切段、解出同一份 banner `#38`、
同样的 10 个差异、同样的 4 个自动定标值，生成的 `target.h` 与裸 Image 路径
**逐字符一致**（只有 id / LABEL / 生成头不同），编出的 so 反汇编只差常量池偏移。
两条入口通到同一个 target。

### 两个已修的坑

- **target 记错内核串**：gen 的 `kernel` 原来直接借「当前连着的设备」的
  `uname -r`。解析 Tab8 的 ROM 时，target 会被记成 Leaf5 的内核串 ——
  `root exploit` 的内核闸就永远比不中，甚至两份不同的镜像被认成同一份。
  现在镜像 banner 里的版本优先，只有镜像内核与设备完全一致时才沿用设备指纹。
- **`--inherit` 与自动定标的关系**：以前 `--inherit` 是「明知有差异也照抄模板」，
  现在是「模板值兜底，能扫的按镜像覆盖」。四份 ROM 上两者结果重合，
  所以出厂模板对这四台都是对的；换成布局真的变过的内核时，扫描值会盖掉旧值。

签名用本机证书库里 Subject 为 `CN=SWT` 的证书，按 Subject 精确匹配。
换名字：`set L5TK_SIGN_CN=CN=你要的名字`。

## 四机型实测（2026-11-02）

四份原厂固件（`.upx` 解密 → `payload.bin` → 裸 ARM64 `Image`）各走一遍完整适配链，
每个数字都是本机跑出来的：

| 型号 | 固件 | 内核 commit / 构建号 | Image | 解出符号 | 62 宏命中 | 与出厂模板差异 | 4 个手工宏 | V2 编出 `preload.so` |
|---|---|---|---:|---:|---:|---:|---|---:|
| Leaf5 | 4.2.3-rel_0928 | `g77c132eccfab-dirty` #38 | 60,735,504 | 117,009 | 55 | 10 | 自动 4/4 | 2,495,432 |
| Tab8 | 4.2-rel_0603 | `ge2582afab8ec-dirty` #11 | 72,483,328 | 110,455 | 55 | 55 | 自动 4/4 | 2,495,352 |
| Tab8C | 4.2-rel_0605 | `ge2582afab8ec-dirty` #12 | 72,483,328 | 110,455 | 55 | 55 | 自动 4/4 | 2,495,352 |
| Tab10 | 4.2-rel_0630 | `g45e1596201e0-dirty` #41 | 72,483,328 | 110,457 | 55 | 55 | 自动 4/4 | 2,495,432 |

- 「与出厂模板差异」= 62 个 `*_OFF` 宏里跟 `targets/onyx-leaf5/target.h` 值不同的个数。
  Leaf5 差 10 个、三台 Tab 差 55 个 —— **换构建号必须整套重算**，抄模板必崩。
- 「自动 4/4」= `ainsn.py` 解机器码定出的 `TASK_REAL_CRED_OFF` / `TASK_CRED_OFF` /
  `PRCTL_FRAME_TOTAL` / `WAITER_LOCAL_OFF`，四份 ROM 得值全同
  `0x7D8 / 0x7E0 / 0x200 / 0x50` —— 同一棵 ONYX 4.19 树，结构体布局一字节没动，
  只有符号地址在漂。
- **型号之间只差地址，不差布局**：符号名字集合四份之间只在重名符号上有个位差异，
  62 个宏用到的符号全在。Tab8 与 Tab8C 是同一 commit（只差增量号），
  一份载荷大概率同时吃这两台；Tab10 是另一个 commit，不能互顶。
- 交叉验证：Leaf5 那份 Image 用 `kallsyms-finder` 独立解一遍，符号集合 0 多 0 少，
  地址 98.1% 相同（剩 1.9% 是重名符号取到不同那份）。
- 两版 `target.h` 的 163 个宏逐条比对，四份 ROM 全部 **0 处不一致**。

### root 成不成功

| 设备 | 是否 root | 依据 | 怎么测的 |
|---|---|---|---|
| Leaf5（`e913138`） | **是** | `id` → `uid=0(root)`、`context=u:r:magisk:s0`；`magisk -v` → `31.0:MAGISK:R`（2026-11-04 由 31.1 降级后复测）；`su` 在 `/product/bin/su` 与 `/debug_ramdisk/su` | 只读命令 |
| Tab8 | 未测 | 没有真机 | — |
| Tab8C | 未测 | 没有真机 | — |
| Tab10 | 未测 | 没有真机 | — |

Leaf5 现场（只读采集）：`selinux=Permissive`、`verity=enforcing`、`slot=_b`、
`uptime=24439s`（`boot_completed=1`）。

本工具的判定（都不写设备）：

| 命令 | 结果 |
|---|---|
| `l5tk root status` | 认出 `Magisk 31.0:MAGISK:R`，`flash.locked=0` 已解锁 |
| `l5tk --dry-run root auto` | 第 0 步短路：**已有 root**，不继续 |
| `l5tk --dry-run root exploit` | 短路：**系统已有 su，不必再跑内核载荷** |

**边界**：Leaf5 的 `uid=0` 是既有状态，本轮没重跑 `root auto` / `root exploit`（跑一次就是写设备）。
三台 Tab 判「可以试」的依据只有**载荷编得出来 + 内核前提同构**（4.19.157 + SLUB/PAN/
`HARDENED_USERCOPY` 同一配置面），不等于在真机上拿得到 `uid=0` —— 所以标「未测」。
拿到机器后的顺序，前两步都不写设备：

```bash
adb shell getprop ro.debuggable        # 1 三台 Tab 是 userdebug，=1 就 adb root，不用 exploit
l5tk probe                             # 2 只读：补准 ro.product.device
l5tk root auto --force-rerun           # 3 临时 root，重启即恢复
```

## 测试

```bash
python -m pytest
```

238 项。adb 与 fastboot 都是注入的假 runner，跑的是真解析逻辑，不起进程、不连设备。

`tests/test_help.py`（11 项）是文档的闸：从 `app.py` 源码里刮出左栏每个按钮的文案，
逐个检查使用说明里有对应章节；另外验三条写入通路的关键词（`fastbootd` /
`is-userspace` / `init_boot` / `unknown command`）都在文档里、搜索坐标不越界、
导出文本稳定（两次导出逐字节相同，没有时间戳之类会漂的东西）。

`tests/test_payload.py`（23 项）自己写了个 protobuf 编码器合成小包，逐段断言解出来是什么字节，
覆盖 `REPLACE` / `REPLACE_BZ` / `REPLACE_XZ` 混合、`data_offset` 相对 blobs 起点、
extents 空洞必须为零、`ZERO` / `DISCARD` 写满、zip STORED 定位、`init_boot` 优先、
增量操作报错且不留半成品。真固件那组用例对四份 OTA 做集成验证。

`tests/test_fastbootd.py`（19 项）假的不是 `Fastbootd` 类，是 **fastboot.exe 的命令行输出**，
连 `(bootloader) is-userspace: yes` 写在 stderr 这个真实形态都照抄，跑的是真解析。

## V1 → V2 未迁清单

「V1 有的 V2 必须有」逐项对，剩下的都在这里，按能不能一个人干完分组：

| 能力 | V1 形态 | V2 现状 | 阻塞点 |
|---|---|---|---|
| 检测 | `--check` | ✅ `probe` / `root status` | — |
| 商店 | `--gplay` | ✅ `gplay enable` | — |
| 全自动 Magisk | `--magisk` | ✅ `root magisk --full` | — |
| AMS 模块 | `--module` | ✅ `--module` + 界面勾选框（默认勾） | — |
| 一键入口 | `--auto` | ✅ `root auto` | — |
| 指定串口 | `--serial` | ✅ | — |
| 内核 exploit | `--root N` | ✅ `root exploit`（单轮 + 判定 + 内核闸） | — |
| 内核适配向导 | `stage_adapt` | ✅ `adapt gen/build/use` + 界面窗口 | — |
| 要求 CAP_SYS_ADMIN | `--sysadmin` | ✅ `root exploit --sysadmin`（夹具验证） | 真机完整分支未跑 |
| 强制重跑 | `--force-rerun` | ✅ `root exploit --force-rerun`（夹具验证） | 真机完整分支未跑 |
| adb 端口 | `--port` | ✅ `--port 5053` | — |
| USB 诊断 | `usbtrace` | ❌ 未迁 | 需 pyusb |
| 驱动安装 | `zadig.exe` | ❌ 未迁 | 5.3 MB 内嵌 exe |
| EDL 只读 | `--edl env/slot/gpt/dump` | ❌ 未迁 | 整套 `edlclient` 11,730 行 |
| EDL 写 flash | `--edl w` | ❌ 不做 | 缺 BENGAL 的 firehose programmer |

内核 exploit 与内核适配都已迁完。剩下三样（usbtrace / zadig / EDL）不影响
「一台 Leaf5 从裸机到 root」这条主路径。适配向导本身在真机上只验证到
「同源镜像 → 生成 → 编译」，换内核那条分支要等一台换了固件的机器。

## 未验证项

写在这里而不是藏起来：

- Tab8C / Tab8 / Tab10 三台的档案**没在真机跑过**，字段来自固件包分析。
- 三台 Tab 的 `ro.product.device` 实际值没采到（手上没设备），
  所以 `aliases` 是按常见命名推断的。拿到设备后 `probe --raw` 一条命令就能补准。
- A11 的 `services.jar` 补丁点未知（V1 那个 `0x2b2fee` 是 A13 偏移）。
- BENGAL 该配哪个 firehose programmer 没确定，所以 V2 不做 9008。
- **fastbootd 写入只验到「进得去、命令发得对」，没在真机上落盘验证**。
  `is-userspace` 判定、`partition-size` 取值、`flash` 的参数顺序都有单测，
  但 `reboot fastboot` 之后 ONYX 的 fastbootd 会不会因为 `ro.boot.flash.locked` 拒写，
  手上没空机器，未测。
