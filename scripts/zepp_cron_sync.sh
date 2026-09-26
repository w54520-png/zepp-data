#!/bin/sh
# Zepp 每日同步 wrapper —— 解决 3 个老问题：
#   1. token 24h 过期 → 先 refresh 自愈
#   2. workspace 路径蒸发 → 用 skill 自带路径
#   3. workspace/zepp_scripts 路径找不到 → symlink 兜底

set -e

SKILL_DIR="/var/minis/skills/zepp-data/scripts"
WS_SCRIPTS="/var/minis/workspace/zepp_data/scripts"
WS_SECRETS="/var/minis/workspace/zepp_data/.secrets/token.json"
HOME_SECRETS="$HOME/.zepp-data/.secrets/token.json"

# 临时禁用 set -e 用于 refresh（exit 2 = 需要重新登录，由 wrapper 内部处理；不应触发 set -e 终止）
set +e

# 1. 修 workspace symlink（向后兼容 dashboard 等老脚本）
if [ ! -e "$WS_SCRIPTS/pull_to_sqlite.py" ]; then
  mkdir -p "$(dirname "$WS_SCRIPTS")"
  ln -sfn "$SKILL_DIR" "$WS_SCRIPTS" 2>/dev/null || true
fi
if [ ! -e "$WS_SECRETS" ] && [ -e "$HOME_SECRETS" ]; then
  mkdir -p "$(dirname "$WS_SECRETS")"
  ln -sfn "$HOME_SECRETS" "$WS_SECRETS" 2>/dev/null || true
fi

cd "$SKILL_DIR" || exit 1

# 2. 先 refresh token（不踢手机 Zepp App）
echo "=== Step 0: refresh app_token ==="
python3 "$SKILL_DIR/zepp_oauth.py" refresh
REFRESH_EXIT=$?

# exit code 2 = refresh 失败（login_token 过期），需要完整 OAuth
if [ $REFRESH_EXIT -eq 2 ]; then
  echo ""
  echo "⚠️ login_token 已过期，需要重新登录（理论上不会踢手机 Zepp App，因为 APP_NAME 已经是 Mi Fit 退役 id）"
  if [ -z "$ZEPP_PHONE" ] || [ -z "$ZEPP_PASSWORD" ]; then
    echo "❌ 缺 ZEPP_PHONE / ZEPP_PASSWORD 环境变量，无法自愈"
    exit 1
  fi
  rm -f "$HOME_SECRETS"
  python3 "$SKILL_DIR/zepp_oauth.py" login --phone "$ZEPP_PHONE"
fi

# 重新启用严格模式（sync 阶段任何错误都应该暴露）
set -e

# 3. sync 数据
echo ""
echo "=== Step 1: sync data ==="
python3 "$SKILL_DIR/pull_to_sqlite.py" sync --days 7

# 4. dedup（防止 daily 流重复）
echo ""
echo "=== Step 2: dedup ==="
python3 "$SKILL_DIR/pull_to_sqlite.py" dedup 2>/dev/null || true

# 5. 拉取运动历史（workouts 表，按需）
echo ""
echo "=== Step 3: fetch workouts ==="
python3 "$SKILL_DIR/fetch_workouts.py" --from 2025-01-01 2>&1 | tail -15

echo ""
echo "=== Done ==="