"""使用说明的内容（纯数据，不 import tkinter）。

为什么单独一个模块：

  * `tests/test_help.py` 要在没有 tkinter 的解释器下检查「界面上每个按钮都有
    对应章节」。内容跟绘制分开，测试才不用起窗口；
  * 改文案不该碰界面代码。

行首标记（`l5tk/ui/helpwin.py:render()` 认这几种，加新标记要同步改那边）：

    "## "   小标题（蓝，段前留白）
    "- "    无序项（悬挂缩进）
    "1. "   有序项（原样显示，同样悬挂缩进）
    "! "    警告（橙）
    "> "    提示（灰）
    "|"     代码行（等宽，绿）
    其它    正文段落

空行分段。
"""

from __future__ import annotations

TITLE = "使用说明"

SECTIONS: list[tuple[str, str]] = [
    ("界面总览", """\
## 三块区域
- 左栏：动作按钮。从上到下就是推荐顺序 —— 检测 → Root → 刷入 boot。
- 右栏：日志。每一步的中间结果都写在这里，同一份内容同时落盘。
- 顶栏：状态灯与状态文字。灰灯＝没连上设备，绿灯＝已认出设备且型号对得上档案。

## 两条不用记的规矩
- 所有 adb / fastboot 调用都在后台线程，界面不会「未响应」；看起来没动静时
  看右栏最后一行，那里一定有一句在做什么。
- 忙碌时所有按钮整组禁用，防止连点同时跑两个刷机流程。

## 窗口装不下的时候
窗口是 900×760，屏幕不够时自动收（最小 620）。左栏有 11 个按钮 × 三行说明，
加起来 850px，一定装不下 —— 所以左栏是滚动的，右边日志区固定不动。

- 左边那条 8px 的细线就是滚动条，拖动它、点空白处、或者直接把鼠标放在左栏上滚轮。
- 滚轮只在鼠标落在左栏时才滚。放在日志区上滚的是日志，两处不会一起动。
- 内容装得下时滚动条只画一条暗轨道、不画滑块 —— 看不到滑块就是真的没有更多了。
"""),

    ("① 检测设备", """\
## 做什么
只读。取型号、Android 版本、内核版本、root 状态、当前槽位、Google 账号，
一个字节都不往设备写。

## 结果里最该看的两行
- 「机型档案」：后面能不能刷 boot 由它决定。三台 Tab 的 `ro.product.model`
  都上报 `TabBoox`，区分不出来时结果是 `tab-unknown`，这时**刷分区会被拒绝** ——
  Tab10 与 Tab8 的 kernel 逐字节差 60%，刷错就是不开机。
- 「锁定」：`ro.boot.flash.locked=1` 时写不进 boot，vbmeta 校验也过不去。

## 什么时候点
第一次连设备、换了机器、OTA 之后各点一次。程序启动 0.3 秒会自动跑一次，
之后也可以手动重跑（菜单「工具 → 重新检测设备」）。
"""),

    ("③ 检查 Root", """\
## 查什么
`su` 是否可用、Magisk 装没装、SELinux 状态、A/B 当前槽位。

## 两个真机上踩出来的坑
- **首次 su 会被 Magisk 的授权框拦住**。15 秒内没在手机上点「允许」就报超时，
  结论是「无（su 调用超时…）」。点一次允许再检测就正常，别当成没 root。
- **su 的参数形式不止一种**。`su -c id` 是 Magisk 的写法，老 ROM 只认 `su 0 id`，
  工具按顺序试三种；Magisk 可执行文件也在四个路径里依次找。

> 结论只有「有」「无」「未探测」三种。没查过的显示「未探测」，不显示「无」 ——
> 没查过的事不能给结论。
"""),

    ("一键 Root", """\
## 它按代价从低到高试六条路
- 1) 已经是 root —— 直接用
- 2) adb root —— userdebug 固件才成立，adbd 会重启
- 3) 内核载荷 exploit —— 临时 root，重启失效
- 4) Magisk dd —— 有 su 之后把补丁写进 boot 分区（持久）
- 5) fastbootd —— 没 su 时写 boot，不经过 abl 的命令表
- 6) fastboot —— bootloader 支持才成立，ONYX 上必失败

任何一步不成立就停在那一步，**不猜、不跳过**。例如镜像 banner 里的 commit 与
设备 `/proc/version` 不一致时直接停 —— 偏移差几百字节，拿去跑就是内核随机位置崩。

## 「顺带装 AMS 修复模块」（默认勾）
ONYX 4.2.3 把 `ActivityManagerService.addPackageDependency` 的 `proc == null`
分支改成了自己的追踪代码，进去就解引用 null。Magisk 的守护进程由 `app_process` 起、
不在 PidMap 里 ⇒ 恒 null ⇒ **Magisk App 每次启动都卡在启动图**（root 本身是好的）。

补丁是单字节：`0x2b2fee` 的 `38 01 33 00` → `38 01 3F 00`。这个偏移只对
Leaf5 的 `4.2.3-rel_0928` 成立；Tab 三台是 Android 11 framework，字节不符时
模块会自动降级成「跳过并说明原因」，不会把已经写好的 boot 判成失败。

## 什么时候别勾
Magisk App 能正常打开时它多余。取消勾选即可，其它流程不受影响。
"""),

    ("装 Magisk App", """\
只装 App，**不写 boot 分区**。已经有 root、只缺管理器时用它。

要「装 App + 打补丁写回 boot + 重启」走命令行 `root magisk --full`，
或者勾上一键 Root 里的持久化路径。

> ONYX 会自动冻结 Magisk App（`enabled=3` DISABLED_USER），重启后 `am start`
> 报 `Activity class does not exist`。工具装完会执行一次 `pm enable` 把状态改回来。
"""),

    ("④ 取原厂 boot", """\
## 解决什么
刷 boot 得先有镜像。自己 `dd` 备份的前提是设备已经 root —— 没 root 的机器连块设备
都读不了。官方 OTA 里的 `payload.bin` 带着完整 boot，纯本地解包就能取出来。

## 怎么用
点按钮 → 选 ONYX 官网那个整包 `update.zip`（1.74 GiB）→ 工具抽出
`boot` 或 `init_boot` → 写到 `work\\` 下。

**不需要设备、不需要 root。**

## 四个容易踩的点
- **`init_boot` 优先**。Android 13 起 ramdisk 从 `boot` 搬到 `init_boot`，两个都在时
  必须打 `init_boot`；打 `boot` 会得到没有 ramdisk 的镜像，刷上去不进系统。
- **`.upx` 不能直接喂**。那是 ONYX 自己的加密容器（随机字节开头、无魔数），
  先解成 `update.zip` 再选。
- **不整个解压**。`payload.bin` 在 zip 里是 STORED，按 local header 算绝对偏移切片读，
  1.8 GB 的包只读用到的那几 MB。
- 增量操作（`MOVE` / `BSDIFF` / `SOURCE_COPY` / `PUFFDIFF` / `ZUCCHINI`）遇到就报错 ——
  full OTA 里出现说明包不对，猜着拼会拼出废镜像。

实测四份（Leaf5 / Tab10 / Tab8C / Tab8）都能解：分区 13/12/12/12，块 4096，
boot 都是 96 MB，抽出来头 8 字节是 `ANDROID!`。
"""),

    ("⑤ 刷入 boot 镜像", """\
## 三条写入通路，自动挑
- `dd` —— 有 root；不重启，写完能回读 md5；`dd bs=1M conv=fsync`
- `fastbootd` —— 无 root；10~30 秒，屏幕有提示；fastbootd 自己的 `write(blockdev)`
- `fastboot` —— 需 abl 支持；重启进 bootloader；`fastboot flash`（ONYX 上必失败）

`auto` 的顺序：先探 su，探到走 `dd`；否则有设备就走 `fastbootd`。

## 为什么需要 fastbootd
ONYX 4.2 的 abl 把 `fastboot flash` 这条 handler 裁了 —— bootloader 模式下
一律回 `remote: 'unknown command'`。但 `fastboot reboot fastboot` 是**另一条
handler**，它拉起的是跑在 Android 里的 userspace 守护进程，写入走
`OpenPartition → write(blockdev)`，**完全不经过 abl 的命令分发表**。

判定只认一条：`fastboot getvar is-userspace` 回 `yes`（bootloader 模式回 `no`）。
product 名两边会重样，不能拿来判。

## 写之前的四道闸
1. 镜像 `ANDROID!` magic、page_size、kernel/ramdisk 长度、是否截断
2. 机型档案 `verified=False` 或识别歧义 → 拒（要 `--force`）
3. `ro.boot.flash.locked=1` → 拒
4. 分区容量装不下镜像 → 不发写命令

## 备份与回滚
**写分区之前一定先备份，备份做不出来就不写** —— 三个通路都这样，没有开关能跳过
（`--no-backup` 已经停用，传了会直接拒绝）。boot 分区刷坏是这台机器上唯一不能自救
的状态，没有原分区就只能整机重刷。

`dd` 通路先 `blockdev --getsize64` 比容量、把当前分区备份到 `work\\`，
写完回读 md5；不符立刻用备份写回。`fastbootd` 通路在**进 fastbootd 之前**备份
（进去以后 adb 就断了），备份失败就中止。备份文件小于 1MB、或者长度对不上分区，
都按「没有备份」处理。

> 备份是这台机器**当前**那份 boot。root 过的机器，它已经是 Magisk 补丁过的版本，
> 拿来当「恢复原厂」的基底不对 —— 要原厂底片请用「④ 取原厂 boot」。
"""),

    ("EDL 刷 boot（9008）", """\
## 什么时候用
`fastbootd` 也进不去，或者设备已经开不了机、只能进 9008 时。

9008 是 SoC 自己的 PBL/SBL 下载模式，走高通 Firehose 协议，跟 fastboot 是两条
互不相干的通道，裁 fastboot 命令表裁不掉它。**设备会黑屏重启，这是正常的。**

## 顺序（任何一步不成立就停，分区一个字节不动）
- 1 分区必须在 GPT 里，容量必须装得下镜像
- 2 先读一份原分区到 PC，长度必须等于 GPT 记的 length
- 3 写
- 4 读回镜像长度那一段，sha256 与源文件逐字节比对
- 5 不符 → 用第 2 步的备份写回，报「已回滚」
- 6 reset 让设备离开下载模式

## programmer 必须与 SoC 匹配
- Leaf5 = LITO(SM6350)：`lito_moto_fhprg.bin` 已内置（默认用这份，
  MaxPayload 1MB，实测 96MB 写入约 4 秒）。
- 同平台的 `lito_op_n10_fhprg.bin` 也在包内，但它是 OnePlus N10 的受限构建，
  `program` 会被 `Device verified failed` 挡掉，只能读不能写。
- Tab8 / Tab8C / Tab10 = BENGAL(SM6115)：仓库里没有对应的 programmer，
  这条通路在那三台上走不通。

拿错 programmer 的表现是 Sahara 握手不应答、超时。工具把「缺 programmer」和
「握手失败」分成两个 stage，后者会提示 SoC 可能不是 LITO。
"""),

    ("② 装 Google Play", """\
## 只有集成版有这一行
普通版包里没有商店 APK，这一行不显示（窗口也矮 32px）。

## 前置条件
先在设备设置里登录 Google 账号并通过验证。ONYX 在 framework 层按 MMKV 里的
`gms_enable` 拦 `com.android.vending` 的启动，而那个值只在账号验证通过的那一刻
由系统自己写 —— 不登录就永远停在「被拦」。

## 判定
「装上了」不算数，判据是**拉起之后几秒，前台是不是它**。四种出口：
`launched` / `need_login` / `blocked` / `failed`。`need_login` 会自动打开
「添加 Google 账号」页面。

工具不锁版本号：能用的版本随 ONYX 的拦截策略变，所以只坚持「装完必须验证真的能起来」。
"""),

    ("内核适配（换固件）", """\
## 什么时候要
OTA 升级、换 ROM、换内核之后。载荷里 62 个符号偏移和 `services.jar` 的补丁点
全是硬编码的，换一版内核还拿旧载荷去跑就是 kernel panic。

## 怎么走
打开向导后，符号有两个来源：
- **设备已 root**：直接从 `/proc/kallsyms` 读，最省事。
- **没 root**：喂一份 `boot.img`（可以用「④ 取原厂 boot」抽出来），向导会切出
  kernel 段、解压、现场反解 kallsyms，再调 NDK 编 `preload.so`。

产物落在 `work\\` / 适配工作区里，**在包外**，不用重新打包 EXE，下次启动自动捡。

> 首次编译要下 NDK（几百 MB），只下这一次。
"""),

    ("日志与排障", """\
## 日志在哪
- 界面上是右栏，落盘在 `logs\\l5tk-<日期>.log`（EXE 同级的 `logs\\`）。
- 菜单「文件 → 打开日志目录」直接开目录；「打开备份目录」是 `work\\`。

## 默认脱敏
日志里账号名、内网 IP 之类会被打码。要排障看原文时设环境变量 `L5TK_LOG_PLAIN=1`
再启动 —— 排障能力不能因为脱敏丢掉，所以留了这个开关。

## 常见现象对照
- `device unauthorized`：手机上有个「允许 USB 调试」的框没点。
- `remote: 'unknown command'`：bootloader 不支持 flash，改用 fastbootd 或 EDL。
- 「镜像不是 boot 镜像」：选错文件了，recovery / vendor_boot 都不能当 boot 用。
- 「识别歧义」：三台 Tab 型号同名，刷分会被拒；先 `probe --raw` 把档案补准。
- 装不上驱动 / 找不到设备：`doctor` 看内嵌 adb 还是 PATH 里的 adb，
  两个 adb 同时跑会抢 5037 端口。
"""),

    ("命令行速查", """\
不用界面时一条命令等价（在项目目录下 `python -m l5tk ...`）：

- `profiles` —— 机型档案表
- `doctor` —— 环境自检（内嵌 adb 还是 PATH 里的）
- `probe` —— 设备画像
- `gplay enable` —— 装商店 + 拉起 + 判定
- `root status` —— root 状态
- `root auto` —— 一键：已 root → adb root → exploit → Magisk dd → fastbootd
- `root payload --ota update.zip` —— 从官方 OTA 抽原厂 boot
- `root flash boot.img --via auto` —— 备份 → 刷入 → 失败回滚
- `root edl --gpt` —— 读分区表（只读）
- `verify` —— 逐项验收

退出码：`0` 成功 / `1` 动作失败 / `2` 用法或环境问题 / `3` dry-run 被拒。

全局开关：`--serial`、`--port`、`--log-plain`、`--dry-run`。
对应环境变量：`L5TK_SERIAL` / `L5TK_LOG_PLAIN=1` / `L5TK_DRY_RUN=1` /
`L5TK_ADB` / `L5TK_FASTBOOT` / `L5TK_TIMEOUT`。
"""),

    ("快捷键", """\
- `F1` —— 打开本说明
- `Ctrl+F` —— 在说明里搜索（回车跳到下一处）
- `Esc` —— 关闭说明窗口
- `Ctrl+L` —— 清空右栏日志（只清界面，盘上的日志不动）
- `Ctrl+Q` —— 退出
> 按钮自身的键盘操作照 tkinter 默认：Tab 切换焦点，空格或回车触发。
"""),
]


def sections() -> list[tuple[str, str]]:
    return SECTIONS


def plain() -> str:
    """整份说明的纯文本（导出 / 测试用）。"""
    out = [TITLE, "=" * 40, ""]
    for name, body in SECTIONS:
        out.append(name)
        out.append("-" * 40)
        out.append(body.rstrip())
        out.append("")
    return "\n".join(out)


def search(word: str) -> list[tuple[int, int]]:
    """在全部章节里找 `word`（不分大小写）。

    返回 [(章节下标, 行号)]。行号是**该章节正文里的行号**，从 0 起 ——
    界面要拿它算 Text 的绝对位置，所以必须相对章节起点，不能是全文行号。
    """
    w = (word or "").strip().lower()
    if not w:
        return []
    hits: list[tuple[int, int]] = []
    for i, (_name, body) in enumerate(SECTIONS):
        for n, line in enumerate(body.splitlines()):
            if w in line.lower():
                hits.append((i, n))
    return hits
