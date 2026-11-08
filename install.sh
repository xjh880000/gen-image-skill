#!/usr/bin/env bash
# ============================================================
#  gen-image skill · 一键安装（macOS / Linux / WSL）
#
#  用法：
#    bash install.sh "sk-你的key"
#
#  或者从网上一行搞定：
#    curl -fsSL <安装脚本地址> | bash -s -- "sk-你的key"
# ============================================================
set -uo pipefail

REPO_OWNER="xjh880000"
REPO_NAME="gen-image-skill"
BRANCH="main"

KEY="${1:-}"
SKILL_NAME="gen-image"

if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_RST=""
fi
ok(){   printf '%s\n' "${C_OK}✓${C_RST} $*"; }
warn(){ printf '%s\n' "${C_WARN}!${C_RST} $*"; }
err(){  printf '%s\n' "${C_ERR}✗${C_RST} $*"; }
dim(){  printf '%s\n' "${C_DIM}$*${C_RST}"; }

echo ""
echo "  gen-image skill 安装程序"
echo "  ─────────────────────────────"
echo ""

# ---------------------------------------------------------------- 1. Python
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  err "没找到 Python 3.8+"
  echo ""
  echo "  先装 Python，再回来跑这个脚本："
  echo "    · Mac：   brew install python3    （或去 python.org 下载）"
  echo "    · Ubuntu：sudo apt install python3 python3-pip"
  exit 1
fi
ok "Python：$($PY -V 2>&1)"

# ---------------------------------------------------------------- 2. 找安装目录
TARGETS=""
if [ -n "${GEN_IMAGE_INSTALL_DIR:-}" ]; then
  # 隔离安装（测试/自定义位置用）
  mkdir -p "$GEN_IMAGE_INSTALL_DIR"
  TARGETS="$GEN_IMAGE_INSTALL_DIR"
  ok "指定安装目录：$GEN_IMAGE_INSTALL_DIR"
else
  for base in "$HOME/.dsh/skills" "$HOME/.claude/skills" "$HOME/.codex/skills"; do
    if [ -d "$base" ]; then
      TARGETS="$TARGETS $base"
    fi
  done
fi
if [ -z "$TARGETS" ]; then
  mkdir -p "$HOME/.claude/skills"
  TARGETS="$HOME/.claude/skills"
  warn "没检测到现成的 AI 客户端 skills 目录，已创建 ~/.claude/skills"
fi

# ---------------------------------------------------------------- 3. 取源码
SRC=""
SELF_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/scripts/gen_image.py" ]; then
  SRC="$SELF_DIR"
  ok "用本地文件安装（$SRC）"
else
  TMP="$(mktemp -d)"
  URL="https://github.com/$REPO_OWNER/$REPO_NAME/archive/refs/heads/$BRANCH.tar.gz"
  echo "  正在下载…"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$TMP/pkg.tar.gz" || { err "下载失败：$URL"; exit 1; }
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$URL" -O "$TMP/pkg.tar.gz" || { err "下载失败：$URL"; exit 1; }
  else
    err "没有 curl 也没有 wget，没法下载"; exit 1
  fi
  tar -xzf "$TMP/pkg.tar.gz" -C "$TMP" || { err "解压失败"; exit 1; }
  SRC="$(find "$TMP" -maxdepth 1 -type d -name "$REPO_NAME-*" | head -1)"
  [ -n "$SRC" ] || { err "解压后找不到源码目录"; exit 1; }
  ok "下载完成"
fi

# ---------------------------------------------------------------- 4. 复制到每个目标
INSTALLED=""
for base in $TARGETS; do
  dest="$base/$SKILL_NAME"
  rm -rf "$dest"
  mkdir -p "$dest"
  cp -R "$SRC/." "$dest/"
  rm -rf "$dest/.git"
  find "$dest" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
  find "$dest" -type f -name "_patch*.py" -delete 2>/dev/null
  rm -f "$dest/install.sh" "$dest/install.ps1"
  INSTALLED="$INSTALLED $dest"
  ok "已装到 $dest"
done

# ---------------------------------------------------------------- 5. 写配置
if [ -z "$KEY" ]; then
  echo ""
  warn "没给 key。装是装好了，但还不能出图。"
  echo "  补上 key 的办法：编辑下面任意一个 config.json，把 key 填进去"
  for d in $INSTALLED; do echo "    $d/config.json"; done
else
  for d in $INSTALLED; do
    "$PY" - "$d/config.json" "$KEY" <<'PYEOF'
import json, sys
path, key = sys.argv[1], sys.argv[2]
cfg = {
    "base": "https://xyuapi.top/v1",
    "key": key,
    "model": "gpt-image-2.5-flare",
    "chain": ["gpt-image-2.5-flare", "NanoBanana-Pro", "gpt-image-2-pro", "gpt-image-2.5-sunburst"],
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PYEOF
  done
  ok "已写入 key"
fi

# ---------------------------------------------------------------- 6. 装依赖
echo ""
if "$PY" -c "import requests" 2>/dev/null; then
  ok "requests 已存在"
else
  echo "  正在装依赖 requests…"
  if "$PY" -m pip install --quiet --user requests 2>/dev/null \
     || "$PY" -m pip install --quiet --break-system-packages requests 2>/dev/null; then
    ok "requests 装好了"
  else
    warn "自动装依赖失败。手动跑一次：$PY -m pip install requests"
  fi
fi

# ---------------------------------------------------------------- 7. 自检
echo ""
echo "  连通性自检…"
FIRST=$(echo $INSTALLED | awk '{print $1}')
if "$PY" "$FIRST/scripts/gen_image.py" --list-models >/dev/null 2>&1; then
  ok "接口通了，key 有效"
else
  warn "自检没通过。手动跑这条看详细报错："
  echo "    $PY \"$FIRST/scripts/gen_image.py\" --list-models"
fi

echo ""
echo "  ─────────────────────────────"
ok "装完了"
echo ""
echo "  怎么用："
echo "    1) 用 Claude Code / DSH 这类 AI 客户端时，直接说："
echo "       「帮我生成一张图：一只橘猫坐在窗台，午后暖光」"
echo ""
echo "    2) 不用 AI、直接命令行："
echo "       $PY \"$FIRST/scripts/gen_image.py\" -p \"一只橘猫坐在窗台，午后暖光\""
echo ""
echo "  图会存在当前目录的 gen-image-out/ 里。"
echo ""
