#!/usr/bin/env bash
# 在宿主机(非容器)直接运行毕昇后端全家桶。
# 前置条件: docker compose -p bisheng up -d 已启动 7 个中间件。
# 用法: ./start-host.sh [api|worker|linsight|beat|all|stop|status]
set -euo pipefail

BACKEND_DIR="/Users/louissum/apps/bisheng/src/backend"
COMPOSE_DIR="/Users/louissum/apps/bisheng/docker"
PY="$BACKEND_DIR/.venv/bin/python"
CELERY="$BACKEND_DIR/.venv/bin/celery"
LOG_DIR="/tmp/bisheng-host"

mkdir -p "$LOG_DIR" "$BACKEND_DIR/data"

load_env() { set -a; . "$BACKEND_DIR/.env.host"; set +a; }

start_api() {
  pgrep -f "uvicorn bisheng.main:app" >/dev/null && { echo "api 已在运行"; return; }
  (cd "$BACKEND_DIR" && load_env && PYTHONPATH=. nohup "$PY" -m uvicorn bisheng.main:app \
     --host 0.0.0.0 --port 7860 --workers 1 --no-access-log > "$LOG_DIR/api.log" 2>&1 &)
  echo "api 启动 -> $LOG_DIR/api.log"
}

start_worker() {
  pgrep -f "celery -A bisheng.worker.main worker" >/dev/null && { echo "worker 已在运行"; return; }
  (cd "$BACKEND_DIR" && load_env && PYTHONPATH=. nohup "$CELERY" -A bisheng.worker.main worker \
     -l info -c 100 -P threads -Q knowledge_celery,ocr_celery,workflow_celery,celery \
     -n min_worker@%h > "$LOG_DIR/worker.log" 2>&1 &)
  echo "worker 启动 -> $LOG_DIR/worker.log"
}

start_linsight() {
  pgrep -f "linsight/worker.py" >/dev/null && { echo "linsight 已在运行"; return; }
  (cd "$BACKEND_DIR" && load_env && PYTHONPATH=. nohup "$PY" bisheng/linsight/worker.py \
     --worker_num 1 --max_concurrency 5 > "$LOG_DIR/linsight.log" 2>&1 &)
  echo "linsight 启动 -> $LOG_DIR/linsight.log"
}

start_beat() {
  pgrep -f "celery -A bisheng.worker.main beat" >/dev/null && { echo "beat 已在运行"; return; }
  (cd "$BACKEND_DIR" && load_env && PYTHONPATH=. nohup "$CELERY" -A bisheng.worker.main beat \
     -l info > "$LOG_DIR/beat.log" 2>&1 &)
  echo "beat 启动 -> $LOG_DIR/beat.log"
}

stop_all() {
  pkill -f "uvicorn bisheng.main:app" || true
  pkill -f "celery -A bisheng.worker.main" || true
  pkill -f "linsight/worker.py" || true
  echo "后端进程已停止"
}

status() {
  echo "--- 宿主机进程 ---"
  for pat in "uvicorn bisheng.main:app" "celery -A bisheng.worker.main worker" \
             "linsight/worker.py" "celery -A bisheng.worker.main beat"; do
    if pgrep -f "$pat" >/dev/null; then echo "  UP   $pat"; else echo "  DOWN $pat"; fi
  done
  echo "--- 中间件容器 ---"
  docker ps --filter "name=bisheng-" --format '{{.Names}}\t{{.Status}}' | sed 's/^/  /'
  echo "--- 端口 ---"
  for p in 7860 3306 6379 9200 19530 8080 9100; do
    nc -z localhost "$p" 2>/dev/null && echo "  OK   $p" || echo "  --   $p"
  done
}

case "${1:-all}" in
  api) start_api ;;
  worker) start_worker ;;
  linsight) start_linsight ;;
  beat) start_beat ;;
  stop) stop_all ;;
  status) status ;;
  middleware)
    # 只起 7 个中间件。别用不带服务名的 `docker compose up -d`——那会把
    # backend/frontend/code-runner/runtime-manager 等生产镜像也拉下来(几个 G)。
    (cd "$COMPOSE_DIR" && docker compose -p bisheng up -d \
       mysql redis elasticsearch etcd minio milvus openfga)
    docker ps --filter "name=bisheng-" --format '{{.Names}}\t{{.Status}}' | sed 's/^/  /' ;;
  all)
    start_api; start_worker; start_linsight; start_beat
    sleep 5; status ;;
  *) echo "用法: $0 [api|worker|linsight|beat|all|stop|status]"; exit 1 ;;
esac
