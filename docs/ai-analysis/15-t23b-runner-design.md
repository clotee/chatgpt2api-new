# T-23b 执行设计 — `_generate_single_image` 状态对象重构

目标文件：`services/protocol/conversation/_runner.py`（T-23a 后
`_generate_single_image` 已 verbatim 落位，约 662 行，L~内偏移）。

**性质**：这是全项目风险最高的单函数重构——12 个跨迭代可变 cell +
4 个闭包 + 每迭代重建的 `finalize_image_slot`。重构必须**行为等价**，
不是"顺手优化"。契约测试不覆盖此路径，验收靠代码走查+图片链路
E2E（用户侧）。

## 现状（分析报告要点）

函数签名：`_generate_single_image(request, index, total)`，内含
`while True` 重试/切号状态机，phase A-H：

- **A 取 token**：`retry_token` 复用 vs
  `account_service.get_available_access_token(...)`；池错误抛
  `retry_error`（若有）否则新 `ImageGenerationError`。
- **B 上下文+finalize 定义**：`finalize_image_slot` 在**每个 while
  迭代内重新定义**，nonlocal `image_slot_finalized`，捕获当轮
  token/凭据/conversation_id/计时等 ~12 个 cell。
- **C 账号解析**：`get_account` → RT 快照+email；
  `pending_switch_attempt_index` 在新 token 解析后回写上一条
  attempt（`switched_account=True` + 日志/监控）。
- **D backend+egress**：fallback profile 判定、`OpenAIBackendAPI`
  构造、`acquire_image_egress`、progress_callback 注入、
  stream_fn 选择（codex vs 普通）。
- **E 流消费**：逐 output 打 account_email、记
  conversation_id、`message_as_error` 提前 raise、置
  emitted/returned 标记。
- **F 完成路径**：message→失败 finalize+尝试切号 continue/return；
  无 result→raise 进 except；成功→cleanup+finalize(True)+return。
- **G 异常路径**：classify→early_connection_failure 判定→
  **fallback-proxy 续试（continue，跳过 finalize，不记 attempt、
  不标 slot）**→归一化 ImageGenerationError→finalize(False,
  quota_consumed 条件)→attach→切号 continue/raise。
- **H finally**：每轮 `release_image_egress`+`backend.close()`。

## 目标结构

```python
@dataclass
class ImageSlotAttempt:      # 每轮上下文（替代 finalize 闭包捕获）
    token: str = ""
    access_token: str = ""
    refresh_token: str = ""
    last_token_refresh_at: Any = None
    started: float = 0.0
    conversation_id: str = ""
    emitted_for_token: bool = False
    returned_message: bool = False
    returned_result: bool = False
    slot_finalized: bool = False
    backend: Any = None       # OpenAIBackendAPI | None
    egress_acquired: bool = False

@dataclass
class SingleImageRunner:
    request: ConversationRequest
    index: int
    total: int
    # 持久状态（跨迭代）
    account_email: str = ""
    retry_token: str = ""
    attempted_tokens: set = field(default_factory=set)
    fallback_retry_pending: bool = False
    fallback_retry_used: bool = False
    fallback_from_egress: dict = field(default_factory=dict)
    image_attempts: list = field(default_factory=list)
    retry_error: Any = None
    pending_switch_attempt_index: Any = None
    account_attempt_started: float = 0.0
    max_account_attempts: int = 0
    single_started: float = 0.0
    attempt: ImageSlotAttempt = field(default_factory=ImageSlotAttempt)
    # 每轮局部状态放 attempt 或 run() 局部变量

    def run(self) -> list[ImageOutput]: ...
    # 建议方法切分（名称可调，职责不变）：
    #   _acquire_token() -> str          # phase A
    #   _resolve_account(token)          # phase C
    #   _open_backend_and_egress(token)  # phase D 前半
    #   _consume_stream(...)             # phase E
    #   _finalize_slot(success, ...)     # phase B/F/G 的 finalize 等价物
    #   _switch_or_raise(failure, error) # retry_with_different_account 等价物
    #   _normalize_image_error(exc)      # phase G 归一化
    #   _attach_attempts(value/outputs)  # 两个 attach 闭包等价物
```

模块级包装：`def _generate_single_image(request, index, total):
return SingleImageRunner(request, index, total).run()`——对外签名
与返回值不变。

## 必须逐条保留的语义（验收清单）

1. `finalize_image_slot` 每轮重建 → 等价物是 `self.attempt` 每轮
   **重置为新 ImageSlotAttempt**（注意原实现是闭包重建捕获当轮
   cell——新attempt必须在每轮开始时重建，不是复用）。
2. `attempt_started = account_attempt_started or account_wait_started`
   ——retry_token 路径保留**首次**取号时间，duration_ms 跨重试累计。
3. fallback-proxy `continue` **完全跳过** finalize/attempt 记录/
   slot 标记；`fallback_retry_used` 每 token draw 至多一次。
4. `retry_error` 在池耗尽的下一次取号时抛出**原始**图片失败。
5. `pending_switch_attempt_index` 在新 token 解析**之后**才消费，
   回写上一条 attempt dict。
6. try 内所有 raise（含 message_as_error、no-result raise）统一进
   except → classify → finalize → 切号判断 → raise/continue。
7. `request.monitor_attempt` 每轮被**原地改写**（被
   `_monitor_image_stage` 读取）。
8. `account_email` 由 `_remember`/解析路径写入，output 逐个回填。
9. finally 每轮释放 egress+close backend（含异常路径）。
10. `image_attempts` 列表跨迭代累积，finalize 时 append 当轮
    attempt dict（含 failure.diagnostic_fields()）。

## 实施约束

- 只动 `services/protocol/conversation/_runner.py`；不改其他文件。
- dataclass 需 `from dataclasses import dataclass, field`（检查现有
  import，_runner.py 已有 dataclass 使用则复用）。
- 所有外部调用（account_service/proxy_settings/OpenAIBackendAPI/
  stream_fn/config/logger/monitor helpers）保持原调用形式。
- 方法内不引入新抽象层；状态字段就是上表。
- 完成后验证：
  ```bash
  uv run python -m compileall -q services/protocol/conversation/
  CHATGPT2API_AUTH_KEY=test-auth-key uv run pytest tests/ -q   # 35
  CHATGPT2API_AUTH_KEY=test-auth-key uv run python -c "import main; print(len(main.app.routes))"  # 112
  uv run ruff check services/protocol/conversation/
  ```
- 自查：对照"必须逐条保留的语义"10 条逐项确认；diff 中
  `_generate_single_image` 包装函数签名不变。
- commit：`refactor: extract SingleImageRunner state object (T-23b)`，
  作者 BLACK·CLOTEE <clotee@foxmail.com> + Devin trailer。
  不 push。
