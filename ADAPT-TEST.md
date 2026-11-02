# 内核适配向导 — 实测报告

日期：2026-11-02
范围：`l5tk adapt`（V2 新增）+ 与 root 相关的所有**只读**路径
设备：Leaf5（e913138，已 root，Magisk 31.1，固件 `2026-09-28_15-24_4.2.3-rel_0928_656b72278`）
素材：`E:\BOOX\firmware\_img\*.Image`（四个型号固件包里解出的裸 ARM64 kernel）
      + `E:\BOOX\out_dump\boot_b.img`（设备上捞出的完整 boot，100 MB）
约束：**不写设备、不刷机、不跑 exploit**（一台已 root 的机器上重复 patch 内核只有 panic 风险）

---

## 1. 结果总表

| # | 项目 | 结果 | 证据 |
|---|---|---|---|
| 1 | 从 ROM 里读构建身份（linux_banner） | 通过 | Leaf5 镜像 banner 与设备 `/proc/version` 逐字相同（#38 / Jul 28 2026） |
| 2 | 镜像↔设备同一构建的判定 | 通过 | Leaf5 判 `dirty`；Tab8/Tab8C/Tab10 各自判 `different`（不是这台机器） |
| 3 | 裸 ARM64 Image 反解 kallsyms | 通过 | 四份镜像全部解出（117,009 / 110,455 / 110,455 / 110,457 条） |
| 4 | 与独立解码器交叉验证 | 通过 | `kallsyms-finder` 对比：名字集合完全一致，98.1% 地址逐条相同 |
| 5 | 符号宏偏移提取 | 通过 | 每份镜像拿到 55 个；7 个是 exploit 自选 .bss 槽位，本就没有同名符号 |
| 6 | **手工宏自动定标（新增）** | 通过 | 4 个全部自动得出，四份镜像结果一致（0x7d8 / 0x7e0 / 0x200 / 0x50 / 3） |
| 7 | `target.h` 生成 + 差异表 | 通过 | 差异数量与内容都对得上（见 §3） |
| 8 | 三段编译（su_daemon → 链接） | 通过 | 四份镜像各编出一份 so：2,495,432 / 2,495,352 ×2 / 2,495,432 字节 |
| 9 | 编译确定性 | 通过 | 同一镜像跑两遍，so 的 md5 逐字节相同 |
| 10 | 适配产物与出厂载荷的等价性 | 通过 | 反汇编只差 1 条指令（见 §4） |
| 11 | **boot.img 与裸 Image 等价（新增）** | 通过 | 两条入口生成的 `target.h` 逐字符一致，so 反汇编只差常量池偏移 |
| 12 | target 仓库与设备匹配 | 通过 | `adapt status` 按 `uname -r` 命中；`root exploit` 过闸后按「已有 su」正确短路 |
| 13 | 打包产物（EXE）跑同一条链 | 通过 | `Leaf5Toolkit.exe adapt gen <Tab8.Image>` 同步 25 个源文件并解出 110,455 符号 |
| 14 | 只读设备路径 | 通过 | `doctor` / `probe`（全字段）/ `root status` / `adapt status` |

未做（按约束）：`root exploit` 实跑、刷 boot、Magisk 写回、`root auto`。
这些都会改设备状态，需要一台可以随便折腾的机器。

---

## 2. 四个型号的适配结果

| 镜像 | 内核 banner | 符号 | 符号宏 | 无同名符号 | 与模板差异 | 自动定标 | 编出 so |
|---|---|---|---|---|---|---|---|
| Leaf5 4.2.3-rel_0928 | `4.19.157-perf-g77c132eccfab-dirty` #38 | 117,009 | 55 | 7 | 10 | 4/4 | 2,495,432 |
| Tab8 4.2-rel_0603 | `4.19.157-perf-ge2582afab8ec-dirty` #11 | 110,455 | 55 | 7 | 55 | 4/4 | 2,495,352 |
| Tab8C 4.2-rel_0605 | 同上 commit，build #12 | 110,455 | 55 | 7 | 55 | 4/4 | 2,495,352 |
| Tab10 4.2-rel_0630 | `4.19.157-perf-g45e1596201e0-dirty` #41 | 110,457 | 55 | 7 | 55 | 4/4 | 2,495,432 |

自动定标的值，四份镜像**完全相同**，也与出厂模板完全相同：

| 宏 | 从哪读出来 | 值 |
|---|---|---|
| `TASK_REAL_CRED_OFF` | `commit_creds` 头两条相邻 64 位 LDR 的第一条 | 0x7D8 |
| `TASK_CRED_OFF` | 同上一对的第二条（与上一条差 8） | 0x7E0 |
| `PRCTL_FRAME_TOTAL` | `__arm64_sys_prctl` 的 `stp …[sp,#-0x60]!` + `sub sp,sp,#0x1a0` | 0x200 |
| `WAITER_LOCAL_OFF` | `task_blocks_on_rt_mutex` 有没有写 `+0x48`（deadline 字段） | 0x50 |
| `PRCTL_WAITER_WORD_DEFAULT` | waiter 与帧大小都与模板一致 → 推断沿用 | 3 |

结论：这是**同一棵 ONYX 4.19 树**，结构体布局一个字节都没动，
漂移的只有符号地址（换构建就整体平移）。所以「换一台同系列机器」
在适配向导里是零人工的 —— 不用 IDA，不用 panic 定标。

Tab8 与 Tab8C 同一 commit（`e2582afab8ec`）、不同 build 号 —— 正是「同源码重编」的
典型样本，55 个符号偏移**完全相同**，编出的 so 只差 `BUILD_VARIANT_LABEL` 字符串
引起的常量池排布。Tab10 与它们差约 15 KB 的符号地址（不同 commit）。

---

## 3. 交叉验证（为什么信这个解码器）

拿 `edl-venv\Scripts\kallsyms-finder.exe`（vmlinux-to-elf 自带，独立实现）解同一份
镜像，逐符号比对：

| 镜像 | 本文 | 对照 | 名字集合 | 地址相同 | 地址不同 |
|---|---|---|---|---|---|
| Leaf5 | 117,009 | 117,009 | 完全一致 | 114,776 | 2,233（1.9%） |
| Tab10 | 110,457 | 110,457 | 完全一致 | 108,320 | 2,137（1.9%） |

- **0 个多、0 个少** ⇒ token 表 / markers / names / offsets 四段结构没有错位。
- 1.9% 的差异全部是**重名符号**：`.compoundliteral.151`（6 次）、
  `status_change_work`（2 次）、`gcc_gpu_cfg_ahb_clk`（2 次）这类。
  本文取最小地址，对照工具取另一条 —— 两种都"对"，只是选了不同实例。
  55 个宏没有一个是重名，所以对适配无影响。

指令定标的交叉验证走的是另一条路：直接拿 `llvm-objdump` 反汇编
`vmlinux-to-elf` 产出的 ELF，看 `commit_creds` 开头。

```
ffffff80080f0fa0 <commit_creds>:
  fffffff…fa0:  stp  x29, x30, [sp, #-0x30]!
  fffffff…fb0:  mrs  x20, SP_EL0
  fffffff…fb4:  ldr  x19, [x20, #0x7d8]     ← TASK_REAL_CRED_OFF
  fffffff…fb8:  ldr  x8,  [x20, #0x7e0]     ← TASK_CRED_OFF
```

```
ffffff80080e0238 <__arm64_sys_prctl>:
  …238:  stp  x29, x30, [sp, #-0x60]!
  …254:  sub  sp, sp, #0x1a0                ← 0x60 + 0x1a0 = 0x200
```

两条独立实现（我们的 Python 指令解码 / LLVM 反汇编）得出同一组数。

---

## 4. 适配产物能不能用

用同一份源码、两个 target 各编一次，比反汇编（`llvm-objdump -d`）：

- 同源 target（模板原值）→ 2,495,432 字节
- Leaf5 ROM 适配 target（偏移按镜像重算）→ 2,495,432 字节
- 29,910 行反汇编里**只差 1 行**：

```
256e84:  mov x22, #-0x8b50      ← 模板值
256e84:  mov x22, #-0x8510      ← ROM 适配值（差 0x640）
```

差值 0x640 正是 `SECURITY_HOOK_HEADS_OFF` 的差（0x3137AB0 − 0x3137470）。
其余 9 个不一致的宏（`RB_ERASE` / `RB_INSERT_COLOR` / `PLIST_ADD` / `PLIST_DEL` /
`RT_MUTEX_SLOWLOCK` / `KIMAGE_VADDR` / `KIMAGE_VOFFSET` / `MEMSTART_ADDR` /
`INIT_NET`）在源码里一次都没被引用，所以一条指令都没变。

**结论：适配算出来的偏移确实进了代码，且只影响该影响的地方。**

---

## 5. 三条实测发现

### 5.1 出厂 `target.h` 有 10 个宏与这台机器的内核不一致

设备固件是 `4.2.3-rel_0928_656b72278`、内核 `#38`，与 ROM 镜像完全同一构建；
但模板里这 10 个宏跟镜像实测值不同：

| 宏 | 模板 | 镜像实测 | 源码引用 |
|---|---|---|---|
| `SECURITY_HOOK_HEADS_OFF` | 0x03137470 | 0x03137AB0 | **1 处**（`common.h` 的 `SECURITY_CAPABLE_HEAD`） |
| `INIT_NET_OFF` | 0x0397E740 | 0x0397E880 | 0 |
| `RT_MUTEX_SLOWLOCK_OFF` | 0x013D63C8 | 0x013DBF48 | 0 |
| `RB_ERASE_OFF` | 0x013C4A08 | 0x013CA588 | 0 |
| `RB_INSERT_COLOR_OFF` | 0x013C48D0 | 0x013CA450 | 0 |
| `PLIST_ADD_OFF` | 0x013C1C38 | 0x013C77B8 | 0 |
| `PLIST_DEL_OFF` | 0x013C1D68 | 0x013C78E8 | 0 |
| `KIMAGE_VADDR_OFF` | 0x013DB000 | 0x013E1000 | 0 |
| `KIMAGE_VOFFSET_OFF` | 0x03136E00 | 0x03137440 | 0 |
| `MEMSTART_ADDR_OFF` | 0x03136DF0 | 0x03137430 | 0 |

模板留的是更早一次构建的值（同一批函数被整体平移了 0x640 ~ 0x5B80）。
主路径不受影响 —— 设备实测能拿 uid=0 就是证据；走一遍适配会把这 10 个按镜像重算。

设备那份 `boot_b.img` 解出来的 banner 与 Leaf5 ROM 镜像**逐字相同**（`#38`、
`Tue Jul 28 10:48:06 CST 2026`），差异表也一模一样 —— 说明设备上跑的就是这份固件，
上面那张表的「镜像实测」列对这台设备成立。

### 5.2 target 记错内核串（已修）

`gen` 里的 `kernel` 原来直接 `dev.get("kernel")`，即「当前 adb 连着的那台设备」的
`uname -r`。解析 Tab8 的 ROM 时，target 被记成 Leaf5 的内核串
（`manifest.json` 里四份全写 `g77c132eccfab-dirty`，banner 却是各自的值）。
后果：`root exploit` 的内核闸拿错串去比，永远不命中；更糟的是两份不同镜像
会被 `pick()` 认成同一份。

修法：镜像 banner 里的版本优先（`pick_identity()`），
fingerprint 只在「镜像内核串 == 设备内核串」时才沿用设备值，否则留空 ——
`pick()` 还能靠内核串和符号指纹命中，编一个指纹只会认错机器。
修完四份 ROM 各记各的：`g77c132eccfab` / `ge2582afab8ec` ×2 / `g45e1596201e0`。

### 5.3 打包漏了适配源码（已修）

`build.py` 的 `_has_real_file()` 用 `glob("*")` 只看一层，
`assets/exploit_src/` 下面只有 `exploit/` 一层目录、文件在更深处，
于是判成"空目录"，整份源码没进包。表现是：
`Leaf5Toolkit.exe adapt gen ...` → `找不到 exploit 源码`。

改成 `rglob` 后重打，EXE 从 27.1 MB → 27.3 MB，`adapt gen` 在打包产物里跑通。

---

## 6. 复现命令

```bash
cd E:\BOOX\leaf5-toolkit

# 读构建身份（会顺手跟连着的那台设备对一下；离线也照跑）
python -m l5tk adapt banner "E:/BOOX/firmware/_img/Leaf5_4.2.3-rel_0928_656b72278_update.Image"

# 反解符号 + 自动定标手工宏 → 生成 target.h（换 --id 避免互相覆盖）
python -m l5tk adapt gen "E:/BOOX/firmware/_img/Tab10_2026-06-30_11-16_4.2-rel_0630_b7cf3517a3_update.Image" --id rom-tab10

# 编（不再需要手填 4 个宏）
python -m l5tk adapt build rom-tab10

# 看仓库与设备匹配情况
python -m l5tk adapt list
python -m l5tk adapt status
```

boot.img 入口同一条命令，把路径换成 `.img`：

```bash
python -m l5tk adapt gen "E:/BOOX/out_dump/boot_b.img" --id dev-bootb
```

交叉验证（可选，用的是本机已装的 vmlinux-to-elf）：

```bash
E:\BOOX\edl-venv\Scripts\kallsyms-finder.exe "E:\BOOX\firmware\_img\<...>.Image" > ref.txt
E:\BOOX\edl-venv\Scripts\vmlinux-to-elf.exe  "...Image" leaf5.elf      # 再 llvm-objdump -d
```
