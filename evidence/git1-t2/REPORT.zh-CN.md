# GIT-1 候选 T2 验收报告：IBM ContextForge v1.1.1

报告时间：2026-11-25 11:47–11:51（亚洲/新加坡，UTC+8）。
仓库：https://github.com/gaochang731-hash/mcp-context-forge
只读对照的上游仓库：https://github.com/IBM/mcp-context-forge
本轮只改本 fork 的测试分支，没有向 IBM/mcp-context-forge 推送或开 PR。

## 结论

| 项 | 结论 |
| --- | --- |
| 安装启动 | PASS |
| 合成闭环 | PASS（SubscribeToTask 不是可用订阅，见缺口） |
| 隔离/安全 | FAIL |
| 故障恢复 | PASS |
| 真实 Grok Bot | 未测 |
| 豆包工作台 | 未测 |
| Hermes | 未测 |
| 三平台联合 | 未测 |

隔离/安全 FAIL 的直接原因：官方 logout 后，同一会话令牌在约 30 秒内仍能访问受保护接口；35 秒后才返回 401。撤销存储被移除时，实测路径是拒绝而不是放行。团队可见的 A2A 资源隔离成立。不能把本轮写成生产多公司门户通过。

建议：保留为“网关必须能访问后端”的本地治理候选。放弃把它当作操作员电脑可关机、只出站、跨公司远程控制的方案。不需要新的付费授权。不要做公网部署。

## 版本与许可

- 运行提交：`dbc643befb6f412e62584179c8f0ebb0207d44fb`
- 该提交是上游 annotated tag v1.1.1 指向的提交。本 fork 没有 v1.1.1 标签名。
- 应用自报版本：ContextForge 1.1.1，MCP protocol `2025-11-25`
- 研究快照 `be5ba95c1945cf1cff68f930148288eb3de13b6a` 未运行。本报告的结论只属于 v1.1.1。
- 镜像：未使用容器。无镜像 digest。
- LICENSE：Apache License 2.0。`pyproject.toml` 的 `license` 为 `Apache-2.0`。`LICENSE` 文件为 Apache 2.0 正文。

## 环境与实测资源

不写最低硬件或操作系统要求。下面都是本虚拟机实测。

- OS：Ubuntu 24.04.5 LTS，Linux 6.12.113+ x86_64
- CPU：4
- 内存：MemTotal 16387328 kB。跑完主流程时 MemAvailable 约 4526924 kB。
- Python：3.12.3
- 依赖：fastapi 0.162.0，uvicorn 0.54.0，sqlalchemy 2.1.6，pydantic 2.14.2，alembic 1.20.1，cryptography 50.0.2，cpex 0.1.4，mcp 2.3.0，httpx 0.28.1，PyJWT 2.15.1
- 数据库：SQLite 3.45.1，文件只在 `/tmp/git1-t2/mcp.db`
- Redis：`/version` 显示 URL 仍是 `redis://localhost:6379/0`，reachable=false。`CACHE_TYPE=database`，没有连接 Redis。
- 网关进程 RSS：276440 KiB。两个合成 peer RSS：21196 KiB 与 22352 KiB。
- 主流程墙钟：23.718 秒（11:47:12 到 11:47:35）。其中首次网关到 `/health` 约 5 秒，重启后再到 `/health` 5.286 秒。
- 依赖安装：uv 0.13.10，`uv sync --python 3.12 --no-dev --no-install-project --frozen`，安装 93 个运行时包，耗时约 3 秒。之前 `pip install uv` 另计。

## 启动步骤

1. 在 fork 的上述提交上创建分支 `cursor/git1-t2-acceptance-afb6`。
2. 用 CPython 3.12.3 安装运行时依赖，不构建 Admin UI 包。
3. 在 `127.0.0.1:9101` 启动公司 A 合成 peer，协议 1.0，支持取消。
4. 在 `127.0.0.1:9102` 启动公司 B 合成 peer，协议 0.3，不支持取消。
5. 在 `127.0.0.1:4444` 启动网关。认证打开。A2A 打开。缓存用数据库。可观测性关闭。插件关闭。
6. 本轮把 `SSRF_ALLOW_LOCALHOST=true`。这是回环测试例外。生产默认 `false`。同一提交在默认值下对 `http://127.0.0.1:9101/` 返回 `UrlPolicyError`，localhost 被 SSRF 策略挡住。
7. `HTTPX_READ_TIMEOUT=5`，用来观察上游超时。
8. Admin UI 与 Admin API 只在这台回环地址打开，用来核对开发界面和 GHSA 管理入口。`SECURITY.md` 写明原生 Admin UI 只供开发使用。

临时口令、JWT 密钥、上游 bearer 只放在 `/tmp/git1-t2`，权限 0600。证据文件已扫描，这些值不在仓库证据里。

## 合成闭环

两个 peer 都是独立进程，不是 ContextForge 内置聊天 agent。它们按消息文本做确定性动作，不调用模型。

公司 A 使用各不相同的上游 bearer。公司 B 使用另一个上游 bearer。调用方的用户令牌不是上游令牌。

### 协议与方法

网关入口：`POST /a2a/{agent_name}/jsonrpc`。网关把 JSON-RPC 转发到登记的 endpoint，并按登记的协议改写方法名。

| 调用方方法 | 1.0 peer 实际收到 | 0.3 peer 实际收到 |
| --- | --- | --- |
| SendMessage | SendMessage | message/send |
| message/send | SendMessage | 未单独再测；映射表会把它留在 0.3 名称 |
| GetTask | GetTask | tasks/get |
| ListTasks | ListTasks | tasks/list |
| CancelTask | CancelTask | tasks/cancel |
| SubscribeToTask | SubscribeToTask | tasks/resubscribe |

1.0 状态字样是 `TASK_STATE_*`。0.3 状态字样是 `working` / `completed`。1.0 角色是 `ROLE_USER` / `ROLE_AGENT`。0.3 角色是 `user` / `agent`，artifact 带 `kind`。

SubscribeToTask 有方法名，也完成了 0.3/1.0 名称改写。它不是订阅流。网关只发一次 POST。两个 peer 都返回 JSON-RPC `-32601`，HTTP 仍是 200。本 release 不能把 SubscribeToTask 记为可用的任务订阅。

### 任务时间线（UTC+8）

- 11:47:20 公司 A `SendMessage` 文本 `GIT1-TEST-001`。task `ea23a602c378464b9c865d45e9ea8405`，context `11668e6e49904291923e58df44273d30`，状态 `TASK_STATE_COMPLETED`，artifact 文本 `GIT1-TEST-001`。后端动作 1。
- 11:47:20 `GetTask` 与 `ListTasks` 返回同一 task。
- 11:47:21 公司 B 收到 `message/send`。task `8c56b802282d4fc780a2e97a7df4f0db`，状态 `completed`，artifact 文本 `GIT1-TEST-001`。后端动作 1。
- 11:47:22 公司 A `SLOW-CANCEL`。task `2c88af4bb67f4930ad3872538c771314` 先是 `TASK_STATE_WORKING`。`CancelTask` 后为 `TASK_STATE_CANCELED`。peer `cancel_applied=1`。之后 `GetTask` 仍是取消，没有变成完成。
- 11:47:22 公司 B 同一慢任务 `b2fb3718de8f48b0871230678840967f`。`tasks/cancel` 返回 JSON-RPC `-32004`，HTTP 200，文案是后端不支持取消且任务继续。`cancel_applied=0`，`cancel_rejected=1`。最终 peer 自己把该任务跑到 `completed`。
- 网关 REST `POST /a2a/{agent_id}/tasks/{task_id}/cancel` 对团队成员令牌返回 404 `Agent not found`。peer 的 cancel 计数没有增加。登记所有者是 `admin@example.com`，成员不是所有者。这条 REST 取消没有打到后端。源码说明该路由只改网关本地任务行，不通知远端。本轮没有用所有者令牌打通这条 REST。删登记也不是停任务：11:47:28 删除 `company-a-ephemeral` 后，peer 上 task `f99b5e6534494a23bd7c3509d5a975bf` 仍是 `TASK_STATE_WORKING`，网关再查该名称返回 404。

### 后端动作计数

公司 A peer，协议 1.0，支持取消：

- HTTP POST 16，未授权 1，业务动作 9，SendMessage 10，GetTask 3，ListTasks 1
- 取消请求 1，取消生效 1，订阅请求 1，重复 messageId 1，JSON-RPC 错误 1，绕过网关的直接调用 1

公司 B peer，协议 0.3，不支持取消：

- HTTP POST 7，业务动作 3，SendMessage 3，GetTask 1，ListTasks 1
- 取消请求 1，取消生效 0，取消拒绝 1，订阅请求 1
- 超时开始 1，网关侧超时后后端仍完成 1

## 隔离与安全：FAIL

团队边界本身成立。总评 FAIL 是因为撤销不能立刻生效，以及 public 登记和 Agent Card 会把后端地址交给另一侧。

### 默认角色叠加

新建非管理员用户立刻得到两份角色，这是数据库事实，不是界面隐藏：

- 全局 `platform_viewer`
- 个人团队上的 `team_admin`

`platform_viewer` 含 `a2a.read`，不含 `a2a.invoke`。`team_admin` 含 `a2a.create`、`a2a.read`、`a2a.update`、`a2a.delete`、`a2a.invoke`。

`GET /rbac/users/{email}/roles` 的 `role_name` 为 null。`GET /rbac/my/permissions` 与管理员的权限列表只返回 platform_viewer 那一组，不列出 team_admin 的 A2A 写权限。同一用户仍能创建 `scope.permissions=["a2a.invoke"]` 的 API 令牌，说明委托检查看得到 team_admin 权限。权限展示和实际委托不一致。

随后把用户加入虚构公司团队，成员角色是 `viewer`，并额外授予自定义角色 `git1_a2a_invoke`（只有 `a2a.read` 与 `a2a.invoke`）。个人团队上的 `team_admin` 没有被摘掉，因为接口没有返回 role name，摘除条件没有命中。

会话令牌是 `token_use=session`，`teams=null`，`scopes.permissions=["*"]`。团队由数据库解析。
API 令牌是 `token_use=api`，`teams` 只有被指定的一个公司团队，权限就是创建时写入的列表。

### 团队负例

公司 A 令牌 `teams=[Company A]`，权限 `a2a.read` + `a2a.invoke`：

- 能列出本公司 team agent 和 public agent。
- 能调用公司 A peer 并取回 task。

公司 B 的同等令牌：

- 列表里没有 `company-a-agent`。有 `company-b-agent` 和 `public-synthetic`。
- `GET /a2a/{company-a-id}/card` 返回 404 `agent not found`。
- 对 `company-a-agent` 的 `GetTask` 返回 404，JSON-RPC `-32001`。
- `GET /a2a/{company-a-id}/tasks/{真实 task}` 与猜测 task id 都是 404，正文都是 agent not found。不存在的 agent 和看不见的 agent 用同一句话。
- 只含 `a2a.read` 的公司 A 令牌调用 `SendMessage` 返回 403 `Access denied`。网关注册读权限不等于后端执行权限。

无令牌调用返回 401 `Authorization token required`。

public agent 不是隔离。公司 B 令牌对 `public-synthetic` 的 `SendMessage` 返回 200，task `e6500536482d436ea3658dd09cddbde5`，artifact `GIT1-PUBLIC`，打到公司 A 的同一进程。列表和卡片都带 `endpointUrl` `http://127.0.0.1:9101/`。`public` 在这里是平台内公开，不是匿名公开。

### GHSA-qmfj-jjw4-8qrq

本提交的 `update_agent()` 跳过 `owner_email` 更新，并在提供用户邮箱时做所有者检查。管理编辑入口会把用户邮箱传进去。

实测没有改掉所有者：

- 公司 B 的最小 API 令牌 `PUT /a2a/{company-a}` 与 `POST /admin/a2a/{id}/edit`：403 `Access denied`。
- 公司 B 的会话令牌，当时已有个人团队 `team_admin`：同样 403。
- 再把 `team_admin` 授到公司 B 团队后重试：仍是 403 `Access denied`。
- 之后读取，name 仍是 `company-a-agent`，owner 仍是 `admin@example.com`，team 仍是 Company A。

拒绝发生在 RBAC 的 `Access denied`，没有走到“Only the owner”那句服务层错误。跨团队修改没有成功。本轮没有复现该 GHSA 的可利用路径。

### Agent Card 与绕过

公司 A 调用方能读到的 card 含：

- `supportedInterfaces[0].url = http://127.0.0.1:9101/`
- `protocolBinding = JSONRPC`
- `protocolVersion = 1.0`
- `securitySchemes.bearerAuth` 只有 `scheme=bearer`，没有上游密钥

直接 POST 该 URL、不带上游 bearer：401。带上游 bearer 且带测试标记：200，task `158bfe51d8b2426daa04082a7710943f`，peer `direct_calls=1`。这条调用不经过网关的用户 RBAC。SDK 只要同时有私网地址和上游凭据，就能绕过网关。地址会从已授权读者的 card 和 agent 读接口泄露。上游密钥在 API 里是 `*****`，本轮没有从 API 读出明文。

### 撤销

存储可用且缓存未命中时，撤销有效：

- 先写入 `token_revocations` 再第一次使用该会话令牌：401 `Token has been revoked`。
- API 令牌 `DELETE /tokens/{id}` 返回 204。之后使用返回 401 `Token has been revoked`。

存储不可用时，实测是失败关闭，不是失败开放：

- 把 `token_revocations` 改名后，已撤销令牌和新签发令牌访问 `/a2a` 或 `/rbac/my/permissions` 都是 401 `Token validation failed`。
- 日志原文是 `denying access (fail-secure)`，并写明 `no such table: token_revocations`。见 `evidence/git1-t2/fail-secure-log.txt`。
- `docs/docs/architecture/security-features.md` 仍写撤销存储不可用时 auth 路径 fail open。`docs/docs/manage/securing.md` 也有同类句子。文档和这条运行路径不一致。
- `mcpgateway/utils/verify_credentials.py` 里 `_enforce_revocation_and_active_user()` 仍在异常时只记日志并继续。本轮 `/a2a` 与 `/rbac` 走的是 `mcpgateway.auth` 的 fail-secure 分支，没有走那条吞掉异常的分支。

官方 `POST /auth/logout` 会写入撤销并返回成功，但立刻再用同一令牌访问 `/rbac/my/roles` 仍是 200。等待 35 秒后再访问，返回 401 `Token has been revoked`。默认 `auth_cache_revocation_ttl` 是 30 秒。撤销不是即时的。这是本轮隔离/安全 FAIL 的主证据。它和“表不存在就放行”不是同一件事。

### 密钥与日志

`a2a_agents.auth_value` 长度为 119，不包含两个上游明文。API 返回 `authValue` 与 `authToken` 为 `*****`。网关日志和 peer 日志不含上游 bearer、管理员口令或 JWT 密钥。

结构化日志能关联 `user_email`、`agent_name`、`agent_id`、`correlation_id`、协议版本和 `endpoint_url`。`team_id` 为 null。调用日志没有 task id。`audit_trails` 行数为 0。`OBSERVABILITY_ENABLED=false`。这些日志不是防篡改审计，也不能代替任务台账。日志里有回环地址。

## 故障恢复：PASS

没有把失败报成完成。

- 上游睡眠长于 5 秒读超时。网关在 5.031 秒返回 HTTP 400，JSON-RPC `-32603`，消息为 `Failed to invoke A2A agent:`，没有 completed。公司 B 后端随后仍完成 task `2260218984ac4d52a15d5ec940b24e2e`，计数 `timeouts_finished_after_client_may_have_given_up=1`。网关超时不等于后端停止。
- 后端以 HTTP 200 返回 JSON-RPC error `-32000`。网关也以 HTTP 200 把 error 交还调用方，没有 result，也没有新的完成任务。只看 HTTP 状态会误判成功。正文没有完成。
- 同一 `messageId` 连续两次 `SendMessage` 产生两个 task：`22f73b08e71345fe96b4b0622ee32386` 与 `6b10dc69d9a8447989eee150b990e318`。peer `duplicate_message_ids=1`。没有恰好一次。
- 11:47:28 杀掉网关进程。peer 仍在，慢任务 `c2945c75007944cf8aa1be38cf25368a` 保持 `TASK_STATE_WORKING`。11:47:35 网关重新起来后，`GetTask` 返回该 working 状态，没有被报成完成。

## 本地管理面和远程控制

本地管理面可以启动。`GET /admin` 无令牌为 401。`GET /admin/login` 为 200，HTML 标题是 `Sign In - ContextForge`。`GET /docs` 无令牌为 401。这是开发登录页，不是零基础可用的多公司门户。零基础用户不能靠这个页面完成团队令牌、任务状态和取消。可用的控制面是带 bearer 的 HTTP API。

这不是对独立远程 agent 的远程控制证明。

- 本轮全部成功调用都在同一台隔离虚拟机的回环地址上。证明类别：仅本地。
- 网关必须向 backend URL 发 HTTP。生产默认 SSRF 拒绝 localhost。跨网使用需要后端从网关侧可达。证明类别：需要入站到后端。本轮没有公司网络。
- 只出站队列或连接器：未测，不能写 PASS。日志写明 UAID 跨网关路由处于关闭，允许列表为空。
- 只杀掉网关进程时，同一台机器上的 peer 继续保存任务。这只证明进程分开。
- 操作员电脑关机、睡眠或断网：若网关和 agent 都在那台电脑上，两者都停止。本轮没有、也不会装到用户的个人电脑。
- 不需要域名或公网服务才能完成本地测试。没有新的云账号，没有费用。若要给不在这台机器上的人用，就需要额外的可达地址和托管，那不在本轮许可内，也没有报价。
- 不需要常驻中继才能完成本地测试。本版本也没有证明一个可替换的常驻中继。

## 缺口

- SubscribeToTask 没有可用的事件流。
- 网关 REST 取消没有通知后端。团队成员打不开该路由。
- 删除登记不会停止 peer 上的任务。
- JSON-RPC 业务错误使用 HTTP 200。
- 重复派发会产生两个 task。
- logout 后约 30 秒内旧会话仍可用。
- 安全文档仍写 fail open，运行路径对表缺失是 fail secure。另一段代码仍吞掉撤销异常。
- 权限列表不显示个人团队 `team_admin` 的实际权限。
- public agent 对其他公司可见，并公开回环 URL。
- Agent Card 对授权读者公开后端 URL。持有上游密钥即可绕过网关。
- 审计表为空。调用日志没有 team id 和 task id。
- 原生 Admin UI 不能验收为生产多租户门户。

## 阻断、费用与建议

本轮没有新的授权阻断，也没有费用。没有公网部署，没有隧道，没有真实模型密钥。

真实 Grok Bot、豆包工作台、Hermes、三平台联合：全部未测。

建议继续的范围：只把 v1.1.1 当作本地、后端必须从网关可达时的 A2A 代理和团队可见性网关。建议放弃的范围：操作员电脑关闭后仍远程控制独立 agent、只出站、或把 Admin UI 当多公司生产门户。隔离/安全总评是 FAIL，原因是撤销延迟和 public/card 地址暴露，不是因为团队可见 agent 被另一公司读到。
