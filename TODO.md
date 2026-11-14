# 安全与加固待办事项

来源：2026-11-13 对仓库的只读安全/架构评审。本文档只登记待修复问题与建议方向，
不代表任何一项已经实现；修复时按 `.codex/rules/workflow.md` 走分析-计划-实现流程。

优先级：P0 立即修复；P1 安全加固；P2 部署与运维；P3 代码质量；P4 工程卫生。

## P0 — 立即修复

### 1. OAuth 换 token 无条件禁用 TLS 校验

- 位置：`services/oauth_login_service.py` `_exchange_code`（约 196 行）
- 问题：`build_session_kwargs(impersonate="chrome", verify=False)` 显式传入的
  `verify=False` 会透传给 `requests.Session`，无论 `skip_ssl_verify` 设置如何，
  `/api/accounts/oauth/token` 的 code→token 兑换永远不校验证书。该请求承载
  `code_verifier + code` 并返回 AT/RT/ID token，中间人可窃取 refresh_token。
- 对比：AT 刷新（`services/account_service.py` 约 1324 行）、CPA
  （`services/cpa_service.py` 约 203/251 行）、Sub2API（`services/sub2api_service.py`
  约 336 行）、R2（`services/backup_service.py` 约 112 行）、上游会话
  （`services/openai_backend_api.py` 约 257 行）均为 `verify=True` 或跟随设置。
- 建议：改为 `verify=True`；若当初是为绕过代理/指纹问题才关闭，需注释说明并评估
  替代方案（如仅对该 host 固定 CA）。
- 验证：单元测试断言 session kwargs 中 `verify is True`；手工走一遍 OAuth 导入。

## P1 — 安全加固

### 2. 上游账号凭据静态加密策略缺失

- 位置：`services/storage/database_storage.py` `AccountModel`（约 27-41 行）
- 问题：`accounts.data` 为完整账号 JSON（AT/RT/ID token、指纹、代理配置），
  全库无静态加密；`data/chatgpt2api.db` 或 PG 读权限即等于全部上游凭据。
  R2 备份有可选 openssl AES-256-CBC+PBKDF2 加密，本地库反而无加密选项。
- 建议：短期至少在 `docs/deployment.md` 写明 `data/` 权限与磁盘加密要求；
  中长期评估字段级加密（需 ADR，涉及备份/迁移/检索影响）。
- 验证：部署文档评审 +（若做加密）迁移与备份回归。

### 3. 日志脱敏双轨，logger 侧覆盖面窄

- 位置：`utils/log.py` `Logger._sanitize`（约 67-85 行）与
  `utils/diagnostics.py` `sanitize_diagnostic_text` / `scrub_diagnostic_value`
- 问题：两套脱敏并存。`utils/log.py` 只按键名含 `token`/`dx` 或 base64/data-URI
  脱敏，`password`/`secret`/`api_key`/`cookie`/`authorization` 键名不 mask；
  直出 dict 时存在泄露面。违反"唯一所有者"规则。
- 建议：收敛到 `utils/diagnostics.py` 单一实现，`Logger._sanitize` 委托给它；
  补齐 `_DIAGNOSTIC_SECRET_KEYS` 之外的常见密钥键名。
- 验证：对含 password/api_key/cookie 的 dict 走 `logger.info` 断言输出被 mask。

### 4. WebDAV 客户端与全局代理/安全体系脱节

- 位置：`services/image_storage_service.py` `WebDAVClient`（约 176-261 行）
- 问题：裸 `requests.Session()` 不走 `proxy_settings`（绕过代理与
  `skip_ssl_verify` 约束）；Basic auth 明文凭据，`webdav_url` 为 `http://` 时
  凭据与图片明文传输；`get()` 无大小上限全量入内存；除 `test()` 外 session
  不 close。
- 建议：settings 校验对 `webdav_url` 强制 https 或至少警告；`get()` 加响应
  大小上限；统一 session 生命周期。
- 验证：设置页 http:// WebDAV 的校验提示；大文件 get 的上限测试。

### 5. 认证接口无失败限流，弱密钥可在线爆破

- 位置：`api/support.py` `require_identity`；`services/auth_service.py`
  `authenticate`；`api/system.py` `/auth/login`
- 问题：错误 key 无尝试次数限制/延迟。`sk-` 生成的 User Key 有 192bit 熵不可
  爆破，但管理员手设的弱 `CHATGPT2API_AUTH_KEY` 可被在线枚举。
- 建议：对认证失败加入按 IP/全局的失败计数与指数退避；文档要求 auth key
  最小长度/熵。
- 验证：连续错误请求触发限流；正常 key 不受影响。

## P2 — 部署与运维

### 6. 无 TLS，必须依赖反向代理

- 位置：`Dockerfile`（EXPOSE 80）、`docker-compose.yml`
- 问题：服务只听 HTTP，Bearer key 明文传输；compose/文档未强制提醒。
- 建议：`docs/deployment.md` 增加"公网部署必须 HTTPS 反代"的硬性说明；
  可选提供 Caddy/Nginx 示例片段。
- 验证：文档评审。

### 7. 容器以 root 运行

- 位置：`Dockerfile`（无 `USER` 指令）
- 问题：应用与 `/app` 运行时卷均由 root 写入。
- 建议：新增非 root 用户并 `chown` 运行目录；验证 entrypoint 的
  `uv sync` 与数据卷写入在非 root 下可用。
- 验证：`docker compose up` 后 `docker exec` 确认非 root、功能回归。

### 8. CORS 全开

- 位置：`api/app.py` `CORSMiddleware`（约 116-123 行）
- 问题：`allow_origins=["*"]`。Bearer 认证下无 cookie CSRF，但任何来源的
  JS 拿到 key 即可调用，放大泄露面。
- 建议：改为配置化允许来源（默认同源/空），或保持 `*` 但文档说明取舍。
- 验证：跨域请求行为回归。

### 9. Compose 缺 app healthcheck 与资源限制

- 位置：`docker-compose.yml`、`docker-compose.postgres.yml`
- 问题：仅 postgres 有 healthcheck；app 无；无 CPU/内存限制。
- 建议：为 app 增加 healthcheck（如 `/version` 或 `/auth/status`）与可选
  `deploy.resources` 注释示例。
- 验证：`docker compose ps` 显示 healthy。

### 10. Auth 快照多副本一致性窗口

- 位置：`services/auth_service.py`（约 21-22 行）
- 问题：key 快照 5s 刷新 + 30s 最大陈旧，多实例部署时禁用/删除的 User Key
  各副本生效时间不一致。
- 建议：文档化该窗口；若需强一致，评估共享失效通道（ADR 级决策）。
- 验证：文档评审。

## P3 — 代码质量

### 11. SVG 公开响应内联可执行脚本

- 位置：`services/image_service.py` `get_image_response`（约 42-43 行）、
  `get_thumbnail_response`（约 105-106 行）
- 问题：`.svg` 以 `image/svg+xml` 内联返回且无 `Content-Disposition`，
  浏览器直接打开可执行 SVG 内脚本（存储型 XSS 面，内容为系统生成）。
- 建议：SVG 响应加 `Content-Disposition: attachment` 或 `sandbox` CSP 头，
  或对 SVG 内容做消毒。
- 验证：直接访问 .svg URL 的响应头断言。

### 12. 列表类请求字段无上限

- 位置：`api/accounts.py` `AccountCreateRequest`/`AccountDeleteRequest` 等
  （约 83-143 行）
- 问题：`tokens`/`accounts`/`access_tokens` 等 `list` 无 `max_items`，
  admin-only 但 body 无界解析。
- 建议：按业务上限加 `max_length`。
- 验证：超限请求返回 422。

### 13. `download_images_zip` 内存无上限

- 位置：`services/image_service.py`（约 314-346 行）
- 问题：全量读文件进内存 BytesIO 打包，大批量时内存放大。
- 建议：限制单次打包文件数/总字节，或改流式临时文件。
- 验证：大批量打包的内存与上限测试。

### 14. Sub2API `base_url` 保存时无 scheme 校验

- 位置：`services/sub2api_service.py` `_normalize_server`（约 144-160 行）
- 问题：仅 `_clean`，配 `http://` 时登录密码明文传输。
- 建议：保存时校验 `https://`（或允许 http 但警告）。
- 验证：设置非法 base_url 的校验提示。

### 15. 认证比较非恒定时间

- 位置：`api/support.py` `_legacy_admin_identity`（约 23-27 行）
- 问题：`token == auth_key` 普通比较，理论时序攻击面，实际风险极低。
- 建议：改 `hmac.compare_digest`。
- 验证：代码审查即可。

### 16. FastAPI 文档端点默认暴露

- 位置：`api/app.py` `create_app`
- 问题：`/docs`、`/openapi.json` 默认开启，泄露 API 结构（低敏感）。
- 建议：生产部署关闭（`docs_url=None` 等）或按认证开关控制。
- 验证：关闭后路由 404。

### 17. OAuth 链路调试 print 残留

- 位置：`api/accounts.py`（约 2269-2274 行）、
  `services/oauth_login_service.py`（约 238-243 行）
- 问题：向 stdout 打印 session/callback 元数据与上游原始响应片段，
  非密钥但生产日志噪音且未经脱敏。
- 建议：改走 `utils.log.logger` 并过脱敏，或删除。
- 验证：日志输出审查。

## P4 — 工程卫生

### 18. 版本元数据漂移

- 位置：`VERSION`/`pyproject.toml`（3.2.9）vs `README.md` badge 与
  `README_EN.md`（v3.2.3）
- 问题：发布线版本号不一致。
- 建议：对齐 badge/文档与 `VERSION`。
- 验证：文档 diff。

### 19. 测试不入库且 CI 无 pytest

- 位置：`.gitignore`（`tests/`、`web-vue/tests/`）、
  `.github/workflows/docker-publish.yml`
- 问题：后端 `tests/` 与前端 `web-vue/tests/` 被 gitignore，CI 只跑
  `compileall` + 前端构建；安全敏感路径（路径遍历、SSRF、脱敏）无回归网。
- 建议：至少把安全相关测试纳入版本库并接入 CI；或建立私有测试包的发布流程。
- 验证：CI 出现 pytest 步骤并通过。

## 已评估、记录为接受风险（无需修复）

- `/files/`、`/images/`、`/image-thumbnails/`、`/version` 公开端点：设计上
  靠不可猜 URL，路径校验已到位；如需更强保护可在反代层加规则。
- `proxy_runtime.skip_ssl_verify`：用户显式配置项，默认关闭。
- Bearer key 无过期/无会话：设计如此，轮换靠禁用/删除 User Key。
- ToS 合规与账号封禁风险：业务决策，非代码缺陷，部署文档已有警告。
