# easepi-r1-armbian

给 **LinkEase EasePi R1（Rockchip RK3568）** 补上 Armbian 板级支持，并用 GitHub Actions 自动出固件。

> `armbian/build` 里已经有同厂的 `easepi-a2`（RK3568）和 `easepi-r2`（RK3588），**唯独没有 r1**。
> 本仓库的板级配置就是照 `config/boards/easepi-a2.conf` 的结构写的。

## 为什么不是 ophub/amlogic-s9xxx-armbian

那是 **Amlogic s9xxx 电视盒子**的仓库，R1 是 **RK3568**，用不上。
官方出这个镜像的框架是 [`armbian/build`](https://github.com/armbian/build)
（原镜像 `/etc/armbian-release` 里记着 `BUILD_REPOSITORY_URL=github.com/xiangfeidexiaohuo/armbian-build`、`BOARD_TYPE=csc`）。

## 上游现在缺什么、本仓库补什么

| | 状态 |
|---|---|
| 内核 dts | **主线已有**（`arch/arm64/boot/dts/rockchip/rk3568-easepi-r1.dts`，commit `deaefeaf3df4`）。v6.18 没有、**v6.19 起才有** |
| u-boot | **上游 v2026.10 起自带** `configs/easepi-r1-rk3568_defconfig` |
| armbian 板级配置 | **缺** → 本仓库 `config/boards/easepi-r1.conf` |
| current(6.18) 的 dts | 内核还没带 → 本仓库 `patch/kernel/archive/rockchip64-6.18/dt/rk3568-easepi-r1.dts` 回移 |

## 怎么出固件

### 方式一：GitHub Actions（推荐）

Actions 页面 → `build-easepi-r1` → **Run workflow**，可选项：

| 输入 | 默认 | 说明 |
|---|---|---|
| branch | `current` | `current`=6.18、`edge`=7.3（edge 内核自带 dts，不用补丁） |
| release | `trixie` | trixie / bookworm / noble |
| variant | `server` | **server** / minimal / desktop |
| dts | `mainline` | `legacy` 换成旧镜像里那份（含风扇调速、RK809 音频、三色灯） |
| armbian_ref | `main` | 想钉某个 armbian/build 提交就填 SHA |

另外每周五 18:00 UTC 自动跑一次，跟着上游滚版本。

构建完成后：
- 运行页面底部 **Artifacts** 下载 `easepi-r1-*.img.xz`（约 450MB）
- Job Summary 里有 sha256

首次构建约 2–4 小时（GitHub 托管 4 核），后续命中缓存会快一些。

### 方式二：本地/服务器跑同一套

```bash
git clone --depth 1 https://github.com/armbian/build.git
./scripts/apply.sh ./build --branch=current --dts=mainline
cd build
./compile.sh BOARD=easepi-r1 BRANCH=current RELEASE=trixie \
    BUILD_MINIMAL=no BUILD_DESKTOP=no KERNEL_CONFIGURE=no COMPRESS_OUTPUTIMAGE=sha,xz
```

> 需 ~50GB 空闲。宿主机不是 Debian 13/Trixie 时最稳，`compile.sh` 在非 root 用户下
> 检测到 docker 会**自己**进容器构建，不要再手写 `docker` 子命令——
> 显式写会让容器里再解析一次 `docker`，报 `asking for docker... inside docker`（exit 43）。

## 两种 dts 的差别

| 功能 | mainline（默认） | legacy（旧镜像实际用的） |
|---|---|---|
| CPU 风扇温控（pwm-fan，45/55/65℃） | ✗ | ✓ |
| RK809 模拟音频（耳机/扬声器） | ✗ | ✓ |
| M.2 位 BT/WiFi 断电（rfkill） | ✗ | ✓ |
| LED | 仅 status 灯 | net / sata / work 三色 |
| 双千兆 + 双 2.5G + NVMe + HDMI + eMMC | ✓ | ✓ |

主线那份是 LinkEase 自己提交、被上游接受的版本；legacy 是社区早年从 OpenWrt 移植的 DIY 版。
**先编 mainline 上机**，确认风扇/音频没影响就固定用它；需要风扇调速才用 `legacy`。

## 目录

```
config/boards/easepi-r1.conf                 板级配置（照 easepi-a2.conf 写）
patch/kernel/archive/rockchip64-6.18/dt/     6.18 的 dts 回移（≥6.19 自动跳过）
extras/rk3568-easepi-r1-legacy.dts           旧镜像那份 dts（--dts=legacy 时才用）
scripts/apply.sh                             注入板级支持（本地与 CI 共用）
scripts/publish.py                           发布/触发/查状态（GitHub API，无需 git push）
.github/workflows/build.yml                  CI 定义
```

## 已知风险

1. 主线 u-boot v2026.10 + `spl-blobs` 这套组合**没在 R1 实机验证过**。旧镜像是 `radxa-e25-rk3568_defconfig` + radxa u-boot 分支。若新 u-boot 起不来，把 `config/boards/easepi-r1.conf` 里 `BOOTCONFIG` 改回 `radxa-e25-rk3568_defconfig`、删掉 `post_family_config__easepi_r1_use_mainline_uboot()` 整个函数即可回到 25.11 那套已验证配方。
2. 2.5G 口（RTL8125B）在主线 `r8169` 下有已知 TX bug（XID 641）。上游 `easepi-a2.conf` 只在 `vendor` 分支挂 `r8125-dkms` 规避。上机若 2.5G 丢包，照抄 `enable_extension "r8125-dkms"`。
3. 四个网口的 `eth0..eth3` 顺序由 PCI 枚举决定，可能与旧镜像不同（旧镜像也没做 udev 绑定）。
4. 想给上游提 PR：把 `easepi-r1.conf` 里的 `BOARD_MAINTAINER` 改成真实维护者，并补一块实机验证记录。
