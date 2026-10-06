#!/usr/bin/env bash
set -euo pipefail

# 远程部署入口。按自托管部署框架约定（docs/deploy-framework.md）：
#
#   /srv/mc-whitelist/deploy.sh [ref]
#
# 形态（决策 T7：**先手工部署**）：
#   代码由人工 scp/rsync 到 docker 可见的项目目录，本脚本负责
#   「注入运行时变量 → 重建镜像与容器 → 健康检查 → 回环自检」。
#   等 CI/CD 补上（T7 的"后补"）后，按 sso/deploy/deploy.sh 的形状改成
#   「Actions 构建推镜像 → 这里拉镜像」，本脚本其余部分不用动。
#
# 为什么这么绕（与 sso 同一组本机约束，详见 docs/mc-whitelist.md §8.3）：
#   1. Docker 是 snap 装的，**看不到 /srv**：所以本脚本（bash，不受 snap 限制）
#      在 /srv 侧读 app.env，写到 /home 下 docker 可见的项目目录；
#      交给 docker 的只有 /home 路径（`--env-file /srv/...` 会失败）；
#   2. mc_default 网络在 mc 容器被 `docker compose down` 重建后会消失，
#      那时本服务连不上 RCON —— 这里提前检查并给出可操作的报错（§11）。
# ============================================================

REF="${1:-prod}"
APP_DIR="/srv/mc-whitelist"
APP_ENV="${APP_DIR}/app.env"

PROJECT_NAME="mc-whitelist"
PROJECT_DIR="/home/docker-admin/elicloud/mc-whitelist"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.yml"
GATEWAY_NETWORK="dsh-nas_dsh-net"
MC_NETWORK="mc_default"
HEALTH_TIMEOUT=180

echo "[deploy] ref=${REF} dir=${APP_DIR}"

# ---------- 0) 前置检查 ----------
command -v docker >/dev/null 2>&1 || { echo "[deploy] 找不到 docker" >&2; exit 1; }
[[ -f "${APP_ENV}" ]] || { echo "[deploy] 缺少运行时变量文件 ${APP_ENV}（见 README-deploy.md 的清单）" >&2; exit 1; }
[[ -f "${COMPOSE_FILE}" ]] || { echo "[deploy] 缺少 ${COMPOSE_FILE}（代码还没放上去？见 README-deploy.md「部署」）" >&2; exit 1; }

for net in "${GATEWAY_NETWORK}" "${MC_NETWORK}"; do
  docker network inspect "${net}" >/dev/null 2>&1 || {
    echo "[deploy] 网络不存在：${net}" >&2
    echo "[deploy] 提示：${MC_NETWORK} 消失通常是 mc 被 'docker compose down' 重建过；先起 mc，再重跑本脚本" >&2
    exit 1
  }
done

# ---------- 1) 运行时变量：/srv → /home（docker 只认 /home） ----------
{
  cat "${APP_ENV}"
  echo ""
  echo "# ---- 以下由 deploy.sh 追加，不在 app.env 里维护 ----"
  echo "COMPOSE_PROJECT_NAME=${PROJECT_NAME}"
} > "${PROJECT_DIR}/.env"
chmod 0600 "${PROJECT_DIR}/.env"

# ---------- 2) 重建镜像与容器（幂等） ----------
echo "[deploy] docker compose build"
docker compose --project-directory "${PROJECT_DIR}" -f "${COMPOSE_FILE}" build

echo "[deploy] docker compose up -d"
docker compose --project-directory "${PROJECT_DIR}" -f "${COMPOSE_FILE}" up -d

# ---------- 3) 等健康检查通过（healthcheck 含 RCON 可达性，§5.7） ----------
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
while :; do
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' mc-whitelist 2>/dev/null || echo missing)"
  case "${status}" in
    healthy)
      echo "[deploy] 容器健康：${status}"
      break
      ;;
    unhealthy)
      echo "[deploy] 健康检查失败（注意 healthcheck 含 RCON 可达性），最近日志：" >&2
      docker logs --tail 50 mc-whitelist >&2 2>&1 || true
      exit 1
      ;;
    missing|exited|dead)
      echo "[deploy] 容器状态异常：${status}，最近日志：" >&2
      docker logs --tail 50 mc-whitelist >&2 2>&1 || true
      exit 1
      ;;
  esac
  if [ "$(date +%s)" -ge "${deadline}" ]; then
    echo "[deploy] 等待健康检查超时（${HEALTH_TIMEOUT}s），当前状态：${status}" >&2
    docker logs --tail 50 mc-whitelist >&2 2>&1 || true
    exit 1
  fi
  sleep 5
done

# ---------- 4) 回环自检 ----------
# compose 里保留了 127.0.0.1:8001 的调试映射（8000 已被 sso 占用）
if command -v curl >/dev/null 2>&1; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8001/healthz || true)"
  echo "[deploy] 自检 GET http://127.0.0.1:8001/healthz -> ${code}"
  if [ "${code}" != "200" ]; then
    echo "[deploy] 自检失败：期望 200（healthz 同时反映 RCON 可达性）" >&2
    exit 1
  fi
fi

echo "[deploy] done (ref=${REF})"
