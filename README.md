# tsz-agent

独立的 Python 命题服务。DeepSeek 负责组合和语义复查；正确答案和干扰项只能引用 tsz-rust 返回的有效已发布词义。生成结果始终需要人工审核。

## 当前交付边界

本仓实现 API、PostgreSQL 持久化任务、独立 worker、DeepSeek/PydanticAI 适配、库内引用校验、审核事件与案例检索、离线评测入口、迁移、契约和容器配置。

tsz-rust 已实现三个内部词库接口、生成委托与审核反馈投递；tsz 已实现后台题库审核和 C 端单选学习链路。Agent 仍只通过受委托接口读取词库，不直连业务库。已有小规模真实模型与跨端验收记录，但不代表大样本命题质量或当前线上版本已经验收；本轮检索优化的验证范围见 [对接约定](docs/integration.md#检索策略与验证)。

## 运行

需要 Python 3.13、uv，以及独立的 Agent PostgreSQL 数据库。复制 `.env.example` 为 `.env`，配置数据库、两个不同方向的随机服务凭据（至少 32 字符）、tsz-rust 地址及 `DEEPSEEK_API_KEY`。密钥仅用于服务调用，不进入模型上下文。

```sh
uv sync --locked
uv run --env-file .env alembic upgrade head
uv run uvicorn tsz_agent.app:create_app --factory --host 127.0.0.1 --port 8390
```

另一个终端运行 worker：

```sh
uv run python -m tsz_agent.worker
```

启动 API/worker 不自动迁移。worker 未配置模型密钥会直接退出；`/readyz` 只检查 API 数据库及表可用，不代表词库接口或模型已联通。

## 接口

除 `/healthz` 外，业务接口使用 `Authorization: Bearer <TSZ_AGENT_API_TOKEN>`。

| Method / Path | 作用 |
|---|---|
| `POST /v1/runs` | 带 UUID `Idempotency-Key` 创建委托，返回 202 |
| `GET /v1/runs/{id}` | 查询状态、最终结果和已知用量 |
| `GET /v1/runs/{id}/model-metrics` | 查询模型缓存命中率、调用耗时及逐次调用明细 |
| `GET /v1/model-calls` | 分页查询模型调用账本与筛选汇总，供业务后端超管接口使用 |
| `POST /v1/runs/{id}/cancel` | 取消未终结任务；迟到结果无法覆盖取消状态 |
| `POST /v1/review-events` | 接收权威审核、修改、退回及撤销事件 |

创建请求：

```json
{"contract_version":1,"external_request_id":"00000000-0000-0000-0000-000000000001","capability":"vocabulary_mcq_zh_en"}
```

详见 [对接约定](docs/integration.md)、[Agent OpenAPI](docs/openapi.json) 和 [来源契约校验](docs/tsz-input.schema.json)。后者由 Agent 输入模型导出；Rust 接口的权威定义仍是 tsz-rust 自己导出的 OpenAPI。

## 命题与经验

- 每个任务对应一个词义。Rust 先按有效发布、词性和等级范围筛选，以基本词形拼写及中文释义的字面相似度排序，再复核并返回最多 40 个不同词条。相似度仅用于召回，不证明语义相近或答案错误。
- 每个任务最多使用两批候选。程序过滤后不足三个，或模型认为首批不能成题时，可以补查一次；模型补查须至少保留重新生成和复查的两次调用额度，新候选优先、单次模型池最多 40 项。没有新候选、仍不能成题或已用过补查时返回 `insufficient_candidates`，不无限循环或调用模型凑数。
- 生成与语义复查使用不同指令；复查阶段不读取生成者的自评。原释义不改写，题干修改单独作为建议返回。
- 模型只能选择候选引用；服务器组装选项和答案位置。候选 ID 冲突、同词变体、已知答案重复、错误词性/等级、非教学条目会被拒绝。语义唯一性仍需人工审核。
- 生成前及返回结果前核对来源与授权。V1 采用严格发布版本匹配，音频更新导致发布版本变化也会阻止旧结果；语义指纹自动豁免尚未启用。
- PostgreSQL 保存步骤快照，复查失败可复用已生成草稿。租约令牌和终态检查阻止过期 worker 写入。API/worker 可以独立重启。
- 审核事件不可变；相同事件重复投递去重，旧修订不能覆盖新修订。数据库先筛选有效案例的题型、词性、等级和英美档位，再按同词义优先、最近更新时间及稳定 ID 排序，最多返回 12 条供来源复核，最终选取最多 4 条。不会先被全局最近 200 条截断；每次生成前检索一次，撤销或来源过期的案例不再使用。
- 原始事件属于 tsz-rust；Agent 只维护派生案例。不自动推广全局规则，不训练模型参数，不直接批准正式题目。当前命题规则由仓库中的版本化提示词维护。
- DeepSeek 使用供应商原生 `max_tokens` 字段；每一次请求（包括 JSON 修复重试）按剩余输出预算收紧上限。显式关闭思考模式。
- worker 在发送前向 `agent_model_calls` 预留调用次数与输出额度；超时、崩溃或缺少用量的回复不退还未知额度。`usage.requests` 表示预留的调用尝试，可能包含预留后尚未发送就中断的尝试；input/output tokens 仅为已知回执，费用未知保持 `null`。
- 已知用量按调用 ID 独立幂等累计，任务取消、租约移交或结果写入失败不会丢弃回执。未知消费保守占用预算，可能提前停止重试；不保证供应商计费恰好一次。
- 补查后的重新生成与复查共用原任务的调用、输出 token 和超时预算，不提高默认限额；结构修复重试或未知消费仍可能使任务以 `model_budget_exceeded` 终止。
- 模型统计逐次保存到调用账本，包含生成/复查阶段、响应/错误/取消状态及耗时，结构修复重试单独计数。缓存命中率按已知回执的 token 加权计算；供应商缺失或返回不一致的缓存字段时保留 `null`，不当作零命中。新查询接口不改变已有 `RunView.usage` 契约，详见对接约定中的统计口径。

## 验证

```sh
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest -q
uv run python -m tsz_agent.export_openapi --check
uv build
```

数据库测试要求 `TEST_DATABASE_URL` 指向 **本机独立的 `tsz_agent_test` 数据库**。未设置会明确跳过；连接其他名称或非 loopback 地址会拒绝。先在该数据库执行迁移。测试会清空 Agent 自己的测试表，绝不可传业务数据库。

质量评测输入按 `evaluate.Suite` 格式保存：冻结目标与候选，独立标注禁止选项/是否应提出修订。评测样本不作为提示示例。默认只检查样本结构：

```sh
uv run python -m tsz_agent.evaluate --suite evals/example.json
```

配置真实 API Key 并明确决定付费试跑后加 `--live`。开发样例不是人类教研金标，评测输出不能替代人工审核。

## 部署

`Dockerfile` 构建同一镜像，API 默认监听 8390；worker 使用 `python -m tsz_agent.worker`。`deploy/compose.yaml` 将 API 绑定在宿主 loopback，数据库和 tsz-rust 的容器可达地址必须显式配置。容器中的 localhost 指向自身，不是宿主。

镜像构建与迁移分别执行，使用不可变镜像标签。

升级到迁移 `0002` 前停止旧 worker，迁移后再启动新 worker，避免混用旧的预算记账方式。旧 `mcq_zh_en_v1` 步骤快照不会继续调用模型，业务侧需建立新委托。`0002` 的回退在调用账本非空时会拒绝执行，不能为回退删除真实调用回执。

Agent 使用独立数据库和账号，不共享 tsz 业务表权限。Compose 仅是部署模板，本次没有部署服务器或创建远端发布。

模型统计需要显式执行迁移 `0003`。更新时停止 API/worker，执行 `alembic upgrade head` 后启动新版；启动过程不会自动迁移。历史调用的统计保持未知，不推算耗时或缓存量。已有统计数据时 `0003` 会拒绝直接回退，需先保全观测记录。

检索优化没有新增迁移或 wire 字段。工作流版本为 `mcq_zh_en_v3`，检查点额外保存检索批次和排除引用；发布前应让旧 worker 完成已有任务。带 `v1`/`v2` 检查点的未完成任务不会被新版继续执行，返回 `workflow_version_changed`，由业务侧建立新委托。新旧 Rust/Agent 在请求响应结构上兼容，但只有新版 Rust 提供候选相似度排序。
