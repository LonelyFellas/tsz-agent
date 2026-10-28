# tsz-rust ↔ tsz-agent v1

状态：Agent 端已实现，tsz-rust 端待实现；尚未真实联调。所有 JSON 使用 snake_case，严格拒绝未知字段，协议版本为 1。

## 数据归属与身份

tsz-rust 保存委托、用户授权、词库和正式题目/审核记录；Agent 保存执行任务、步骤输入、用量、候选草稿和派生案例。tsz-rust 发起任务后主动拉取结果，再按当前来源与权限检查后写入待审核题库。

两个方向使用不同的可轮换 Bearer 服务凭据。内部接口必须由 tsz-rust 根据 external_request_id 查到操作者、有效期、撤销状态及任务范围，不可仅信任客户端自报 actor_id。Agent 不持有用户登录 token 或业务数据库权限。跨机器通过 TLS/私网访问；模型只看到词条数据和案例，不看到服务凭据。

## tsz-rust 需提供的内部接口

公共前缀 `/api/internal/agent/v1/requests/{external_request_id}`：

| 接口 | 输入/输出模型 |
|---|---|
| `GET /context` | 输出 `GenerationContext` |
| `POST /candidates/search` | `CandidateSearch` → `CandidatePage`；每次最多 40 项 |
| `POST /entries/batch-get` | `EntriesRequest` → `CandidatePage`；按引用独立复核，不截断结果 |

具体类型见 `tsz-input.schema.json` 的 `$defs`，由 `schemas.py` 自动导出。正确答案必须根据目标词义的基本词形绑定和 `BrE/AmE` 设置解析，不能直接使用可能含并列词形的词条展示名。

Candidate 含来源六个 ID、`content_fingerprint`、spelling、part_of_speech、level、definition、other_definitions、accepted_spellings、answer_profile、teaching_eligible。`definition_id` 属于 `sense_id`；`form_id` 必须绑定该词义且为 base。必须来自当前有效发布，禁止混入草稿、外部词典或验收条目。fingerprint 按确定的语义内容规范计算，不能用常量代替。

`candidate_ref` 在同一委托内必须稳定且不能重绑。详情接口也允许复核已确认审核案例所引用的其他已发布词条，仍受本委托读取范围限制；原样回显候选引用。案例源已失效时返回 409/410，Agent 会丢弃该案例。可选案例返回 401/403 时，Agent 重新读取当前委托 context；仅当读取成功且与本次目标一致时跳过该案例。委托撤销、目标变化或复核网络故障仍终止/重试当前任务。

第一版严格比较发布 ID 和完整候选内容。若任何被检查引用过期、缺失或不符合授权，返回 409/410/403，不能悄悄替换成最新内容。内容变化后由业务侧建立新委托。

状态码：401/403 → `authorization_revoked`；404/409/410 → `source_changed`；429/5xx/传输故障 → 有限重试；其他非 200 或错误 JSON → `source_contract_invalid`。跳转不会跟随。

## Agent 接口

`POST /v1/runs` 必须带 UUID `Idempotency-Key`。`external_request_id` 是业务身份，`run_id` 是执行身份。相同原始键及内容重复投递复用任务，键被用于不同内容返回 409；同一业务请求不会产生第二个执行任务。

`GET /v1/runs/{run_id}` 返回 `queued/running/succeeded/failed/cancelled`。`succeeded` 的业务结果为 `draft` 或 `insufficient_candidates`，**不意味着审核通过**。草稿始终包含 `requires_human_review: true`。

模型原始提议仅含三个候选引用及理由；API 返回完整来源选项供后台审核。正确项标记属于服务端/管理员数据，禁止把本接口响应直接返回学生。tsz-rust 后续 C 端 DTO 应单独裁剪。

`POST /v1/runs/{run_id}/cancel` 对运行中/排队任务生效；已经终结时返回原状态，不覆盖成功结果。正式业务委托的取消状态由 tsz-rust 再次拦截，避免取消与同步结果竞态。

API 的错误为 `application/problem+json`，包含 type/title/status/detail/code，调用方按 code 分支，不解析 detail。每个 HTTP 响应提供 `x-request-id`；跨服务使用 external_request_id 关联。

## 审核反馈

`POST /v1/review-events` 只接受可信 tsz-rust 服务调用。事件含 `event_id`、`review_id`、`review_revision`、`source_run_id`、`question_revision_id`、decision、reason_codes、comment，以及已审核 example。

- `approved`/`edited_and_approved`：必须提供最终人工确认的完整案例。目标不得被换成另一词义，三个选项也需满足来源结构约束。
- `rejected`：必须提供原因；未提供 example 时从实际生成题目构造负面案例，保留“退回”标签。
- `revoked`：不可携带有效 example，立即停用该 review_id 的案例。
- 同一事件重复提交幂等；同一事件不同内容冲突。同一 review_id/review_revision 只允许一个事件。高版本撤销先到时，随后低版本通过不会恢复案例。

审核记录 ID 同时绑定原 source_run_id 与 question_revision_id，即使高修订撤销事件首次到达，这两个身份也固定。换用新执行结果/新题目修订时，应创建新的审核记录，不能重绑旧记录。

tsz-rust 在审核事务里记录专用待投递事件，并可靠重试。不要让新的消费者抢占现有 outbox 的处理标记。Agent 反馈派生数据可由权威事件重建；当前仅提供增量接收，历史重放由业务侧重复投递事件完成。

## 版本和恢复

运行中原始上下文、候选池、案例、模型提议、复查结果保存在步骤快照中。新的进程只能在租约有效时写入；模型提议已保存后重试不重新生成，但重新核对词库。提示词版本改变时旧执行失败关闭，使用新业务委托重跑。

Agent 的基础校验不证明语义唯一性。所有正式题目在 tsz-rust 审核入库时仍需重新校验四个选项，并按业务版本记录人工决定。

独立 API 演进应发布新的明确协议版本或完成双边兼容验证，不能默认新字段对旧严格消费者安全。Agent 不改写 tsz-rust 的 OpenAPI；该服务实现内部接口后，以其导出的 OpenAPI 进行最终对账。

## 调用预算与回执

### 模型缓存与耗时统计

admin 账本使用独立服务鉴权的 `GET /v1/model-calls?page=1&page_size=20&run_id=<可选任务UUID>`。`page` 范围 1–1,000,000，`page_size` 范围 1–100；省略 `run_id` 时查询全部调用。返回 `items/total/page/page_size/summary`，汇总覆盖全部筛选结果，不限当前页。只投影调用标识、时间、模型、已知 token、未知费用和观测，不返回提示词、候选池或模型原文。`tsz-rust` 的 `/api/v1/admin/ai-ledger` 用当前超管身份鉴权后访问，浏览器不持有 Agent 服务凭据。跨仓范围与验收见 [超管 AI 账本](features/admin-ai-ledger/README.md)。

`GET /v1/runs/{run_id}/model-metrics` 使用相同的服务鉴权；不存在的任务返回 `404 run_not_found`。这是新增的独立响应契约，原有 `GET /v1/runs/{run_id}` 和 `RunView.usage` 保持兼容。

- `reserved_calls`：该任务调用账本中的预留数量，可能包含预留后尚未发送就中断的调用。
- `observed_calls`：已保存完整调用耗时观测的数量，包含返回响应、失败和取消的调用。进程被强制终止或历史调用可能没有观测。
- `cache_observed_calls`：同时取得有效命中/未命中计数的调用数。缺失任一计数、负值或两者之和与输入 token 总量不一致时，两项均记为 `null`。
- `cache_hit_tokens` / `cache_miss_tokens`：仅汇总有效缓存回执；没有有效回执时为 `null`。采用 DeepSeek 原生 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`，不使用 SDK 缺失数据时的默认零值。[供应商字段说明](https://api-docs.deepseek.com/guides/kv_cache/)
- `cache_hit_rate`：`sum(hit) / (sum(hit) + sum(miss))`，范围 0–1；无有效回执或分母为零时为 `null`。这是按已观测 token 加权的比例，不是各调用命中率的平均值；同时查看 `cache_observed_calls` 可判断数据覆盖情况。
- `total_duration_ms` / `average_duration_ms`：已观测调用的总耗时/平均耗时；没有观测时为 `null`。使用单调时钟，从模型请求发出前计到 SDK 返回或抛错，包含网络与 SDK 响应处理，不包含额度预留、落库、任务排队和词库请求；不是首 token 延迟或任务端到端耗时。
- `calls`：按预留时间排列的明细，包含 `call_id`、`created_at`、`metrics`。未观测调用的 `metrics` 为 `null`；已观测明细包含 `phase=generate/review`、`outcome=response/error/cancelled`、`duration_ms` 以及缓存计数。`response` 仅表示收到模型响应，不保证结构或语义检查通过；结构修复会产生独立的新调用明细。

统计按调用 ID 幂等写入；重复相同内容不累加，不同内容拒绝覆盖。取消和租约移交不阻止迟到观测写入。统计独立于用量结算：超时、取消或缺失 token 回执时即使已记录耗时，未知输出预算仍保持预留，费用仍为 `null`。历史记录不回填估计值。

数据库迁移为 `0003`，增加可空的逐调用 `metrics`。必须显式迁移后启动新版 API/worker；已有观测时回退会拒绝删除统计数据。

若需查看全服务的总体指标，可在 Agent 自有数据库对同一时间窗口求和，而不要平均每个任务的命中率。例如：

```sql
SELECT count(*) AS reserved_calls,
       count(metrics) AS observed_calls,
       count(metrics->>'cache_hit_tokens') AS cache_observed_calls,
       sum((metrics->>'cache_hit_tokens')::numeric)
         / nullif(sum((metrics->>'cache_hit_tokens')::numeric
                    + (metrics->>'cache_miss_tokens')::numeric), 0) AS cache_hit_rate,
       avg((metrics->>'duration_ms')::double precision) AS average_duration_ms
FROM agent_model_calls
WHERE created_at >= now() - interval '24 hours';
```

### 预算结算

迁移 `0002` 引入独立调用账本。API 的 `usage.requests` 是发送前预留的尝试数，不依赖 SDK 成功响应计数。每次发送（包括 SDK 的结构修复）按已知输出和未结算预留共同计算剩余额度；未知用量继续占用预留额度。

结果状态与记账独立：取消和租约失效阻止新调用及题目写入，但已经预留调用的供应商回执仍按调用 ID 累计。重复相同回执幂等，不同内容冲突。未知 token/金额不会被声明为已确认的零消费。

API 的非预期异常在 HTTP 中间件返回脱敏 Problem Details，包含 `x-request-id`；原始异常不交给 Uvicorn 输出 traceback。此边界覆盖当前普通 JSON 接口，后续添加流式响应/后台响应任务时需另行验证响应开始后的错误处理。
