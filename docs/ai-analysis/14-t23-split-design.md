# T-23 执行设计 — protocol 层包化拆分

来源：结构分析报告（2026-11）。分两阶段：**T-23a 纯搬迁**（机械，
本次执行）与 **T-23b 状态机重构**（`_generate_single_image` →
`SingleImageRunner`，单独评估，本次不做）。

## 阶段 A：纯搬迁（Mixin/函数搬迁，零行为变化）

### conversation.py → `services/protocol/conversation/` 包

`from services.protocol.conversation import X` 公共面（facade 必须
re-export 全部，含 `ImageGenerationError`——它经 :31 import 绑定进
命名空间，是 de-facto re-export）：

| 模块 | ~行 | 内容 |
| --- | --- | --- |
| `__init__.py` | ~80 | 显式 re-export 全列表 |
| `_core.py` | ~950 | 常量/校验、message+token+format helpers、3 dataclass、assistant-text helpers、SSE 状态机、`conversation_events` |
| `_monitor.py` | ~270 | `_monitor_image_stage`、egress/timing/progress helpers |
| `_text.py` | ~175 | `text_backend`、`stream_text_deltas`、`collect_text`、SVG trio |
| `_image_stream.py` | ~860 | failure/recovery、`stream_image_outputs`、codex stream、`_image_result_output_from_urls`、cleanup |
| `_runner.py` | ~800 | `_generate_single_image`（**verbatim 搬迁，nonlocal 仍合法**）、`_select_image_pool_error`、`stream_image_outputs_with_pool` |
| `_output.py` | ~110 | `_image_stream_payload`、`stream_image_chunks`、`collect_image_outputs` |

- conversation.py **有** `from __future__ import annotations`（签名惰性），
  但模块内运行时引用仍需真实 import。
- 死代码 `is_token_invalid_error` 保留 re-export（零成本）。

### openai_backend_api.py → `services/openai_backend_api/` 包

| 模块 | ~行 | 内容 |
| --- | --- | --- |
| `__init__.py` | ~40 | facade re-export：`OpenAIBackendAPI`、`SEARCH_MODEL`、`EDITABLE_FILE_MODEL`、`MemoryStateInvalidError`、`InvalidAccessTokenError` |
| `_constants.py` | ~200 | 模块常量/regex/prompt、`ChatRequirements`、`EditableFileArtifact`、`EditableFileExportResult`、timing helpers |
| `_core.py` | ~900 | `OpenAIBackendAPICoreMixin`：`__init__`/lifecycle/headers/timing/SSE/errors/bootstrap/requirements/payload/`stream_conversation`/`list_models`/account-info |
| `_memory.py` | ~115 | memory 4 方法（自包含） |
| `_conversation_mgmt.py` | ~115 | `_get_conversation`/`delete_conversation`/`_list_recent_conversations`/`find_conversation_by_prompt` |
| `_search.py` | ~370 | search 全链 |
| `_editable.py` | ~850 | editable export ~40 helpers（注意 `client_version`/headers 中途突变、`session.curl_options` TIMEOUT_MS 突变+恢复） |
| `_image_results.py` | ~855 | `_poll_image_results`(373行)/`_resolve_image_urls`/`download_image_bytes`/diagnostics |
| `_codex.py` | ~350 | codex responses——**先处理 8 处 `OpenAIBackendAPI.` 字面量**（:987,:992,:1028,:1097,:1169,:1180,:1191,:1193）：`_iter_codex_response_events` 是 `@staticmethod`，把字面量改为模块内类引用或将该方法所在静态方法改 `@classmethod` 用 `cls.`；若风险大可把 codex 留在 `_all.py` |

- **无** `__future__` annotations——所有签名注解**急切求值**，
  每个方法签名用到的名字（`ChatRequirements`、`EditableFileArtifact`、
  `ImageFailure`、`ProxyRuntimeProfile`、`requests.Response`、`Path`…）
  必须在宿主模块真实 import。
- 组合：`_all.py` 中 `class OpenAIBackendAPI(CoreMixin, MemoryMixin,
  ConversationMgmtMixin, SearchMixin, EditableMixin, ImageResultsMixin,
  CodexMixin)`，收尾时类定义移 `__init__.py`，删 `_all.py`。
- `_editable` 与 `_image_results` 的交叉私有调用
  （`_walk_search_dicts`、`_editable_message_text`）经 MRO 解析，
  保持在 core 或各自模块均可——选依赖方向无环的放法。
- 近重复 helper（`_find_editable_value`/`_find_search_value` 等）
  **不去重**，原样搬迁。

## 依赖方向（不可成环）

`conversation/*` 可 import `openai_backend_api`；反向禁止。
新模块只许向"下游"依赖：core ← monitor ← stream ← runner/output；
backend_api: constants ← core ← 各域 mixin。

## 每步验证

```bash
uv run python -m compileall -q services/
CHATGPT2API_AUTH_KEY=test-auth-key uv run pytest tests/ -q
CHATGPT2API_AUTH_KEY=test-auth-key uv run python -c "import main; print(len(main.app.routes))"  # 112
# 额外：逐一 import 所有外部消费者符号（见分析报告清单）
uv run ruff check services/
```

## 风险备注

- contract 测试**不覆盖**图片链路内部——阶段 A 是纯搬迁风险低，
  但建议补一个纯函数单测（`ImageOutput.to_chunk`、
  `stream_image_chunks`、`_select_image_pool_error`）作护栏。
- `_generate_single_image` 的 `nonlocal`/闭包在模块级函数内仍合法，
  verbatim 搬迁不触发行为变化；状态机重构留给 T-23b。
- `OpenAIBackendAPI` 实例的动态属性 `account_email`/
  `progress_callback` 不在 `__init__` 声明——搬迁不受影响。
