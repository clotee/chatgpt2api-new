# 05: Technical Debt Audit — chatgpt2api

Status: current（2026-11-13 只读审计；行号基于当日工作区）

技术债 = 已存在、可识别、修复有明确收益但当前未付的成本。
与代码质量报告（CQ）互补：本报告聚焦**结构性/历史性/流程性**债务，
按"偿还成本 vs 不偿还代价"排序。标注 **[已证实]** / **[潜在风险]**。

## 摘要

| 级别 | 数量 |
| --- | --- |
| High | 3 |
| Medium | 5 |
| Low | 5 |
| Info | 3 |

项目文档治理（CONTEXT/ADR/maps/PRD）和规则体系（.codex/rules）
在同类开源项目中罕见地完善，债务主要集中在**代码体量膨胀**、
**双轨实现**和**工程流程缺口**（测试不入库/CI 无测试）。

---

## TD-01 核心模块体量失控 **[已证实] [High] [结构债]**

- 证据（`wc -l` + AST）：
  - `services/account_service.py` — 5304 行 / 169 方法
  - `services/openai_backend_api.py` — 3894 行
  - `services/protocol/conversation.py` — 3280 行（`_generate_single_image`
    单函数 660 行、8+ nonlocal 可变状态）
  - `api/accounts.py` — 2495 行（`create_router` 1327 行 + 模块级
    辅助 ~1100 行）
  - `services/image_storage_service.py` — 1471 行
- 性质：非"浅包装"问题，是**深 Module 过度膨胀**——单一所有者
  承载过多子域。符合 `.codex/rules/architecture.md` 的 Module
  定义但已超出单人可维护粒度。
- 不偿还代价：每次改动波及面大；review/测试粒度粗；新人上手陡；
  合并冲突常态化。
- 建议：按 CQ-02 方案就地拆分（包化 + 按子域分文件），保持
  Interface 不变；优先拆 `account_service`（子域最多）和
  `api/accounts.py`（路由与辅助分离）。
- 偿还成本：高（需完整回归）；收益随模块继续膨胀递增。
- 验证：拆分后公开路由/服务 Interface 签名不变；pytest 全量 +
  主链路 smoke。

## TD-02 日志脱敏双轨实现 **[已证实] [High] [一致性债]**

- 证据：`utils/log.py:_sanitize` 与 `utils/diagnostics.py:
  sanitize_diagnostic_text`/`scrub_diagnostic_value` 两套脱敏并存；
  logger 侧只认 `token`/`dx` 键名，`password`/`secret`/`api_key`/
  `cookie`/`authorization` 不 mask（SEC-07）。
- 性质：违反仓库自己的"唯一所有者"规则——同一安全语义两个实现，
  覆盖面不一致。
- 不偿还代价：每新增日志点都要人工判断走哪套；漏用 diagnostics
  的路径即泄露面（SEC-07 已证实缺口）。
- 建议：`Logger._sanitize` 委托 diagnostics 单一实现（CQ/SEC-07
  已述）。
- 偿还成本：低-中。

## TD-03 测试体系不入库、CI 无测试 **[已证实] [High] [流程债]**

- 证据：`.gitignore` 排除 `tests/` 与 `web-vue/tests/`；本地两目录
  均不存在；`.github/workflows/docker-publish.yml` verify job 只跑
  `compileall` + `npm run build`。
- **重要上下文**：`.codex/rules/backend.md:37` 与 `frontend.md:51`
  明确规定"测试源码不纳入提交"——这是**有意策略**（测试含真实
  账号链路/敏感 fixture，不入公开仓库）。因此这不是"忘记提交"
  的缺陷，而是**已接受的流程债**：
  - 代价：CI 无回归网；安全敏感改动（SEC-01 OAuth、SSRF、脱敏）
    无自动化防护；测试知识只在维护者本地；贡献者无法验证。
  - 若项目未来接受外部贡献或多维护者，该策略需重审。
- 建议（不改变策略前提下）：
  1. CI 增加**不依赖私有测试**的最小回归：`compileall` 已有，可加
     `ruff check` + 关键 Import 冒烟（`python -c "import main"`）；
  2. 将安全关键路径的**契约测试**（不含真实凭据）考虑入库——
     需先与规则维护者确认是否破坏"测试不入库"策略；
  3. `docs/` 记录本地测试运行手册（已有 testing-and-review.md
     部分覆盖）。
- 偿还成本：取决于策略调整幅度。

## TD-04 `except Exception` 无约定 **[已证实] [Medium] [一致性债]**

- 247 处 `except Exception`；142 记日志/重抛、83 裸、22 仅 pass。
  抽样 22 处 pass-only 均为合理清理路径（CQ-05）——**无缺陷，
  但无显式约定区分"有意忽略"与"漏处理"**。
- 建议：确立 `contextlib.suppress` 用于有意忽略的约定；新增
  `except Exception` 必须记录或重抛（写入 backend.md 或 review 清单）。
- 偿还成本：低（约定+增量遵守）。

## TD-05 双源配置面（config.json 文件 + DB settings）**[已证实] [Medium] [结构债]**

- 证据：`services/config.py` 混合读取 `config.json` 文件
  （`auth_key` 每请求读盘）与 DB `SystemSettingsRepository`
  （其余 settings 走 `reload_if_changed` 1s 轮询）；
  `_promote_legacy_basic_settings` 处理文件→DB 的遗留迁移。
- 性质：同一"配置"概念两个权威源+一层兼容迁移逻辑；`auth-key`
  在文件、其余在 DB——心智模型割裂。
- 不偿还代价：新人不知改哪里生效；优先级规则靠代码隐式维持；
  每请求文件 I/O（PERF-04）。
- 建议：文档化"auth-key 仅文件/env，业务设置仅 DB"的边界；
  `auth_key` 加缓存；长期评估 auth-key 是否也应入 DB settings
  （需 ADR，涉及启动顺序——auth 是启动前提，DB 可能未就绪，
  这是文件源存在的合理理由，倾向**维持现状+文档化**）。
- 偿还成本：低（文档+缓存）。

## TD-06 远程导入诊断逻辑复制 **[已证实] [Medium] [重复债]**

- `sub2api_service._diagnostic_context`/`_server_error_context` 与
  `cpa_service._import_error_context` 同构复制（CQ-04）。
- 第三个远程导入源出现时会继续复制；脱敏规则修复需多处同步。
- 建议：提炼共享辅助到 diagnostics/operation_events。
- 偿还成本：低。

## TD-07 兼容别名/shim 累积 **[已证实] [Low] [演进债]**

- `api/support.py:111` `start_limited_account_watcher` 别名；
  `_legacy_admin_identity` 旧 admin key 通道；
  `log_service.py:55` `LogService = CallRecordService`；
  `api/support.py` 注释明确 "Compatibility alias for integrations"。
- 性质：为外部集成保留的第二入口，每个都是永久维护面。
- 建议：确认无外部消费者后按弃用流程删除；或在 CONTEXT/docs
  记录弃用计划。
- 偿还成本：低（需先确认消费者）。

## TD-08 `print()` 与 logger 双轨日志 **[已证实] [Low] [一致性债]**

- 14 处 `print(`：OAuth 失败直出 stdout（含未经脱敏的上游响应
  片段，SEC 报告已列）、`api/accounts.py:2269` 元数据、
  `json_file.py` stderr 警告、运维脚本 print。
- `json_file` stderr 警告可视为合理（底层库不应依赖应用 logger），
  但生产路径的 print 应统一走 `utils.log.logger` 并过脱敏。
- 偿还成本：低。

## TD-09 版本元数据漂移 + 元数据占位 **[已证实] [Low] [工程卫生]**

- `VERSION`/`pyproject.toml` = 3.2.9；`README.md` badge 与
  `README_EN.md` 引用 v3.2.3；`pyproject.toml:8` description 仍是
  "Add your description here"。
- 建议：对齐 badge/文档版本；补 description。
- 偿还成本：极低。

## TD-10 install.sh 供应链信任锚 **[已证实] [Low] [安全债]**

- `deploy/install.sh`：`curl|bash` 入口；内部 `curl -LsSf
  https://astral.sh/uv/install.sh | sh` 装 uv；`raw_url()` 按
  **分支名**（默认 main，可变引用）从 GitHub 拉 compose 文件；
  `REPO_OWNER`/`REPO_NAME` 可覆盖指向任意仓库。
- 性质：安装器本身是供应链信任锚——镜像/脚本均按分支浮动，
  tag/commit 不固定。
- 建议：文档建议生产环境用 `BRANCH=<tag>` 固定版本；
  install.sh 可增加 `--ref` 固定 commit 校验（或文档说明风险）。
- 偿还成本：低（文档为主）。

## TD-11 `__del__` 析构做 I/O **[已证实] [Low] [代码质量债]**

- `services/openai_backend_api.py` `__del__` 调 `self.close()`
  关闭 session——析构函数做资源释放在 CPython 下"通常工作"，
  但解释器关闭/GC 时机不可控，循环引用下可能延迟或不执行。
- 建议：确认所有 `OpenAIBackendAPI` 实例有显式 close/上下文管理
  路径；`__del__` 仅作兜底可接受，或改弱引用 finalizer。
- 偿还成本：低。

## TD-12 前端大组件与适配器体量 **[已证实] [Low] [结构债]**

- `AppShell.vue` 1723、`StudioMessageItem.vue` 1513、
  `StudioComposer.vue` 1372、`Studio.vue` 1171、`Accounts.vue` 1134、
  `api/accounts.ts` 905 行。
- 已按 `views/<page>/` 拆 runtime，结构合理；大 SFC 是 Vue 项目
  常态，属"可接受的膨胀"——建议仅在改动触及时就地拆，不做专项。
- 偿还成本：中（专项重构不值）。

## TD-13 未关闭 PRD 与进行中需求 **[已证实] [Info] [流程状态]**

- `docs/requirements/`:0001（implementing，GIF/SVG 输出）、
  0002（pending-verification，账号记忆控制）——正常在途工作，
  非债务；仅提示验收闭环待完成。

## TD-14 `services/protocol/` 与 `services/` 平铺混合 **[潜在风险] [Info] [结构观察]**

- `services/protocol/` 是上游协议子包，但 `openai_backend_api.py`
  （上游客户端本体）在 `services/` 根而非 protocol 包内；
  `account_service.py` 等也直接持有上游调用逻辑。
- 观察：协议边界（`services/protocol/`）与上游传输（`openai_backend_api`）
  分离尚可，但"谁拥有上游 HTTP 会话"的边界略模糊——
  `account_service` 也直接建 `requests.Session`（`:1324` AT 刷新）。
- 建议：无需立即行动；若未来上游协议再扩展，考虑把
  `openai_backend_api` 归入 protocol 或明确"backend_api=传输、
  protocol=编排"的边界文档。
- 偿还成本：低（文档澄清）。

---

## 债务偿还路线图建议

| 波次 | 项 | 理由 |
| --- | --- | --- |
| 立即（低成本高收益） | TD-02 脱敏收敛、TD-09 版本/元数据、TD-08 print 清理、TD-05 auth_key 缓存 | 小改动消除真实风险/不一致 |
| 近期 | TD-04 except 约定、TD-06 导入诊断合并、TD-07 别名确认、SEC 报告依赖升级（starlette/pillow/python-multipart/anyio/markdown-it/axios） | 一致性+供应链 |
| 中期 | TD-01 模块拆分（account_service 优先）、PERF-01/03 图库与账号加载优化 | 结构性，需排期+完整回归 |
| 持续 | TD-03 测试策略重审（若引入外部贡献）、TD-10 安装器固定引用、TD-12 按需拆分 | 流程与演进 |

## 与其他报告的交叉引用

- TD-01 ↔ CQ-02/CQ-03（超大文件/函数）
- TD-02 ↔ SEC-07（脱敏双轨）
- TD-03 ↔ 全部 SEC 修复的验证缺口
- TD-05 ↔ PERF-02/04（配置读取）
- TD-06 ↔ CQ-04（诊断重复）
- TD-08 ↔ SEC 报告 OAuth print 项
- TD-10 ↔ SEC-19（部署面）
- 依赖升级项见 `03-security-audit.md` SEC-02/03/04/05/10
