# 07: Development Tasks — chatgpt2api

Status: draft（待用户审核批准）

配套 `06-improvement-roadmap.md`。每个任务卡含：来源、目标、当前实现、
建议实现、涉及文件、技术方案、工作量、风险、前置依赖、验收、测试、回滚。
工作量为 T 恤尺码（S<1天当量、M 1-3 天、L 3-5 天、XL>5 天，仅相对估算）。
所有任务默认遵循 `.codex/rules/workflow.md`：分析→计划→同步→实现→验证。

---

## Phase 1 — P0 安全止血

### T-01 OAuth 换 token 启用 TLS 校验

- **来源**：SEC-01（Critical）
- **目标**：code→token 兑换恢复证书校验，消除 refresh_token 被中间人窃取路径。
- **当前实现**：`services/oauth_login_service.py:196` `_exchange_code` 中
  `proxy_settings.build_session_kwargs(impersonate="chrome", verify=False)`，
  `verify=False` 无条件透传给 `requests.Session`。
- **建议实现**：改 `verify=True`。若当初为绕过代理/指纹问题才关闭，先在
  Issue/注释中确认根因；推荐方案=直接 True（与全仓其他凭据调用一致）。
  备选=仅对 `auth_base` host 固定 CA bundle。
- **涉及文件**：`services/oauth_login_service.py`（1 行）；
  可能 `services/proxy_service.py`（若 build_session_kwargs 需调整）。
- **技术方案**：最小改动；`build_session_kwargs` 已支持 verify 参数，
  直接改调用点。
- **工作量**：S。**风险**：低（若上游证书链有特殊问题会暴露——那是
  应暴露的问题）。
- **前置依赖**：无。
- **验收**：单测断言 `_exchange_code` 构造的 session kwargs `verify is True`；
  手工走一遍 OAuth 导入全流程成功。
- **测试**：新增 `tests/` 单测（本地）断言 kwargs；OAuth 真实导入 smoke
  （需真实账号，仅手工）。
- **回滚**：单行 revert。

### T-02 依赖升级批次 A（starlette / python-multipart / anyio）

- **来源**：SEC-02（starlette 1.0.0，10 项 OSV）、SEC-04（python-multipart
  0.0.26）、SEC-05（anyio 4.13.0）
- **目标**：消除已确认漏洞且不破坏兼容。
- **当前实现**：`uv.lock` 锁定 starlette==1.0.0、python-multipart==0.0.26、
  anyio==4.13.0；`pyproject.toml` 约束 `fastapi>=0.136.0`（starlette 为
  传递依赖）。
- **建议实现**：
  ```bash
  uv lock --upgrade-package starlette --upgrade-package python-multipart \
         --upgrade-package anyio
  ```
  目标版本：starlette≥1.3.1、python-multipart≥0.0.30、anyio≥4.14.2。
  **注意**：fastapi 0.136.0 对 starlette 的版本约束需先确认（`uv pip show
  fastapi` 或查其 metadata）；若 fastapi 上限 <1.3.1，需同步升级 fastapi
  至兼容版本（OSV 显示 fastapi 0.136.0 本身 0 漏洞，升级要谨慎评估
  minor 兼容）。
- **涉及文件**：`uv.lock`、可能 `pyproject.toml`（若需抬 fastapi 下限）。
- **技术方案**：锁文件升级，最小侵入；分两步走——先 starlette+anyio，
  再 python-multipart，便于定位回归。
- **工作量**：M（含兼容性验证）。**风险**：中（starlette 1.0→1.3 跨
  minor，FastAPI 表单/异常处理行为需回归；python-multipart 解析边界）。
- **前置依赖**：无（建议与 T-03 分开验证）。
- **验收**：`uv export | pip-audit` 或 OSV 复查三包 0 命中；
  `/v1/images/edits` multipart+JSON 双 content-type 回归；表单超限
  urlencoded 请求被拒（验证 GHSA-82w8 修复生效——构造 urlencoded
  body 到 `parse_image_edit_request` 路径确认有 max_fields 约束）。
- **测试**：pytest 表单解析用例；手工 multipart/JSON/urlencoded 三种
  content-type 各发一次编辑请求。
- **回滚**：`git checkout uv.lock pyproject.toml` + `uv sync`。

### T-03 Pillow 13.x 升级

- **来源**：SEC-03（~88 项 OSV advisory，处理不可信图片）
- **目标**：消除图片解析器已知漏洞。
- **当前实现**：`pyproject.toml` `pillow>=12.2.0`；`uv.lock`==12.2.0；
  最新 13.0.0。API 使用面小：`Image.open`×12、`ImageOps.exif_transpose`×4、
  `Image.Resampling`×2、`Image.MIME`×2、`Image.new`×1、`Image.LANCZOS`×1
  （grep 统计）。
- **建议实现**：
  ```bash
  uv lock --upgrade-package pillow   # 目标 13.0.0
  ```
  若 13.0.0 与 fastapi/其他依赖冲突，退至 12.3.x（已修复部分 CVE，
  但非全部——12.3.0 修复了 mmap OOB 读等；13.0.0 修更多）。**优先 13.x**。
- **涉及文件**：`uv.lock`、`pyproject.toml`（下限可能需抬到 >=13）。
- **技术方案**：大版本升级，单独 PR 单独验证；重点回归图片全链路。
- **工作量**：M。**风险**：中高——Pillow 主版本可能移除/改名 API
  （`Image.LANCZOS`→`Image.Resampling.LANCZOS` 等，项目已混用需统一）；
  解码器行为差异可能影响图片输出。
- **前置依赖**：无（建议 Phase 1 内但与 T-02 分开）。
- **验收**：`python -c "from PIL import Image; print(Image.__version__)"`
  ≥13；OSV 复查命中数大幅下降（剩余为未修复的 0day 属正常）；
  图片生成/编辑/压缩/放大/格式转换全链路 smoke。
- **测试**：本地 pytest 图片用例；各格式（png/jpg/webp/gif/svg）
  上传→处理→输出回归；`_image_dimensions` 对样例图片断言。
- **回滚**：`git checkout uv.lock pyproject.toml`。

### T-04 前端依赖升级（axios / markdown-it）

- **来源**：SEC-10
- **目标**：消除 markdown-it ReDoS（渲染模型输出）与 axios 已知问题。
- **当前实现**：`web-vue/package-lock.json` axios==1.19.0、
  markdown-it==14.2.0；最新 axios 1.20.0、markdown-it 14.3.3+。
- **建议实现**：`cd web-vue && npm update axios markdown-it` 或精确
  `npm install axios@^1.20.0 markdown-it@^14.3.3`；`npm audit` 复查。
- **涉及文件**：`web-vue/package.json`、`web-vue/package-lock.json`。
- **技术方案**：常规依赖升级；markdown-it 为 minor 升级注意
  `html:false` 配置保持不变（studioMarkdownRenderer.ts）。
- **工作量**：S。**风险**：低（axios 漏洞多为 Node adapter，浏览器
  影响小；markdown-it minor 兼容好）。
- **前置依赖**：无。
- **验收**：`npm audit` 无 high/critical；Studio markdown 渲染正常；
  build 通过。
- **测试**：`npm run build`；Studio 对话 markdown 展示 smoke；
  构造含 linkify/smartquotes 边界的 markdown 文本渲染不断言崩溃。
- **回滚**：`git checkout package*.json && npm ci`。

### T-05 琐碎安全修复包

- **来源**：SEC-12/13/15/16/17、CQ-06（部分）
- **目标**：一批小而明确的安全/卫生修复，独立小 PR。
- **当前实现与建议**：
  1. `api/support.py:25` `token == auth_key` → `hmac.compare_digest`
     （需 import hmac；注意两侧 encode）。
  2. `api/app.py:114` FastAPI 文档端点：加环境变量开关
     `CHATGPT2API_DISABLE_DOCS`（默认生产关）或直接
     `docs_url=None, redoc_url=None, openapi_url=None`——**需决策：
     开发调试是否需要保留**（建议 env 开关，默认关）。
  3. `api/accounts.py` `AccountCreateRequest`/`AccountDeleteRequest`/
     `AccountExportRequest` 等 list 字段加 `max_length`（按业务上限，
     如 tokens/accounts ≤500，access_tokens ≤1000）。
  4. `services/image_service.py` SVG 响应加
     `Content-Disposition: attachment` 或 `Content-Security-Policy:
     sandbox` 头（`get_image_response`/`get_thumbnail_response`）。
  5. `services/sub2api_service.py:144-160` `_normalize_server` 校验
     `base_url` scheme ∈ {https}（http 允许但返回 warning 字段，或直接拒绝——
     **需决策**：是否允许 http 内网部署）。
  6. `services/oauth_login_service.py:238-243` 与 `api/accounts.py:2269`
     的 `print` 改走 `utils.log.logger` 并过 `sanitize_diagnostic_text`。
- **涉及文件**：上述 6 处。
- **工作量**：S-M（6 个子项）。**风险**：低。
- **前置依赖**：无。
- **验收**：各子项独立验证（见测试列）。
- **测试**：compare_digest 功能不变；docs 关闭后 404；超限 list 422；
  SVG 响应头断言；http sub2api URL 校验提示；日志无明文 print。
- **回滚**：逐子项 revert。

---

## Phase 2 — P1 稳定性与关键性能

### T-06 日志脱敏收敛

- **来源**：SEC-07、TD-02
- **目标**：单一脱敏所有者，消除 `utils/log.py` 覆盖面缺口。
- **当前实现**：`utils/log.py:67-85` `Logger._sanitize` 只按
  `token`/`dx` 键名或 base64/data-URI mask；`utils/diagnostics.py`
  有完善的 `sanitize_diagnostic_text`/`scrub_diagnostic_value`（正则级
  URL userinfo/Bearer/query/key=value）。
- **建议实现**：`Logger._sanitize` 内部委托 `utils/diagnostics.py`
  的统一实现；补齐 `_DIAGNOSTIC_SECRET_KEYS` 未覆盖的
  `password`/`secret`/`api_key`/`cookie`/`authorization` 键名。
  **技术决策**：保留 `utils.log` 作为 logger 入口（全仓引用），
  只把 mask 逻辑收敛到 diagnostics——不新建第三套。
- **涉及文件**：`utils/log.py`、`utils/diagnostics.py`。
- **工作量**：M。**风险**：中——日志输出变化，需确认无下游解析依赖
  旧 mask 行为；diagnostics 依赖方向（utils→utils 无环，安全）。
- **前置依赖**：建议在 T-13（循环依赖）之前做，因 diagnostics 是
  叶子模块。
- **验收**：`logger.info({"password": "x", "api_key": "y",
  "cookie": "z"})` 输出全部 mask；现有日志格式不变。
- **测试**：单测构造敏感 dict 断言 mask；抽查现有日志调用点输出。
- **回滚**：revert 两文件。

### T-07 WebDAV 客户端加固

- **来源**：SEC-08
- **目标**：消除 http 明文凭据、无界响应内存、session 泄漏、代理脱节。
- **当前实现**：`services/image_storage_service.py:176-261`
  `WebDAVClient`：裸 `requests.Session()`；`get()` 全量
  `bytes(response.content)` 无上限；仅 `test()` close session；
  不走 `proxy_settings`；`test()` 允许 http/https。
- **建议实现**：
  1. `get()` 改流式读取+大小上限（对齐图片 50MB 或独立上限）；
  2. session 生命周期：`__enter__/__exit__` 或每次操作后 close
     （评估 keep-alive 收益 vs 泄漏风险——WebDAV 操作低频，建议
     每操作新 session 或统一 finally close）；
  3. settings 校验：`webdav_url` scheme==https 强制或 warning
     （**需决策**：是否允许 http 内网）；
  4. 代理策略明确：文档注明 WebDAV 不走账号代理（独立基础设施），
     或接入 `proxy_settings.build_session_kwargs`（评估必要性——
     WebDAV 通常是管理员自有存储，代理需求低，**建议维持独立+文档化**）。
- **涉及文件**：`services/image_storage_service.py`、
  `services/config.py`（settings 校验）、`web-vue` 设置页提示（若加 warning）。
- **工作量**：M。**风险**：中。
- **前置依赖**：无。
- **验收**：http:// webdav_url 被拒或警告；超大响应触发上限；
  session 在操作后关闭（连接数断言）；正常 https WebDAV 上传/下载/删除回归。
- **测试**：mock WebDAV server（http.server）测大小上限与 session close；
  settings 校验单测。
- **回滚**：revert。

### T-08 认证失败限流

- **来源**：SEC-09
- **目标**：弱 admin key 不可在线爆破；不误伤正常 API 调用。
- **当前实现**：`api/support.py:30-46` `require_identity`/`require_admin`
  → `auth_service.authenticate` 无失败计数；`/auth/login` 同。
- **建议实现**：进程内失败计数器（按 IP+token 前缀 8 字符哈希），
  连续 N 次（如 5）失败后指数退避（1s→2s→…→60s 封顶）或临时 429；
  **只对失败认证生效**，成功即清零；`/auth/login` 与 `require_identity`
  共享同一限流器（单一所有者，放 `services/auth_service.py` 或新
  `services/auth_throttle.py`）。多实例部署时各副本独立计数（D4 单
  实例假设下可接受）。
- **涉及文件**：`services/auth_service.py`（或新文件）、`api/support.py`、
  `api/system.py`（login 路径）。
- **工作量**：M。**风险**：中——限流器自身 bug 可致全站 401；
  需精细测试边界。
- **前置依赖**：无。
- **验收**：连续 5 次错误 key → 第 6 次起延迟/429；正确 key 始终
  立即通过；不同 IP 独立计数；重启清零。
- **测试**：单测限流状态机；手工 curl 连续错误验证。
- **回滚**：revert；限流器设计为可整体摘除（feature flag 或
  独立模块易删）。

### T-09 图库索引优先加载

- **来源**：PERF-01
- **目标**：`GET /api/images` 不再每请求 `rglob` 全量扫盘。
- **当前实现**：`services/image_storage_service.py:735-776`
  `list_items` 默认 `refresh_index=True` → `root.rglob("*")` 全量
  遍历 + 新文件 `read_bytes`+`_image_dimensions`；`_load_clean_index`
  每次全量读 JSON。图库页每翻页/筛选都触发，持 `_index_lock`。
- **建议实现**（技术决策——三选一，推荐 B）：
  - **A. 保守**：`refresh_index` 改为 TTL 节流（如 30s 内跳过
    rglob，仍读索引）——改动最小，但索引仍有滞后窗口。
  - **B. 推荐**：列表请求只读索引（`refresh_index=False` 为主），
    新文件索引由**写路径**（图片生成完成/导入/上传时）同步建立 +
    后台定时全量对账（如每 5min 或启动时）补漏。需梳理所有
    "文件出现但索引未建"的路径（上游下载完成、WebDAV 同步、本地
    手动拷贝）。
  - **C. 激进**：索引迁 SQLite/JSONL 分页——工作量大，暂不建议。
- **涉及文件**：`services/image_storage_service.py`、
  `services/image_service.py`（写路径挂钩）、`services/retention_
  cleanup_service.py`（对账时机）、`api/system.py`（列表参数）。
- **工作量**：L。**风险**：中高——索引与文件系统一致性语义变化，
  漏挂写路径会导致"图生了但图库看不到"。
- **前置依赖**：需先梳理所有图片资产写入点（`save_image_bytes`、
  WebDAV sync、editable file 导出等）建立完整清单。
- **验收**：5000 文件目录 `GET /api/images` P99 显著下降；新生成
  图片立即出现在图库（写路径同步）；手动拷入文件在对账窗口内
  可见；删除/重命名一致性保持。
- **测试**：压测脚本；写路径→索引断言；对账任务单测。
- **回滚**：feature flag 回退到 `refresh_index=True` 行为。

### T-10 账号列表快照缓存

- **来源**：PERF-03
- **目标**：`GET /api/accounts` 不再每请求全量反序列化+投影。
- **当前实现**：`api/accounts.py:1099-1127` `_accounts_page` →
  `account_service.list_accounts()` 全量加载 `accounts.data` JSON
  blob → 内存过滤 → Python 分页 → 逐项 `_account_for_api` 投影。
- **建议实现**（推荐 A，B 为中长期）：
  - **A. 推荐**：`list_accounts` 结果加短 TTL 快照缓存（如 2-5s）
    + 所有写路径（增/删/改/刷新/分组/记忆操作）主动失效；
    过滤/分页仍在应用层（数据量可控时够用）。
  - **B. 中长期**：账号核心字段（email/status/group_id）结构化
    为独立列或轻量元数据表，过滤下沉 SQL——需 ADR，暂不做。
- **涉及文件**：`services/account_service.py`（缓存层）、
  `api/accounts.py`（失效点）、各写路径 service 方法。
- **工作量**：M。**风险**：中——缓存一致性（写后读必须立即可见，
  需严格失效时机）。
- **前置依赖**：无（独立于 T-09）。
- **验收**：1000 账号 `GET /api/accounts` 翻页延迟达标；写操作后
  立即读到新状态（失效断言）。
- **测试**：缓存命中/失效单测；写后读一致性集成测试。
- **回滚**：摘除缓存层恢复直读。

### T-11 配置读取优化

- **来源**：PERF-02/04
- **目标**：消除每请求 config.json 读盘与每秒 DB 轮询。
- **当前实现**：`services/config.py:485-490` `auth_key` property 每次
  `read_json_object` 读盘（`require_identity` 每请求触发）；
  `reload_if_changed` 1s 节流读 settings+groups 两仓储。
- **建议实现**：
  1. `auth_key` 加 TTL 缓存（如 2s）或并入 `reload_if_changed`
     统一刷新——**注意**：auth_key 是启动前提，文件变更需在
     TTL 内生效，TTL 不宜过长（建议 ≤5s）；
  2. settings 刷新保持 1s 节流（已合理）或拉长至 5s；评估是否
     改事件驱动（写路径失效）——写路径在 `settings_management_
     service.py`，可挂失效钩子，**推荐保留周期兜底+写时失效**。
- **涉及文件**：`services/config.py`、`services/settings_management_
  service.py`（失效钩子）。
- **工作量**：S-M。**风险**：低-中（配置生效延迟语义需文档注明）。
- **前置依赖**：无。
- **验收**：strace/计数确认 auth 请求不再每次读 config.json；
  修改 config.json 后 ≤TTL 生效；settings PATCH 后立即生效。
- **测试**：缓存 TTL 单测；写后读一致性。
- **回滚**：revert。

### T-12 输入边界补齐

- **来源**：SEC-13/14、PERF-07
- **目标**：无界输入/内存路径全部加上限。
- **当前实现**：`api/ai.py` `ChatCompletionRequest.messages` 无条数/
  体积上限（`extra="allow"`）；`api/accounts.py` list 字段（T-05 已
  覆盖 max_items）；`services/image_service.py:314-346`
  `download_images_zip` 全量内存；全局无 body-size 中间件。
- **建议实现**：
  1. `messages` 加 `max_length`（如 ≤1000 条）+ 总字符上限校验
     （在 `normalize_messages` 或契约层）；
  2. `download_images_zip` 加文件数（如 ≤500）/总字节（如 ≤2GB）
     上限，超限返回 413 或截断+警告；
  3. 全局 body-size：文档建议反代限制（`client_max_body_size`），
     应用层可选加中间件——**低优先，文档化即可**。
- **涉及文件**：`api/ai.py`、`contracts/`、`services/image_service.py`、
  `docs/deployment.md`。
- **工作量**：S-M。**风险**：低。
- **前置依赖**：无。
- **验收**：超限 messages/zip 请求被拒；正常请求不受影响。
- **测试**：边界值单测；大 body 拒绝断言。
- **回滚**：revert。

---

## Phase 3 — P2 架构与工程债（非结构性）

### T-13 解除 utils→services 循环依赖

- **来源**：CQ-01
- **目标**：恢复 `api→services→utils` 单向分层。
- **当前实现**：`utils/log.py`→`services.config`；`utils/helper.py`→
  `services.proxy_service`+`services.config`。
- **建议实现**：
  - `utils.log`：logger 初始化时注入配置快照（或读取最小 env），
    去掉对 `services.config` 的运行时依赖；
  - `utils.helper`：代理/配置读取下沉到调用方——把
    `proxy_settings.build_session_kwargs` 的调用点移到 services
    侧，helper 只保留纯函数；或将 helper 中依赖 services 的函数
    迁入 `services/` 新模块（如 `services/http_utils.py`）。
- **涉及文件**：`utils/log.py`、`utils/helper.py`、调用方（全仓 grep）。
- **工作量**：M。**风险**：中——utils 被全仓引用，改动半径大；
  需全量回归。
- **前置依赖**：建议在 T-06（脱敏收敛）后做，避免同时动两处。
- **验收**：AST 导入图无 `utils→services` 边；`python -c "import
  utils.log"` 不触发 services 导入（或反向验证无环）。
- **测试**：compileall + pytest 全量；日志/代理功能 smoke。
- **回滚**：revert。

### T-14 远程导入诊断逻辑合并

- **来源**：CQ-04、TD-06
- **目标**：消除 sub2api/cpa 诊断上下文复制。
- **当前实现**：`services/sub2api_service.py` `_diagnostic_context`/
  `_server_error_context` 与 `services/cpa_service.py`
  `_import_error_context` 同构复制。
- **建议实现**：提炼到 `services/account_operation_events.py` 或
  `utils/diagnostics.py` 共享辅助，差异化字段为参数；两服务改为
  调用共享实现。
- **涉及文件**：上述 3 文件。
- **工作量**：S。**风险**：低。
- **前置依赖**：无。
- **验收**：导入失败事件字段与合并前一致（快照对比）。
- **测试**：触发一次失败导入比对事件 JSON。
- **回滚**：revert。

### T-15 except Exception 约定

- **来源**：CQ-05、TD-04
- **目标**：确立"有意忽略"显式表达约定。
- **当前实现**：247 处 `except Exception`；22 处 pass-only（已证实
  均为合理清理）。
- **建议实现**：不改存量代码；在 `.codex/rules/backend.md` 或
  review 清单新增约定：清理路径用 `contextlib.suppress(Exception)`
  显式表达；新增 `except Exception` 必须记录或重抛。
- **涉及文件**：`.codex/rules/backend.md`（或新增 review 检查项）。
- **工作量**：S。**风险**：无（文档约定）。
- **前置依赖**：无。
- **验收**：规则文件含约定；后续 PR review 按约定执行。
- **测试**：—（流程项）。
- **回滚**：—。

### T-16 print 清理 + 版本对齐 + 元数据 + install.sh 固定

- **来源**：CQ-06、TD-08/09/10
- **目标**：卫生项批量清理。
- **当前实现与建议**：
  1. `services/oauth_login_service.py:238`、`api/accounts.py:2269`
     的 `print` → `utils.log.logger` + 脱敏（与 T-05-6 合并做）；
     `services/json_file.py` stderr 警告保留（底层库合理）或加注释；
  2. `VERSION`/`pyproject.toml`=3.2.9 vs README badge v3.2.3 →
     以 `VERSION` 为准改 README/README_EN badge（**需决策 D7**）；
  3. `pyproject.toml:8` description 占位 → 补真实描述；
  4. `deploy/install.sh`：文档建议 `BRANCH=<tag>` 固定版本；脚本
     可选加 `--ref <commit>` 校验（低优先，文档化即可）。
- **涉及文件**：上述各文件 + `README.md`/`README_EN.md` +
  `docs/deployment.md`。
- **工作量**：S。**风险**：低。
- **前置依赖**：D7 决策。
- **验收**：grep 无生产路径 print；badge 版本一致；description 非占位。
- **测试**：文档 diff 审查。
- **回滚**：revert。

### T-17 兼容别名确认与弃用

- **来源**：TD-07
- **目标**：确认兼容 shim 是否仍有外部消费者。
- **当前实现**：`api/support.py:111` `start_limited_account_watcher`、
  `_legacy_admin_identity`、`log_service.py:55` `LogService =
  CallRecordService` 等别名。
- **建议实现**：全仓 grep 引用 + 询问是否有外部集成依赖；
  无消费者则删除或标记 deprecated 注释；有消费者则文档记录弃用计划。
- **涉及文件**：`api/support.py`、`services/log_service.py` 及引用方。
- **工作量**：S。**风险**：低（需先确认）。
- **前置依赖**：需用户确认是否有外部集成。
- **验收**：别名引用清单明确；删除/保留有据。
- **测试**：compileall + 功能 smoke。
- **回滚**：revert。

### T-18 `__del__` 资源释放审查

- **来源**：TD-11
- **目标**：确认 `OpenAIBackendAPI.__del__` 的 session close 兜底是否
  足够，或需显式生命周期管理。
- **当前实现**：`services/openai_backend_api.py` `__del__` 调
  `self.close()`——析构做 I/O，CPython 下通常工作但时机不可控。
- **建议实现**：审查所有 `OpenAIBackendAPI` 实例化点是否有显式
  close/上下文管理；若均有，`__del__` 可保留为兜底或改弱引用
  finalizer；若存在泄漏路径，补显式 close。
- **涉及文件**：`services/openai_backend_api.py` 及实例化调用方。
- **工作量**：S。**风险**：低。
- **前置依赖**：无。
- **验收**：实例化点清单确认均有 close 路径；无资源泄漏告警。
- **测试**：长跑图片任务观察 fd/连接数。
- **回滚**：revert。

### T-19 部署加固

- **来源**：SEC-19
- **目标**：compose 健康检查、非 root 容器验证、TLS/权限文档。
- **当前实现**：`docker-compose.yml` 无 app healthcheck/资源限制；
  `Dockerfile` 无 `USER`；`./config.json` 挂载在本地无文件时
  Docker 建目录的坑；无 TLS。
- **建议实现**：
  1. `docker-compose.yml` app 加 `healthcheck`（`/version` 或
     `/auth/status`，interval/retries/timeout）；`deploy.resources`
     注释示例（不强制）；
  2. `config.json` 挂载改 `./config.json:/app/config.json` 保留但
     文档说明需先创建文件（或改用 env-only，`config.json` 可选——
     需确认 config.json 是否必须）；
  3. 非 root 容器（**需决策 D6**）：新增 `USER` + `chown /app`，
     验证 entrypoint `uv sync` 与数据卷写入——风险是 `/app` 运行时
     卷权限；
  4. `docs/deployment.md` 增加"公网必须 HTTPS 反代"硬性说明 +
     Caddy/Nginx 示例片段。
- **涉及文件**：`docker-compose.yml`、`Dockerfile`、
  `deploy/docker-entrypoint.sh`（若非 root）、`docs/deployment.md`。
- **工作量**：M。**风险**：中（非 root 可能破坏 entrypoint/卷写入）。
- **前置依赖**：D6 决策。
- **验收**：`docker compose ps` 显示 healthy；非 root 下功能回归
  （若做 D6）；文档含 TLS 要求。
- **测试**：`docker compose up` 全流程；`docker exec` 确认 user。
- **回滚**：revert compose/Dockerfile。

### T-20 CORS 策略配置化

- **来源**：SEC-18
- **目标**：消除 `allow_origins=["*"]` 或文档化取舍。
- **当前实现**：`api/app.py:116-123` `allow_origins=["*"]`。
- **建议实现**（**需决策 D5**）：
  - A. 保持 `*` + 文档说明（Bearer 认证下风险可控）；
  - B. 配置化 `CHATGPT2API_CORS_ORIGINS`（默认同源/空，允许列表）；
  - C. 关闭跨域（前端同源部署不受影响）。
  推荐 B（灵活且安全）。
- **涉及文件**：`api/app.py`、`services/config.py`（新配置项）、
  `.env.example`、`docs/deployment.md`。
- **工作量**：S-M。**风险**：低-中（跨域 API 消费方需配置）。
- **前置依赖**：D5 决策。
- **验收**：跨域请求行为符合决策；同源控制台不受影响。
- **测试**：curl 带 Origin 头验证。
- **回滚**：revert。

---

## Phase 4 — P2 结构性重构

### T-21 `account_service.py` 包化拆分

- **来源**：CQ-02、TD-01
- **目标**：5304 行单文件按子域拆分，降低维护成本。
- **当前实现**：`services/account_service.py` 单类 `AccountService`
  169 方法，涵盖凭据/刷新/记忆/导入/分组/批量操作全生命周期。
- **建议实现**（技术决策，推荐渐进式包化）：
  - `services/account_service/` 包：`__init__.py`（门面，保持
    `account_service` 单例导出兼容）、`credentials.py`（AT/RT 管理）、
    `refresh.py`（AT 刷新/OAuth refresh）、`memory.py`（记忆维护）、
    `import_.py`（账号导入/OAuth/CPA/Sub2API 协调）、`groups.py`
    （分组）、`batch.py`（批量操作/选择器）、`_shared.py`（内部
    状态/锁/快照）。
  - **关键**：保持 `from services.account_service import
    account_service` 公共 Interface 不变——外部调用零改动；
    拆分是 Implementation 层重组，符合 Module/Interface 理论。
  - 分多个 PR：先抽纯函数/无状态辅助，再抽子域，每步完整回归。
- **涉及文件**：`services/account_service.py` → `services/
  account_service/`；调用方无需改（Interface 不变）。
- **工作量**：XL。**风险**：高——核心路径，169 方法相互调用密集；
  拆分顺序错误会引入循环依赖。
- **前置依赖**：Phase 3 完成；建议先做 T-13（循环依赖）确保
  utils 干净；**需决策 D3** 确认拆分方案。
- **验收**：无单文件 >2000 行；`account_service` 公共方法签名
  不变；pytest 全量 + 账号五条主链路 smoke（导入/刷新/记忆/
  分组/批量）。
- **测试**：现有 tests 全量；每子任务独立回归。
- **回滚**：每子 PR 独立 revert；包化是纯移动，可整包 revert。

### T-22 `api/accounts.py` 路由分文件

- **来源**：CQ-02
- **目标**：2495 行路由文件按资源分文件。
- **当前实现**：模块级辅助 ~1100 行 + `create_router` 1327 行内嵌
  ~50 路由。
- **建议实现**：`api/accounts/` 包：`__init__.py`（`create_router`
  聚合）、`auth_keys.py`、`accounts.py`（CRUD/列表/导出/更新）、
  `groups.py`、`operations.py`（刷新/同步/记忆/进度）、
  `remote_import.py`（OAuth/CPA/Sub2API）；共享辅助进 `_shared.py`。
  保持 `create_router()` 返回同一路由表。
- **涉及文件**：`api/accounts.py` → `api/accounts/`。
- **工作量**：L。**风险**：中。
- **前置依赖**：T-21 或并行（路由层独立于 service 层）。
- **验收**：路由表不变（`app.routes` 对比）；所有 `/api/accounts*`
  端点 smoke。
- **测试**：路由清单快照测试；端点功能回归。
- **回滚**：整包 revert。

### T-23 `conversation.py`/`openai_backend_api.py` 拆分

- **来源**：CQ-02/03、TD-14
- **目标**：图片生成状态机与上游客户端拆分。
- **当前实现**：`conversation.py` 3280 行（`_generate_single_image`
  660 行 8+ nonlocal 状态）；`openai_backend_api.py` 3894 行。
- **建议实现**：`_generate_single_image` 的重试/切号状态机抽为
  显式状态对象（dataclass+方法），闭包只委托；`conversation.py`
  按"请求规范化/上游调用/结果解析/输出格式化"分段；
  `openai_backend_api` 按"会话管理/认证/文本/图片/记忆"分方法组
  或子模块。
- **涉及文件**：上述两文件及调用方。
- **工作量**：XL。**风险**：高——图片链路核心，状态机隐式依赖多。
- **前置依赖**：Phase 4 前段；**需决策 D3** 确认是否做。
- **验收**：图片生成/编辑/多图/重试/切号全链路 smoke；状态机
  行为与拆分前一致（用例覆盖）。
- **测试**：图片任务全类型用例；故障注入（断流/超时/切号）。
- **回滚**：整文件 revert。

### T-24 前端大组件按需拆分

- **来源**：CQ-08、TD-12
- **目标**：Studio/Accounts 大 SFC 在改动触及时就地拆分。
- **建议实现**：不做专项重构；`AppShell.vue`/`StudioMessageItem.vue`/
  `StudioComposer.vue`/`api/accounts.ts` 在下次功能改动时顺带
  抽出子组件/composable。
- **工作量**：持续。**风险**：低。
- **前置依赖**：无。
- **验收**：单文件 <1000 行（软目标）。
- **测试**：`npm run build` + 页面 smoke。
- **回滚**：revert。

---

## Phase 5 — P3 策略与可选

### T-25 上游凭据静态加密

- **来源**：SEC-06
- **目标**：`accounts.data` 中 AT/RT/ID token 应用层加密。
- **当前实现**：`services/storage/database_storage.py:27-41`
  `AccountModel.data` Text 列存完整账号 JSON 明文。
- **建议实现**（**需决策 D1**，推荐先 A 后 B）：
  - **A（推荐先做）**：文档化 `data/` 权限（700/600）、磁盘加密、
    PG 访问控制——零代码，立即降低风险；
  - **B（中长期）**：字段级加密——敏感字段（access_token/
    refresh_token/id_token）AES-256-GCM 加密，密钥来自 env
    `CHATGPT2API_CREDENTIAL_KEY` 或 OS keyring；需 ADR（影响
    备份/迁移/按 token 检索/account_view 投影）；迁移脚本加密存量。
- **涉及文件**：`services/storage/database_storage.py`、
  `services/account_credentials.py`、`services/account_service.py`、
  `services/backup_service.py`（备份兼容）、ADR 新文件。
- **工作量**：XL（B 方案）。**风险**：高（数据迁移+备份兼容+密钥管理）。
- **前置依赖**：**决策 D1** + ADR 批准。
- **验收**：db 文件中 token 字段不可读；功能无回归；备份可恢复。
- **测试**：迁移脚本测试；加解密单测；备份恢复演练。
- **回滚**：保留明文备份列或双写过渡期；密钥轮换方案。

### T-26 测试策略重审

- **来源**：TD-03
- **目标**：评估安全关键路径契约测试是否入库。
- **当前实现**：`tests/`、`web-vue/tests/` gitignore（`backend.md:37`/
  `frontend.md:51` 明确"测试源码不纳入提交"）；CI 只 compileall+build。
- **建议实现**（**需决策 D2**）：
  - **A**：维持策略，CI 加 `ruff check` + `python -c "import main"`
    冒烟 + 关键契约断言（不依赖私有 tests）；
  - **B**：安全关键路径契约测试入库（无真实凭据：SSRF 拦截、
    路径遍历、脱敏、CAS 并发）——需修订 backend.md/frontend.md
    策略条款。
- **涉及文件**：`.github/workflows/docker-publish.yml`、
  `.codex/rules/backend.md`/`frontend.md`（若 B）、新增测试文件。
- **工作量**：M。**风险**：低-中（B 方案需策略修订共识）。
- **前置依赖**：**决策 D2**。
- **验收**：CI 出现测试步骤；安全关键路径有自动化断言。
- **测试**：CI 本身即测试。
- **回滚**：revert workflow/规则。

### T-27 引入 ruff lint

- **来源**：CQ-07
- **目标**：Python 侧静态检查基线。
- **建议实现**：`uv add --dev ruff`；`pyproject.toml` 配
  `[tool.ruff]`（line-length、select 规则保守起步）；CI verify
  job 加 `ruff check`。
- **涉及文件**：`pyproject.toml`、`uv.lock`、CI workflow。
- **工作量**：S。**风险**：低（初始只开少量规则，避免噪音）。
- **验收**：`ruff check` 通过或仅有可接受的既有告警基线。
- **测试**：CI 运行。
- **回滚**：revert。

### T-28 PERF-05/06/08 小优化

- **来源**：PERF-05/06/08
- **目标**：边界场景优化。
- **建议实现**：
  - PERF-05：`openai_backend_api.py` `time.sleep` 轮询改
    `threading.Event.wait(timeout)`（可被取消唤醒）；
  - PERF-06：`image_inputs.py` 回退分支先查 Content-Length/分块读；
  - PERF-08：`_account_zip_bytes` 评估流式（账号数有限，可暂不做）。
- **工作量**：S。**风险**：低。
- **验收**：轮询可被取消；回退路径有上限。
- **测试**：对应单测。
- **回滚**：revert。

### T-29 文档化收尾项

- **来源**：SEC-11/20/21/22、TD-14
- **目标**：把设计取舍与运维假设写进文档。
- **建议实现**：
  - `docs/deployment.md`：admin key=全部上游凭据的信任级别说明；
    auth 快照 30s 窗口与单实例假设；公开端点不可猜 URL 设计；
    key 轮换流程；多实例不支持声明（若 D4=A）；
  - `CONTEXT.md` 或 docs：protocol/backend_api 边界澄清（TD-14）。
- **工作量**：S。**风险**：无。
- **验收**：文档含上述条目。
- **测试**：文档评审。
- **回滚**：—。

---

## 任务依赖图

```
T-01 ────────────────────────────────────────────── (独立, 最先做)
T-02/T-03/T-04 (依赖升级, 可并行, T-03 单独验证)
T-05 (琐碎安全包, 独立)
        │
        ▼
T-06 (脱敏) → T-13 (循环依赖) → T-14/T-15/T-16/T-17/T-18 (债务清理)
T-07/T-08/T-09/T-10/T-11/T-12 (Phase 2, 相互独立可并行)
T-19/T-20 (需 D5/D6 决策)
        │
        ▼
T-21/T-22 (Phase 4, 需 D3, T-21 先行)
T-23 (可选, XL)
        │
        ▼
T-25 (需 D1+ADR) / T-26 (需 D2) / T-27/T-28/T-29 (P3)
```
