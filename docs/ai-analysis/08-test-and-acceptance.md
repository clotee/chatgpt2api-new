# 08: Test and Acceptance — chatgpt2api

Status: draft（待用户审核批准）

配套 `06-improvement-roadmap.md` / `07-development-tasks.md`。
本文定义：验证分层、每阶段验收矩阵、端到端验收清单、回归基线、
以及"如何证明改动没有破坏现有功能"。

---

## 一、验证分层（Verification Pyramid）

项目当前测试现状（见 TD-03）：`tests/`、`web-vue/tests/` 被 gitignore
且本地不存在；CI 仅 `compileall` + 前端 `npm run build`。
本计划**不改变"测试源码不入库"的默认策略**（决策 D2 可能修订），
但要求每个任务提供可执行的验证证据，按以下层级组织：

| 层级 | 工具 | 覆盖范围 | 入库? |
| --- | --- | --- | --- |
| L0 静态 | `compileall`、`ruff check`（T-27 后）、`npm run build`、`vue-tsc` | 语法/类型/构建 | CI 已有/新增 |
| L1 单元 | `pytest`（本地 `tests/`）、前端 vitest（本地 `web-vue/tests/`） | 函数级逻辑、状态机、边界 | 本地（现状） |
| L2 集成 | 本地启动 `uvicorn main:app` + `curl`/httpx 脚本 | 路由→service→storage 全链路 | 本地脚本/手工 |
| L3 契约 | OpenAI/Anthropic 兼容端点形状断言 | `/v1/chat/completions`、`/v1/responses`、`/v1/images/*` 响应 schema | 本地（或 D2=B 时入库） |
| L4 端到端 | Docker compose 起栈 + 真实上游账号 | 真实 ChatGPT Web 链路 | 手工/预发 |
| L5 非功能 | 压测脚本、`docker stats`、OSV 复查 | 性能/资源/依赖 | 手工 |

**原则**：P0/P1 安全与稳定性任务必须提供 L1-L3 证据；
Phase 4 重构必须 L1-L4 全覆盖；文档/卫生项 L0 即可。

---

## 二、回归基线（Regression Baseline）

在开始任何 Phase 前，建立"改动前行为快照"，作为回归对照：

### 2.1 后端行为基线

1. **路由表快照**：启动 app，dump `app.routes`（method+path+name）
   为 JSON 基线；任何重构后 diff 必须为空或仅含预期新增。
   ```bash
   uv run python -c "
   from main import app
   import json
   print(json.dumps(sorted(f'{r.methods}:{r.path}' for r in app.routes if hasattr(r,'methods')), indent=0))
   " > /tmp/routes-baseline.json
   ```
2. **OpenAPI 快照**：`GET /openapi.json` 存档（注意 T-05 关闭 docs
   后此端点消失——在关闭前先存档；或 env 开关保留开发模式）。
3. **关键投影样例**：对 `/api/accounts`、`/api/images`、`/api/call-
   records` 各录一次响应样例（脱敏后），作为字段级回归参照。

### 2.2 前端行为基线

1. `npm run build` 产物 hash（仅作构建健康检查，非行为断言）。
2. 主要页面手工走查清单（见 §五）。

### 2.3 数据基线

1. `data/chatgpt2api.db` 结构 dump（`sqlite3 .schema`）——
   确认 Phase 全程无 schema 意外变更（除非任务明确要求迁移）。
2. `data/image_index.json` 样例 + `data/image_tasks.json` 样例。

---

## 三、分阶段验收矩阵

### Phase 1 — P0 安全止血

| 验收项 | 方法 | 通过标准 |
| --- | --- | --- |
| OAuth TLS 校验恢复 | L1 单测断言 kwargs + L4 真实 OAuth 导入 | `verify=True`；导入成功拿到账号 |
| 依赖漏洞清零 | OSV.dev 批量查询 `uv.lock` 全量 | starlette/python-multipart/anyio/pillow 命中数=0（或仅剩未修复 0day） |
| fastapi 兼容 | L2 multipart/JSON/urlencoded 三 content-type 请求 `/v1/images/edits` | 均正确路由，无 500 |
| Pillow 兼容 | L1 图片用例 + L2 各格式上传→处理→下载 | png/jpg/webp/gif/svg 全链路成功；EXIF 旋转正确 |
| 前端依赖 | `npm audit` + L0 build + L2 Studio markdown 渲染 | 无 high/critical；构建过；渲染不断言崩溃 |
| 琐碎安全包 | 逐项 L1/L2 断言（见 T-05 验收列） | 全部通过 |

**Phase 1 完成条件**：上述全绿 + 路由表快照无意外 diff +
无新增 print 到 stdout（`grep -rn "print(" services/ api/ | grep -v test`）。

### Phase 2 — P1 稳定与性能

| 验收项 | 方法 | 通过标准 |
| --- | --- | --- |
| 日志脱敏收敛 | L1 构造 `{password, api_key, cookie, authorization, token}` dict | 全部 mask；既有日志格式不变 |
| WebDAV 加固 | L1 mock server 测大小上限/session close；settings 校验单测 | http 被拒或警告；超限触发；session 关闭 |
| 认证限流 | L1 状态机单测 + L2 curl 连续错误 | 5 次失败后退避/429；正确 key 不受影响；重启清零 |
| 图库索引优先 | L5 压测：5000 文件目录 `GET /api/images` | P99 相比基线显著下降（建议 ≥50%）；新图立即可见；对账窗口内手动拷入可见 |
| 账号列表缓存 | L1 缓存命中/失效单测 + L2 写后读 | TTL 命中；写操作后立即读新值 |
| 配置缓存 | L2 strace/计数 auth 请求读盘次数 | 不再每请求读 config.json；TTL 内生效 |
| 输入边界 | L1 边界值 + L2 超限请求 | messages>上限 422；zip 超限 413/截断；list max_items 422 |

**Phase 2 完成条件**：Medium 安全项清零 + 两处性能瓶颈有
**前后对比数据**（不是"感觉快了"）+ 无功能回归。

### Phase 3 — P2 架构与工程债

| 验收项 | 方法 | 通过标准 |
| --- | --- | --- |
| 循环依赖解除 | AST 脚本遍历 `utils/**/*.py` import | 无 `services` 导入边 |
| 诊断合并 | L2 触发失败导入比对事件 JSON | 字段与基线一致 |
| except 约定 | 规则文件 diff | 约定已写入且可执行 |
| 卫生项 | grep/文档 diff | print 清理、版本对齐、description 补全 |
| 部署加固 | `docker compose up -d` + `docker compose ps` | healthy；非 root（若 D6=A）下功能回归 |
| CORS | curl 带 `Origin` 头 | 符合 D5 决策 |

### Phase 4 — P2 结构性重构

**每个拆分子任务独立验收**，通用标准：

| 验收项 | 方法 | 通过标准 |
| --- | --- | --- |
| Interface 不变 | 路由表快照 diff + `account_service` 公共方法签名 diff | 零意外变化 |
| 行为不变 | L1-L3 全量回归 + 五条主链路 smoke | 全绿 |
| 规模达标 | `wc -l` 统计 | services/api 顶层无单文件 >2000 行 |
| 无新循环依赖 | AST 导入图重算 | 包间无环 |

**主链路 smoke 清单**（每次重构后必跑）：
1. 账号导入（OAuth/AT 直导）→ 出现在列表 → 状态正确
2. AT 刷新（手动触发）→ token 更新 → 调用成功
3. 记忆维护（启用账号）→ 记忆内容生成
4. 分组 CRUD + 账号分配
5. 批量操作（全选/反选/删除）
6. 图片生成（文生图）→ 任务完成 → 图库可见 → 下载
7. 图片编辑（多模态输入）→ 输出正确
8. `/v1/chat/completions` 文本对话流式/非流式
9. `/v1/responses` 兼容端点
10. 控制台登录/登出/设置保存

### Phase 5 — P3 策略项

按 D1/D2 决策结果分别验收（见 T-25/T-26 卡片）。

---

## 四、端到端验收清单（最终交付前）

在所有 Must/Should 任务完成后执行：

- [ ] `git status` 干净（除预期文件）
- [ ] 路由表快照 vs 基线 diff 为空
- [ ] OSV 复查 `uv.lock`：无 high/critical 已知漏洞
- [ ] `npm audit`：无 high/critical
- [ ] `compileall` + `npm run build` + `npm run test:runtime` 通过
- [ ] L4 Docker 全栈启动：`docker compose up -d` → healthy → 控制台可访问 → API key 调用成功
- [ ] 五条主链路 smoke（§三 Phase 4 清单 1-10）
- [ ] 安全专项：错误认证限流生效、OAuth TLS 校验、`/docs` 状态符合预期、超限输入被拒
- [ ] 性能专项：图库/账号列表压测数据 vs 基线
- [ ] 文档：`docs/deployment.md` 含 TLS/权限/key 轮换/单实例假设说明
- [ ] 决策项 D1-D8 全部有记录结论（ADR 或文档）

---

## 五、前端手工走查清单

（L4 阶段，浏览器验证）

- [ ] 登录页：错误密码提示、正确登录跳转
- [ ] 仪表盘：账号数/今日调用/错误率显示正确
- [ ] 账号页：列表/筛选/分组/导入/删除/刷新
- [ ] Studio：文本对话流式渲染、markdown/代码高亮、图片生成/编辑、历史会话
- [ ] 图库：瀑布流加载、筛选、预览、下载（单个/多选/zip）
- [ ] 设置：修改保存生效、敏感字段脱敏显示、重启持久化
- [ ] 日志/调用记录：筛选、详情、导出
- [ ] 公开端点：`/version`、图片直链（无 key）按设计工作

---

## 六、性能验收阈值

（建议值，实施时可按实际基线调整）

| 场景 | 基线（改动前实测） | 目标 |
| --- | --- | --- |
| `GET /api/images`（5000 文件） | 待测 | P99 ↓≥50% |
| `GET /api/accounts`（1000 账号翻页） | 待测 | P99 ↓≥50% |
| `/v1/chat/completions` 首字节 | 待测 | 不劣化（±10%） |
| 图片生成任务 | 待测 | 不劣化 |
| 容器内存（空闲） | 待测 | 不劣化 |

**注意**：Phase 2 性能任务必须先测基线再改代码，否则无法证明改善。

---

## 七、测试证据规范

每个任务 PR/提交需附：

1. **验证命令 + 实际输出**（不是"测试通过"的口头声明）
2. **前后对比**（性能/行为变化时）
3. **未验证项明确标注**（如"真实 OAuth 导入需账号，未测"）
4. **回滚方式**（每个任务卡已有，执行时确认仍有效）

遵循 `.codex/rules/testing-and-review.md`：区分自动化测试、
浏览器检查、用户验收、未验证项。

---

## 八、与仓库规则的衔接

- **测试入库**：已修订（2026-11，用户批准）——`backend.md` 允许
  `tests/` 覆盖公共 Interface 的回归测试入库；`web-vue/tests/`
  仍维持本地不入库，前端测试策略待后续决策。
- **文档治理**：本目录文件为 `ai-analysis` 分析产物，不属于
  `docs/` 的 current/PRD/ADR 治理体系；实施产生的 ADR（如 D1 加密、
  D3 拆分方案）应按 `docs/adr/` 规范单独创建。
- **workflow.md**：每个任务按"分析→计划→同步→实现→验证"执行；
  Phase 4 大重构需先获用户确认拆分方案。
