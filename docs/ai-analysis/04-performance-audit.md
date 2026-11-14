# 04: Performance Audit — chatgpt2api

Status: current（2026-11-13 只读审计；行号基于当日工作区）

方法：调用链追踪 + 复杂度分析 + 资源生命周期审查。标注
**[已证实]** / **[潜在风险]** / **[优化建议]**。

## 摘要

| 级别 | 数量 |
| --- | --- |
| High | 0 |
| Medium | 4 |
| Low | 4 |
| Info | 5 |

无 High 级性能缺陷。系统在并发治理（有界线程池/任务槽位/图片抓取
信号量）、缓存边界（chat cache max_entries）、DB 索引（call_records
8 个复合索引）、前端轮询（可见性感知+终态清理）上做得规范。
主要问题是**图库全量文件系统扫描**和**配置/账号全量加载**两处
O(N) 路径。

---

## PERF-01 图库列表每次请求全量扫描文件系统 **[已证实] [Medium] [I/O]**

- 位置：`services/image_storage_service.py:735-776` `list_items`；
  入口 `api/system.py:265-287` `GET /api/images`。
- 证据：`list_items` 默认 `refresh_index=True` → `root.rglob("*")`
  **递归遍历整个图片目录**；对索引外新文件逐个 `read_bytes()` +
  `_image_dimensions()` 解析尺寸；同时 `_load_clean_index()` 每次
  全量读+解析 `data/image_index.json`。图库页每翻页/筛选/刷新都触发。
- 复杂度：O（磁盘文件数 + 索引条目数） per request；图库增长到数千
  张后单页加载变慢，且持 `_index_lock` 期间阻塞其他图库操作。
- 原因：索引与文件系统的一致性靠"每次读时重建"保证，没有失效/增量
  同步机制。
- 影响：图库越大越慢；锁内做磁盘 I/O 放大尾延迟；多管理员并发浏览
  时相互阻塞。
- 建议（按侵入度排序）：
  1. `refresh_index` 改为按需/定时（如 mtime 水位线或后台同步），
     列表请求只读索引；
  2. `_image_dimensions` 结果随索引持久化（已部分如此），新文件
     尺寸解析可异步补齐；
  3. 索引 JSON 大时改 SQLite/JSONL 分页读取。
- 修改风险：中——涉及图库一致性语义（索引与文件可能漂移），
  需明确"索引滞后可接受窗口"。
- 验证：构造 5000 文件目录压测 `GET /api/images` P99；
  新增图片后索引刷新时延断言。

## PERF-02 配置读取每秒 DB 轮询 + auth_key 每次请求读文件 **[已证实] [Medium] [I/O]**

- 位置：`services/config.py:474-483` `reload_if_changed`；
  `:485-490` `auth_key` property；调用方 `api/support.py:24`。
- 证据：
  - `_DATABASE_SETTINGS_REFRESH_INTERVAL_SECONDS = 1.0`（`:42`）——
    任何 `reload_if_changed()` 触发的属性访问若距上次 >1s 就执行
    `_load()`，而 `_load()`（`:466-472`）**同时读 settings 仓储 +
    account_groups 仓储** = 每秒最多 2 次 DB 查询（空闲时也持续）。
    `config` 有数十个属性各自调 `reload_if_changed()`，单请求内多次
    访问共享同一 1s 节流，实际压力可控但非零。
  - `auth_key` property（`:487`）**每次调用 `read_json_object` 从磁盘
    读 config.json**——每个认证请求（`require_identity` →
    `_legacy_admin_identity`）都产生一次文件 I/O + JSON 解析。
- 影响：空闲实例每秒 ~2 次无谓 DB 查询；每请求一次文件读。
  单机影响小，但属"热路径上的同步 I/O"反模式。
- 建议：
  - `auth_key` 加短 TTL 缓存（如 1-5s）或仅在 `reload_if_changed`
    时重载；
  - settings 刷新改事件驱动（写路径失效缓存）或拉长间隔，
    保留兜底周期。
- 修改风险：低-中（配置生效延迟语义变化，需文档注明）。
- 验证：压测下 DB 查询计数；修改 config.json 后 auth 生效时延断言。

## PERF-03 账号列表全量加载+内存过滤分页 **[已证实] [Medium] [算法]**

- 位置：`api/accounts.py:1099-1127` `_accounts_page`；
  `services/account_service.py` `list_accounts`；
  存储层 `services/storage/database_storage.py:27-32`。
- 证据：`GET /api/accounts` → `account_service.list_accounts()`
  **加载全部账号行**（`accounts.data` 为整行 JSON blob，逐行
  `json.loads` 反序列化）→ `_filtered_accounts` 内存过滤 →
  Python 切片分页 → 每项 `_account_for_api` 投影。
  `page_size` 上限 500 但**过滤前已物化全量**。
- 复杂度：O（账号数 × 单账号 JSON 大小 + 投影成本） per request；
  账号池数百-数千时每次翻页/筛选都全量重算。
- 原因：存储模型是"整行 JSON"，无 SQL 级字段索引；过滤/分页在
  应用层（ADR 0004 的设计取舍——账号数据异构，JSON blob 换查询
  灵活性）。
- 影响：账号越多控制台越卡；批量操作后的列表刷新尤甚。
- 建议（不破坏 ADR）：
  1. `list_accounts` 结果加短 TTL 快照缓存（写路径失效），
     翻页/筛选复用；
  2. 过滤维度（status/group/keyword）下沉到投影前的轻量元数据
     索引（如内存中维护 `token→{status,group}` 映射）；
  3. 中长期评估账号核心字段（email/status/group）结构化列。
- 修改风险：中（涉及快照一致性与写后读语义）。
- 验证：1000 账号压测 `GET /api/accounts`；写后立即读一致性断言。

## PERF-04 `config.auth_key` 每次认证读文件 **[已证实] [Low] [I/O]**

- 同 PERF-02 第二点，单列是因为它在**每个 API 请求**的认证路径上
  （不只管理端）：`require_identity` → `_legacy_admin_identity` →
  `config.auth_key` → `read_json_object(config.json)`。
- 影响：高 QPS 下每请求一次 open+read+json.loads；文件在 Docker
  bind mount 上，跨文件系统可能更慢。
- 建议：与 PERF-02 合并修复（TTL 缓存）。
- 修改风险：低。验证：strace/计数文件打开次数。

## PERF-05 `time.sleep` 轮询占用工作线程 **[已证实] [Low] [并发]**

- 位置：`services/openai_backend_api.py:435,2001,2019,2624`。
- 证据：图片结果轮询/流恢复用 `time.sleep(min(poll_interval, remaining))`
  ——**阻塞式睡眠占用线程池线程**。线程池容量 = `CHATGPT2API_THREAD_TOKENS`
  （默认 120）。
- 影响：大量并发图片任务时，等待中的轮询线程占住池位，可能挤压
  文本请求；但图片任务本身有 `image_request_timeout_secs` 生命周期
  上限兜底，且等待本就是该线程的工作。
- 建议：可接受现状；若观测到线程池饥饿，把轮询改 `threading.Event.wait`
  （可被取消唤醒）或异步化。
- 修改风险：中。验证：并发图片任务下 `/v1/chat/completions` 延迟分布。

## PERF-06 `_read_response_limited` 回退路径先读全量再校验 **[已证实] [Low] [内存]**

- 位置：`api/image_inputs.py:349-356`。
- 证据：`iter_content` 不可用的轻量 response double 回退到
  `bytes(response.content)` **全量读入后才查 50MB 上限**；真实
  curl-cffi Response 支持 iter_content，回退仅防测试 double，
  实际风险低。
- 建议：回退分支同样先查 `Content-Length`/分块读，或注释说明
  仅测试路径。
- 修改风险：低。验证：单测覆盖。

## PERF-07 `download_images_zip` 全量内存打包 **[已证实] [Low] [内存]**

- 位置：`services/image_service.py:314-346`（同 SEC-14）。
- 全量 `read_bytes` → `BytesIO` ZIP，无聚合上限；大批量图库下载
  内存放大。
- 建议：文件数/字节上限或临时文件流式。
- 修改风险：低。

## PERF-08 账号导出 ZIP 全量内存 **[已证实] [Low] [内存]**

- 位置：`api/accounts.py:1130-1147` `_account_zip_bytes`。
- 全部导出项 `json.dumps` 进 `BytesIO` ZIP；admin-only，账号数
  通常有限，风险低。
- 建议：可选流式；现状可接受。

## PERF-09 Dashboard 指标投影设计良好 **[已证实] [Info] [正面]**

- `services/dashboard_metrics_service.py:342+` `sync_from_log_service`
  用**游标增量摄取**（`open_call_window(cursor)`）+ checkpoint +
  revision CAS，而非全量扫描；`call_records` 表 8 个复合索引覆盖
  常用过滤（`call_record_repository.py:40-48`）；小时投影三表分离。
- 启动时 `reset_projection_schema_if_needed` + 首次全量同步一次，
  之后增量——设计正确。

## PERF-10 并发治理规范 **[已证实] [Info] [正面]**

- `CHATGPT2API_THREAD_TOKENS` 统一控制 anyio thread limiter
  （`api/app.py:34-45`）并上报 monitor；
- `bounded_task_runner.py` reservation/commit/rollback 有界运行器；
- `account_processing` 共享账号处理槽位（CONTEXT.md 定义）；
- 图片 URL 抓取 `_IMAGE_FETCH_SLOTS` 信号量（并发 8）+
  `acquire(timeout=60)` 防死锁；
- `image_task_service`/GenBox 有界 worker+队列+shutdown 协议；
- OAuth 会话 64 上限 + TTL 清理；chat_completion_cache
  `max_entries`（默认 256）+ TTL（60s）双界。

## PERF-11 前端轮询与渲染治理规范 **[已证实] [Info] [正面]**

- `web-vue/src/composables/usePageRuntime.ts`：interval/timer 注册表
  + `isActive`/`isVisible` 双门控（页面隐藏不轮询）+ deactivate
  清理 scope；
- `vue-virtual-scroller` 用于长列表；axios 统一 600s 超时 +
  401 拦截；Studio 会话态 localStorage（不持久化敏感内容）。

## PERF-12 数据库查询模式总体健康 **[已证实] [Info] [正面]**

- Repository 层 SQLAlchemy 参数化 + 显式索引；`call_records`/`dashboard`
  投影表索引完备；账号/设置走 CAS revision 乐观锁避免长事务；
- SQLite `BEGIN IMMEDIATE` 串行写、PG advisory lock——并发写
  语义正确。
- 已知例外：accounts 全量 JSON blob（PERF-03）、settings 每秒
  轮询（PERF-02）。

---

## 性能风险排序（供修复参考）

| 优先级 | 项 | 触发条件 |
| --- | --- | --- |
| P1 | PERF-01 图库全量扫描 | 图库 > 数千文件或高频浏览 |
| P2 | PERF-03 账号全量加载 | 账号池 > 数百 |
| P3 | PERF-02/04 配置每秒 DB+每请求读文件 | 高 QPS 或多实例 |
| P4 | PERF-05/06/07/08 | 边界场景，现状可接受 |
