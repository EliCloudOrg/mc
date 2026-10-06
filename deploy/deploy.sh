#!/usr/bin/env bash
set -euo pipefail

# 远程部署入口。由 GitHub Actions（.github/workflows/deploy-prod.yml）通过 SSH 调用：
#
#   /srv/mc/deploy.sh [ref]
#
# 约定：
#   - 参数：要部署的 Git ref，默认 prod
#   - 幂等：可重复执行，重复跑同一 ref 不产生破坏性副作用
#   - 失败必须非 0 退出（set -e 已保证，但请勿自己吞掉错误）
#   - 本文件由仓库 deploy/deploy.sh 同步而来，不要在服务器上直接改；
#     改动请提交到仓库，再由 deploy-prod.yml 上传
#   - 运行时环境变量放在 /srv/mc/app.env，不要写进本文件

REF="${1:-prod}"
APP_DIR="/srv/mc"

cd "$APP_DIR"

echo "[deploy] ref=$REF dir=$APP_DIR"

# ===== 项目自定义部署逻辑 =====
#
# 本项目形态（DEPLOY_MODE=image）：阶段一已在 Actions 里构建并推送
# ghcr.io/<owner>/<repo>:prod 与 :sha-<short>，本脚本只负责：
#   注入运行时变量 → 取镜像 → 重建容器 → 等健康检查 → 回环自检。
#
# 本机三个硬约束决定它长这样（详见 README-deploy.md）：
#   1. Docker 是 **snap 装的，看不到 /srv**：所以本脚本（bash，不受 snap 限制）在 /srv 侧
#      读 app.env，写到 /home 下 docker 可见的项目目录；交给 docker 的只有 /home 路径
#      （`--env-file /srv/...`、`-v /srv/...`、`-f /srv/...` 全部会失败）。
#   2. **ghcr.io 拉取实测会卡死**（曾 10 分钟零输出）：pull 必须带 timeout，
#      超时就回退到本机已有镜像，并在日志里明确说清用的是哪一个。
#   3. 服务要同时接入两张 external 网络：dsh-nas_dsh-net（网关反代）、
#      mc_default（连 urania-mc 的 RCON）；后者在 mc 被 `compose down` 重建后会消失，
#      所以这里先检查并给出可操作的报错。
#
# ⚠️ 一次性迁移：本脚本首次执行时，会把此前「手工部署」的容器（容器名 mc）
#    移除，交给 compose 接管（容器名 mc-mc-1）。SQLite 数据在
#    项目目录的 data/ 下，迁移前若旧目录有数据，请先手工 copy 过来（见 README-deploy.md）。

PROJECT_NAME="mc-svc"      # ⚠️ 不能叫 mc：MC 服务器那套栈的 compose 项目名就是 mc
                           # （它的编排在 /home/docker-admin/mc/，项目名由目录名推导），
                           # 撞名会让 docker compose 按项目名匹配到对方的容器并按本文件重建，
                           # 2026-10-06 已实际踩到（见 README-deploy.md「项目名冲突」）。
CONTAINER_NAME="mc"        # 与 compose 的 container_name 一致（这是服务 id）
PROJECT_DIR="/home/deploy/elicloud-mc"
APP_ENV="${APP_DIR}/app.env"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.yml"
GATEWAY_NETWORK="dsh-nas_dsh-net"
MC_NETWORK="mc_default"
HEALTH_TIMEOUT=180
PULL_TIMEOUT=120                      # 秒；ghcr 拉取会卡，必须有上限

# ---------- 0) 前置检查 ----------
command -v docker >/dev/null 2>&1 || { echo "[deploy] 找不到 docker" >&2; exit 1; }
[[ -f "${APP_ENV}" ]] || { echo "[deploy] 缺少运行时变量文件 ${APP_ENV}" >&2; exit 1; }
[[ -f "${COMPOSE_FILE}" ]] || { echo "[deploy] 缺少 ${COMPOSE_FILE}（代码还没放上去？）" >&2; exit 1; }

# 0b) **防撞项目名**（2026-10-06 实际踩到的严重事故）：
#     compose 会按「项目名」匹配已有容器，若本项目与别的栈撞名，`up -d` 可能
#     按本文件去重建**别人的**容器。这里在执行任何 compose 命令之前先核对
#     编排里声明的服务名与容器名，不一致就直接失败。
python3 - "${COMPOSE_FILE}" "${PROJECT_NAME}" "${CONTAINER_NAME}" <<'PY' || exit 1
import sys
import yaml

path, project, container = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, encoding="utf-8") as fh:
    doc = yaml.safe_load(fh)
services = doc.get("services") or {}
if list(services) != ["mc"]:
    print(f"[deploy] 禁止：{path} 的服务名不是 ['mc']，实际 {list(services)}", file=sys.stderr)
    sys.exit(1)
name = services["mc"].get("container_name")
if name != container:
    print(f"[deploy] 禁止：container_name={name!r} 与预期 {container!r} 不一致", file=sys.stderr)
    sys.exit(1)
print(f"[deploy] 编排核对通过：project={project} service=mc container={container}")
PY

# 0c) 项目名核对：容器若已属别的 compose 项目，说明发生过撞名/手工创建，
#     这里给出明确提示（第 3 步会做一次性迁移），而不是静默重建。
existing="$(docker inspect "${CONTAINER_NAME}" -f '{{ index .Config.Labels "com.docker.compose.project" }}' 2>/dev/null || true)"
if [ -n "${existing}" ] && [ "${existing}" != "${PROJECT_NAME}" ]; then
  echo "[deploy] 提示：容器 ${CONTAINER_NAME} 现属项目 '${existing}'（本项目为 '${PROJECT_NAME}'），将按第 3 步迁移" >&2
fi

for net in "${GATEWAY_NETWORK}" "${MC_NETWORK}"; do
  docker network inspect "${net}" >/dev/null 2>&1 || {
    echo "[deploy] 网络不存在：${net}" >&2
    echo "[deploy] 提示：${MC_NETWORK} 消失通常是 mc 被 'docker compose down' 重建过；先起 mc，再重跑本脚本" >&2
    exit 1
  }
done

# 从 app.env 安全取值：不做 shell eval，避免值里的特殊字符被当作命令执行
env_get() { sed -n "s/^$1=//p" "${APP_ENV}" | tail -n 1; }

IMAGE_REPO="$(env_get MCW_IMAGE_REPO)"; IMAGE_REPO="${IMAGE_REPO:-ghcr.io/elicloudorg/mc}"
IMAGE_TAG="${MCW_IMAGE_TAG:-$(env_get MCW_IMAGE_TAG)}"; IMAGE_TAG="${IMAGE_TAG:-prod}"
IMAGE="${IMAGE_REPO}:${IMAGE_TAG}"
FALLBACK_IMAGE="elicloud-mc:1.0.0"

# ---------- 1) 运行时变量：/srv → /home（docker 只认 /home） ----------
{
  cat "${APP_ENV}"
  echo ""
  echo "# ---- 以下由 deploy.sh 追加，不在 app.env 里维护 ----"
  echo "COMPOSE_PROJECT_NAME=${PROJECT_NAME}"
} > "${PROJECT_DIR}/.env"
chmod 0600 "${PROJECT_DIR}/.env"

# ---------- 2) 取镜像：先试 pull（带超时），失败回退本机镜像 ----------
USE_IMAGE=""
echo "[deploy] docker pull ${IMAGE}（上限 ${PULL_TIMEOUT}s；本机 ghcr 拉取实测会卡）"
if timeout "${PULL_TIMEOUT}" docker pull "${IMAGE}"; then
  USE_IMAGE="${IMAGE}"
  echo "[deploy] 使用拉到的新镜像：${USE_IMAGE}"
elif docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  USE_IMAGE="${IMAGE}"
  echo "[deploy] ⚠️ pull 失败/超时，但本机已有 ${IMAGE}，改用它" >&2
elif docker image inspect "${FALLBACK_IMAGE}" >/dev/null 2>&1; then
  USE_IMAGE="${FALLBACK_IMAGE}"
  echo "[deploy] ⚠️ pull 失败且本机没有 ${IMAGE}，回退到 ${FALLBACK_IMAGE}" >&2
else
  echo "[deploy] pull 失败且本机没有任何可用镜像（${IMAGE} / ${FALLBACK_IMAGE}）" >&2
  exit 1
fi
echo "${USE_IMAGE}" > /tmp/mcw-use-image

# ---------- 3) 一次性迁移：清掉不属本 compose 项目的同名容器 ----------
if docker inspect mc >/dev/null 2>&1; then
  existing_project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' mc 2>/dev/null || true)"
  if [ "${existing_project}" != "${PROJECT_NAME}" ]; then
    echo "[deploy] 迁移：现有 mc 容器属于旧部署（project='${existing_project:-非 compose}'），先移除"
    docker rm -f mc >/dev/null
  fi
fi

# ---------- 4) 重建容器（幂等：镜像与变量都没变时 compose 不做任何事） ----------
# 通过环境变量把选定的镜像传给 compose，compose 文件里用 ${MCW_IMAGE:?} 引用
export MCW_IMAGE="${USE_IMAGE}"
docker compose --project-directory "${PROJECT_DIR}" -f "${COMPOSE_FILE}" up -d

# ---------- 5) 等健康检查通过（healthcheck 含 RCON 可达性） ----------
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
while :; do
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${CONTAINER_NAME}" 2>/dev/null || echo missing)"
  case "${status}" in
    healthy)
      echo "[deploy] 容器健康：${status}"
      break
      ;;
    unhealthy|missing|exited|dead)
      echo "[deploy] 容器状态异常：${status}，最近日志：" >&2
      docker logs --tail 50 "${CONTAINER_NAME}" >&2 2>&1 || true
      exit 1
      ;;
  esac
  if [ "$(date +%s)" -ge "${deadline}" ]; then
    echo "[deploy] 等待健康检查超时（${HEALTH_TIMEOUT}s），当前状态：${status}" >&2
    docker logs --tail 50 "${CONTAINER_NAME}" >&2 2>&1 || true
    exit 1
  fi
  sleep 5
done

# ---------- 6) 回环自检 ----------
# compose 里保留了 127.0.0.1:8001 的调试映射（8000 已被 sso 占用）
if command -v curl >/dev/null 2>&1; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8001/healthz || true)"
  echo "[deploy] 自检 GET http://127.0.0.1:8001/healthz -> ${code}"
  if [ "${code}" != "200" ]; then
    echo "[deploy] 自检失败：期望 200（healthz 同时反映 RCON 可达性）" >&2
    exit 1
  fi
fi

echo "[deploy] 镜像：${USE_IMAGE}"
echo "[deploy] done (ref=${REF})"
