#!/usr/bin/env bash
# ---------------------------------------------------------------
# 把 EasePi R1 板级支持注入 armbian/build 工作树（本地与 CI 共用）
#
#   ./scripts/apply.sh <armbian-build-dir> [--branch=current] [--dts=mainline|legacy]
#
#   --branch  current | edge | bleedingedge（默认 current）
#   --dts     mainline（默认，内核主线那份）| legacy（旧镜像实际运行的 DIY 版）
#
# 做的事：
#   1. config/boards/easepi-r1.conf
#   2. 若该分支的内核还没有这块板的 dts（< 6.19），把 dts 放进
#      patch/kernel/archive/rockchip64-<ver>/dt/ ；已经自带的就不放，避免重复定义。
# ---------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-}"
BRANCH="${BRANCH:-current}"
DTS_KIND="${DTS_KIND:-mainline}"
shift || true
for arg in "$@"; do
	case "$arg" in
		--branch=*) BRANCH="${arg#--branch=}" ;;
		--dts=*) DTS_KIND="${arg#--dts=}" ;;
		-h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}"; exit 0 ;;
		*) echo "未知参数: $arg" >&2; exit 2 ;;
	esac
done

die() { echo "错误: $*" >&2; exit 1; }
say() { echo "[easepi-r1] $*"; }

[[ -n "$TARGET" ]] || die "用法: ./scripts/apply.sh /path/to/armbian/build [--branch=current] [--dts=mainline]"
[[ -f "$TARGET/compile.sh" ]] || die "$TARGET 不像 armbian/build（缺 compile.sh）"
[[ -d "$TARGET/config/boards" ]] || die "$TARGET 缺 config/boards"

case "$DTS_KIND" in
	mainline|legacy) ;;
	*) die "--dts 只能是 mainline 或 legacy" ;;
esac

# ---- 1) 板级配置 ----
BOARD_DST="$TARGET/config/boards/easepi-r1.conf"
cp -f "$HERE/config/boards/easepi-r1.conf" "$BOARD_DST"
say "已写入 config/boards/easepi-r1.conf"

# ---- 2) 查这个分支用的内核版本 ----
COMMON="$TARGET/config/sources/families/include/rockchip64_common.inc"
[[ -f "$COMMON" ]] || die "找不到 $COMMON，armbian 目录结构变了，脚本需要更新"

KVER="$(awk -v b="$BRANCH" '
	$0 ~ "^[ \t]*" b "\\)" { inside = 1; next }
	inside && /KERNEL_MAJOR_MINOR/ {
		sub(/.*KERNEL_MAJOR_MINOR="/, ""); sub(/".*/, ""); print; exit
	}
	inside && /^[ \t]*;;/ { inside = 0 }
' "$COMMON")"

[[ -n "$KVER" ]] || die "没从 rockchip64_common.inc 解析出 $BRANCH 的内核版本（分支名写错了？）"
say "BRANCH=$BRANCH => 内核 $KVER"

# ---- 3) 需要时安装 dts 补丁 ----
# 主线内核自 6.19 起自带 rk3568-easepi-r1.dts，不需要补丁。
need_dts=0
if [[ "$(printf '%s\n%s\n' "$KVER" "6.19" | sort -V | head -1)" == "$KVER" && "$KVER" != "6.19" ]]; then
	need_dts=1
fi

if [[ "$need_dts" == "1" ]]; then
	DT_DIR="$TARGET/patch/kernel/archive/rockchip64-$KVER/dt"
	[[ -d "$DT_DIR" ]] || die "缺目录 $DT_DIR"
	if [[ "$DTS_KIND" == "mainline" ]]; then
		SRC_DTS="$HERE/patch/kernel/archive/rockchip64-6.18/dt/rk3568-easepi-r1.dts"
	else
		SRC_DTS="$HERE/extras/rk3568-easepi-r1-legacy.dts"
	fi
	[[ -f "$SRC_DTS" ]] || die "找不到 dts 源文件 $SRC_DTS"
	cp -f "$SRC_DTS" "$DT_DIR/rk3568-easepi-r1.dts"
	say "已写入 patch/kernel/archive/rockchip64-$KVER/dt/rk3568-easepi-r1.dts  ($DTS_KIND)"
else
	say "内核 $KVER 已自带 rk3568-easepi-r1.dts，跳过 dts 补丁"
fi

say "完成。"
