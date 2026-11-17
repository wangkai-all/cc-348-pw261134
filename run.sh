#!/usr/bin/env bash
# 便捷启动脚本：bash run.sh [端口] [场景]
set -euo pipefail

PORT="${1:-8000}"
SCENARIO="${2:-default}"

cd "$(dirname "$0")"
exec python3 -m timesync --host 127.0.0.1 --port "$PORT" --scenario "$SCENARIO"
