#!/usr/bin/env bash
set -u

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
LOCK_FILE="${LOCK_FILE:-/tmp/feishu-business-dashboard.lock}"

cd "$PROJECT_DIR" || exit 1

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    printf '%s 上一次同步尚未结束，本次跳过\n' "$(date '+%F %T')"
    exit 0
fi

status=0
run_sync() {
    name="$1"
    shift
    printf '\n===== %s %s 开始 =====\n' "$(date '+%F %T')" "$name"
    if "$@"; then
        printf '===== %s %s 完成 =====\n' "$(date '+%F %T')" "$name"
    else
        code=$?
        printf '===== %s %s 失败（状态码 %s） =====\n' "$(date '+%F %T')" "$name" "$code" >&2
        status=1
    fi
}

run_sync "渠道日汇总" "$PYTHON_BIN" sync_channel_daily_to_feishu.py --env "$ENV_FILE"
run_sync "订单明细" "$PYTHON_BIN" sync_order_details_to_feishu.py --env "$ENV_FILE"
run_sync "APP用户数据" "$PYTHON_BIN" sync_app_user_metrics_to_feishu.py --env "$ENV_FILE"

exit "$status"
