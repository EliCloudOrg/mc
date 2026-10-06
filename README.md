# EliCloud MC 白名单服务（`mc`）

> 规格与唯一真源：[`docs/mc.md`](../docs/mc.md)（740 行，含线上实测证据）。
> 本文件是**实现与运维手册**：环境变量、启动方式、API 摘要、RCON 语义、部署步骤、排错与验收。

## 一句话

**已经在 EliCloud SSO 注册过的用户，通过本服务的 API 提交自己的 Minecraft 用户名（每个账号最多 2 个）并附必填备注；
服务校验通过后立即通过 RCON 把该用户名写入 `urania-mc` 的白名单，全过程留审计。**

```text
前端/客户端 ──Bearer <SSO access_token>──► mc:8000 ──RCON──► urania-mc:25575
   (OIDC 授权码+PKCE)                          │                        (whitelist add/remove/list)
                                               └── SQLite：绑定 / 审计 / 快照
```

本服务**不是** OIDC 客户端：不在 SSO 注册、不换令牌，只验签 SSO 签发的 access token
（RS256 + JWKS + `iss`/`aud`/`exp` + 必须含 `mc:whitelist` scope）。

## 目录结构

```text
mc/
├── app/
│   ├── main.py         # FastAPI 装配、CORS、统一错误处理、访问日志（/docs 全关）
│   ├── config.py       # 全部配置来自环境变量（RCON_PASSWORD 无默认值 → 缺了启动失败）
│   ├── db.py           # SQLite 引擎/会话/建表（CREATE TABLE IF NOT EXISTS）
│   ├── models.py       # mc_names / audit_logs / whitelist_cache（含部分唯一索引）
│   ├── errors.py       # 统一错误结构 + 进程内限流器
│   ├── sso_auth.py     # SSO access token 验签（JWKS 缓存、RS256 白名单、iss/aud、scope）
│   ├── rcon.py         # 最小 RCON 客户端 + 白名单语义层（list/add/remove）
│   ├── whitelist.py    # 业务编排：名额/唯一性/幂等/回读校验/审计/对账
│   ├── deps.py         # 依赖注入、客户端 IP、Bearer 身份、管理员鉴权、限流
│   ├── cli.py          # 管理员命令（list-names / list-audit / force-remove / reconcile）
│   └── routers/
│       ├── names.py    # /v1/names、/v1/me
│       └── ops.py      # /healthz、/v1/admin/*
├── tests/              # 92 个用例：假 RCON 服务器 + 假 JWKS 服务，不依赖真实 MC
├── deploy/deploy.sh    # 部署框架入口（/srv/mc/deploy.sh）
├── Dockerfile / docker-compose.yml / .env.example
└── README.md
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SSO_ISSUER` | `https://146.56.237.33/auth` | **必须与 SSO 的 `PUBLIC_BASE_URL` 逐字一致**，否则所有令牌验签失败 |
| `SSO_JWKS_URL` | `https://146.56.237.33/auth/.well-known/jwks.json` | 公钥来源；缓存尊重 `Cache-Control: max-age` |
| `JWT_AUDIENCE` | `elicloud-services` | `id_token` 的 `aud` 是 client_id，会在这里被拒 |
| `REQUIRED_SCOPE` | `mc:whitelist` | 令牌必须含它，否则 `403 insufficient_scope`；**空串 = 关闭校验，只允许本地调试** |
| `JWKS_CACHE_SECONDS` | `300` | JWKS 缓存的兜底 TTL（响应头没有 `max-age` 时用） |
| `JWT_LEEWAY_SECONDS` | `30` | `exp`/`nbf` 的时钟偏移容忍 |
| `DATABASE_URL` | `sqlite:////data/mc.db` | 挂卷持久化 |
| `RCON_HOST` / `RCON_PORT` | `urania-mc` / `25575` | 只经 `mc_default` 内网可达 |
| `RCON_PASSWORD` | **无默认值** | 只放服务器 `/srv/mc/app.env`（600）；缺了直接启动失败 |
| `RCON_TIMEOUT` | `5` | 建连 + 读写各自的超时 |
| `MAX_NAMES_PER_USER` | `2` | 每个 `sub` 的 active 名额上限 |
| `CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | 供将来浏览器直连 |
| `ADMIN_TOKEN` | 空 | 管理接口的 Bearer；**留空 = `/v1/admin/*` 整组 403**（fail closed） |
| `SUBMIT_ATTEMPTS_PER_WINDOW` | `10` | 提交/撤回限流（按 `sub` + IP 双维度） |
| `SUBMIT_WINDOW_SECONDS` | `600` | 限流窗口 |
| `LOG_LEVEL` | `info` | 日志级别（日志里**绝不含令牌原文与 RCON 密码**） |

## API 摘要

对外路径带 `/mc` 前缀，网关剥离后服务内就是 `/v1/...`（§6.3）。错误结构统一为
`{"error": "...", "error_description": "..."}`。

| 方法 | 对外路径 | 服务内 | 说明 |
|---|---|---|---|
| POST | `/mc/v1/names` | `/v1/names` | 提交用户名；首次 **201**，重复提交同名 **200（幂等）** |
| GET | `/mc/v1/names` | `/v1/names` | 我的条目 + 名额（`?include_removed=true` 带历史） |
| DELETE | `/mc/v1/names/{id}` | `/v1/names/{id}` | 撤回自己的条目（别人的一律 **404**） |
| GET | `/mc/v1/me` | `/v1/me` | 当前身份（只回令牌 claim，不查 SSO 库） |
| GET | `/mc/healthz` | `/healthz` | **含 RCON 可达性**；RCON 不可达 → 503 |
| GET | `/mc/v1/admin/names` | `/v1/admin/names` | 全部绑定（含 sub、备注全文、UUID） |
| GET | `/mc/v1/admin/audit` | `/v1/admin/audit` | 审计日志（分页 + `user_id`/`action` 过滤） |
| DELETE | `/mc/v1/admin/names/{id}` | — | 管理员强制移除（`removed_by=admin`） |
| POST | `/mc/v1/admin/reconcile` | — | 与 MC 白名单对账；`{"apply": true}` 时补回漂移 |

错误码：`400 invalid_request`、`401 invalid_token`、`403 insufficient_scope` / `forbidden`、
`404 not_found`、`409 name_taken` / `quota_exceeded`、`429 rate_limited`、`503 rcon_unavailable`。

`POST /v1/names` 的处理顺序（**每一步的失败都可区分**）：

```text
验签 → scope → 校验 name/note → 库里已有？（自己的 → 200 幂等；别人的 → 409）
     → whitelist list（真源）→ 白名单已有 → 409 name_taken
     → 名额 → 409 quota_exceeded
     → whitelist add → whitelist list 回读确认（没确认 → 503，**不落库**）
     → 落库 + 审计
```

## RCON 语义（来自线上实测，见规格 §5）

四条必须照做的行为，`app/rcon.py` 与假 RCON 服务器都复刻了：

1. **一条连接只跑一条命令**（MC 响应后主动关闭连接）；
2. 每条命令**可能先回一个空包、再回真实内容** → 读到空包不算响应；
3. 用户名一律被服务端**转小写** → 服务内部一律用小写做键，展示时才回显原始写法；
4. `whitelist add` 的 UUID **形态不稳定**（v5 与随机 v4 并存）→ **绝不构造 UUID**，
   `mc_names.uuid` 只作记录（当前恒为 `null`，见「已知限制」）。

**判定原则：不信 `add`/`remove` 的回执，以 `whitelist list` 为准。**
写入流程固定为 `list → add/remove → list（回读确认）`，回读没确认就返回 503 且**不改库**。

## 数据模型

| 表 | 用途 | 关键约束 |
|---|---|---|
| `mc_names` | 用户名 ↔ SSO 账号的绑定（一行 = 一次申请） | `ux_mc_names_active_name`：**部分唯一索引**保证同一用户名只有一条 `active`（并发下也成立） |
| `audit_logs` | 全量审计（成功 + 失败 + 拒绝） | 含 `action`/`result`/`detail`(JSON，RCON 原文)/`request_ip` |
| `whitelist_cache` | 白名单快照（辅助对账） | **真源永远是 MC 的 `whitelist list`** |

> 会话里没有"审批状态机"（决策 2：自助即时生效），全量审计替代事前审核。

## 本地开发

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -q                                          # 92 passed，不需要真实 MC
```

测试自带**假 RCON 服务器**（复刻上面四条行为，另有两档故障注入：`lying_add` 用于验证
「不信回执」、密码不对用于验证认证失败）与**假 JWKS 服务**（真实 HTTP + 真实 RS256 验签）。

## 部署

### 首次部署（手工，决策 T7）

```text
1. 服务器：建 /srv/mc（deploy:deploy 750）与 /srv/mc/app.env（600）
2. app.env 里填 RCON_PASSWORD（从 MC 容器取，见下）等运行时变量
3. 代码放到 docker 可见的项目目录 /home/docker-admin/elicloud/mc（data/ 属主 1002:1003）
4. deploy/deploy.sh 放到 /srv/mc/deploy.sh
5. sudo -u deploy /srv/mc/deploy.sh prod     # 注入变量 → build → up -d → 健康检查
6. 网关：给 dsh-nas 的 Caddyfile 追加 /mc/* 独立站点块 → caddy validate → caddy reload
```

**取 RCON 密码**（不落盘、不进日志、不进本对话）：

```bash
sudo docker exec urania-mc printenv RCON_PASSWORD      # 然后填进 /srv/mc/app.env
```

**为什么 deploy.sh 要绕一层**：本机 Docker 是 snap 装的，**看不到 `/srv`**，
所以脚本在 `/srv` 侧读 `app.env`、写到 `/home` 下 docker 可见的项目目录；
`/srv` 只放 `deploy.sh` 与 `app.env`（与 SSO 同一套约束）。

### 网关片段（独立站点块）

```caddyfile
https://146.56.237.33/mc/* {
	tls { issuer acme { profile shortlived } }
	log
	handle /mc/* {
		uri strip_prefix /mc
		reverse_proxy mc:8000
	}
}
```

> ⚠️ 必须是**独立站点块**：面板站点块里有不带 matcher 的 `basic_auth` 兜底指令，
> 把 `/mc/*` 并进去会被它拦下（同一个坑在 SSO 上踩过一次）。
> 改网关的铁律：先 `caddy validate`，再 `caddy reload`；**绝不 `docker restart dsh-caddy`**
> （配置写错会进崩溃循环、公网全 502）。

### MC 侧（启用白名单，敏感操作）

itzg 镜像的 `server.properties` **由环境变量生成**，改宿主机那份文件没有用：

```yaml
# /home/docker-admin/mc/docker-compose.yml 的 mc 服务 environment 追加
      WHITELIST: "TRUE"
      ENFORCE_WHITELIST: "TRUE"
```

```bash
cd /home/docker-admin/mc && docker compose up -d mc
docker exec urania-mc grep -E '^white-list|^enforce-whitelist' /data/server.properties
```

影响面（已确认接受）：生效瞬间，**不在白名单里的非 OP 玩家会被踢出且进不来**；
8 个 OP 不受影响（`PlayerList.isWhiteListed` 对 OP 直接放行）。

## 运维与排错

```bash
cd /home/docker-admin/elicloud/mc
docker compose ps
docker compose logs -f mc
curl -s http://127.0.0.1:8001/healthz               # 仅回环映射（8000 已被 sso 占用）
docker compose exec mc python -m app.cli list-names
docker compose exec mc python -m app.cli list-audit --limit 20
docker compose exec mc python -m app.cli whitelist-list
docker compose exec mc python -m app.cli reconcile --apply
docker compose exec mc python -m app.cli force-remove mn_0001
```

| 现象 | 排查顺序 |
|---|---|
| 所有请求 401 | 比对 `SSO_ISSUER` 与 SSO 的 `PUBLIC_BASE_URL` 是否逐字相同；确认客户端送的是 access token 而不是 id_token |
| 所有请求 403 `insufficient_scope` | 令牌里没有 `mc:whitelist`：**用户需要重新登录**一次拿新令牌 |
| `/mc/*` 返回 404 | 网关站点块是否生效（`caddy validate` / `reload`）；站点块地址是否也带 `/mc/*` |
| `/healthz` 报 `reachable=false` | MC 容器是否在跑；`mc_default` 网络是否还在（`mc` 被 `compose down` 重建会让它消失 → 重跑 `deploy.sh`）；容器是否同时加入两张网 |
| `whitelist add` 成功但玩家进不去 | 白名单是否真的启用；UUID=随机 v4 的行不生效（§12.1） |
| 库里说加了、白名单里没有 | `python -m app.cli reconcile`；`audit_logs` 里找该条的 RCON 原文与 `result` |
| 重建 MC 容器后连不上 RCON | 网络重建导致；`docker compose up -d --force-recreate mc` |

**备份**：`./data/mc.db`（绑定 + 审计）。白名单本身由 MC 侧备份。

## 验收（§9）

* **§9.1 单元/契约测试**：92 passed（令牌 401/403 全谱、name/note 正则、幂等、名额、唯一性、
  IDOR 404、大小写、RCON 挂掉 503 不落库、lying_add 回读拦截、撤回不确认不改库、审计留痕、
  限流、healthz 含 RCON 可达性）。一条命令：`pytest -q`。
* **§9.3 部署后线上冒烟**（待部署完成后执行）：`/mc/healthz` 200 且 `rcon.reachable=true`；
  真实令牌 POST 201 且 MC 内 `whitelist list` 可见；第 3 个 409 `quota_exceeded`；
  DELETE 后 MC 内消失；`id_token` → 401；无令牌 `/mc/v1/names` → 401（不是 404）；日志 grep 令牌原文零命中。
* **§9.4 白名单真正生效**：启用 `WHITELIST`/`ENFORCE_WHITELIST` 后，未加白账号被拒、加白后可进、OP 始终可进。

## 安全清单（§10 自查结论）

| # | 项 | 本实现 |
|---|---|---|
| 1 | 命令注入 | 用户名过 `^[A-Za-z0-9_]{3,16}$`，**RCON 层再校验一次**（`guard_name`）；备注不进任何 RCON 命令 |
| 2 | 越权 IDOR | `DELETE` 只允许 `user_id == sub`，不属于自己一律 404 |
| 3 | 身份伪造 | RS256 白名单；`iss`/`aud` 逐字校验；拒绝 `alg:none`/HS256；拒绝 `id_token` |
| 4 | 密钥泄漏 | JWKS 只读公钥；RCON 密码只在服务器 `app.env`，不进仓库/镜像/日志/响应 |
| 5 | 日志安全 | 只记 `sub`/`name`/结果/耗时；有专门的「令牌原文不出现在响应与日志」用例 |
| 6 | 限流 | 提交/撤回按 `sub` + IP 双维度；管理员认证按 IP 限流 |
| 7 | 网络暴露 | 容器不发布公网端口（仅回环调试映射）；RCON 只在 `mc_default` 内可达 |
| 8 | 内部接口 | `docs_url`/`redoc_url`/`openapi_url` 全部关闭（有用例锁定） |
| 9 | 请求体 | `extra="forbid"`，未知字段直接 400 |
| 10 | 审计不可抵赖 | 成功、失败、拒绝都写 `audit_logs`（拒绝写入按 IP 限流，防刷表） |
| 11 | 拒绝服务 | RCON 串行（进程内 RLock）+ 超时；写操作不自动重试 |
| 12 | 备注内容 | 长度 1–200 + 控制字符过滤 |

## 已知限制

1. **`uuid` 恒为 `null`**：只走 RCON（决策 7），而 `whitelist list` 不返回 UUID，
   读 `whitelist.json` 又因 MC 数据目录是匿名卷而不可行。规格 §5.6 允许「只作记录」。
2. **离线模式 UUID 语义不稳定**（规格 §12.1）：随机 v4 的行可能不生效，
   正确修法是让玩家先登录一次再加白，**不是**让本服务去猜 UUID。
3. **本期只服务一台 MC**（决策 T5）：表结构将来加 `server_id` 演进。
4. **限流是进程内计数**：多副本时各副本独立计数（当前单进程部署）。
5. **CI/CD 尚未接入**（决策 T7「先手工部署」）：`deploy/deploy.sh` 已按框架写好，
   后续按 `sso/deploy/deploy.sh` 的形状改成「Actions 构建推镜像 → 这里拉镜像」即可。
