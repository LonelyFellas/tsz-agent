# tsz-agent

独立的 Python 命题服务。DeepSeek 负责组合和语义复查；正确答案和干扰项只能引用 tsz-rust 返回的有效已发布词义。生成结果始终需要人工审核。

## 当前交付边界

本仓实现 API、PostgreSQL 持久化任务、独立 worker、DeepSeek/PydanticAI 适配、库内引用校验、审核事件与案例检索、离线评测入口、迁移、契约和容器配置。

**tsz-rust 的三个内部接口尚未实现；本仓不会直连业务库或绕过鉴权。** 当前验证使用独立 PostgreSQL 和受控词库/模型响应，不能称为真实业务联调。正式题库、后台审核界面、C 端单选题属于 tsz/tsz-rust 后续工作。

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
| `POST /v1/runs/{id}/cancel` | 取消未终结任务；迟到结果无法覆盖取消状态 |
| `POST /v1/review-events` | 接收权威审核、修改、退回及撤销事件 |

创建请求：

```json
{"contract_version":1,"external_request_id":"00000000-0000-0000-0000-000000000001","capability":"vocabulary_mcq_zh_en"}
```

详见 [对接约定](docs/integration.md)、[Agent OpenAPI](docs/openapi.json) 和 [来源契约草案](docs/tsz-input.schema.json)。后者是待 tsz-rust 实现确认的协议，不是其已发布 OpenAPI。

## 命题与经验

- 每个任务对应一个词义。读取目标、按词性/等级/拼写过滤候选；不足时最多补查一次，仍不足则正常返回 `insufficient_candidates`，不调用模型凑数。
- 生成与语义复查使用不同指令；复查阶段不读取生成者的自评。原释义不改写，题干修改单独作为建议返回。
- 模型只能选择候选引用；服务器组装选项和答案位置。候选 ID 冲突、同词变体、已知答案重复、错误词性/等级、非教学条目会被拒绝。语义唯一性仍需人工审核。
- 生成前及返回结果前核对来源与授权。V1 采用严格发布版本匹配，音频更新导致发布版本变化也会阻止旧结果；语义指纹自动豁免尚未启用。
- PostgreSQL 保存步骤快照，复查失败可复用已生成草稿。租约令牌和终态检查阻止过期 worker 写入。API/worker 可以独立重启。
- 审核事件不可变；相同事件重复投递去重，旧修订不能覆盖新修订。通过案例和退回案例按词义、词性、等级检索，撤销或来源过期的案例不再用于新任务。
- 原始事件属于 tsz-rust；Agent 只维护派生案例。不自动推广全局规则，不训练模型参数，不直接批准正式题目。当前命题规则由仓库中的版本化提示词维护。
- DeepSeek 使用供应商原生 `max_tokens` 字段；每一次请求（包括 JSON 修复重试）按剩余输出预算收紧上限。显式关闭思考模式。
- worker 在发送前向 `agent_model_calls` 预留调用次数与输出额度；超时、崩溃或缺少用量的回复不退还未知额度。`usage.requests` 表示预留的调用尝试，可能包含预留后尚未发送就中断的尝试；input/output tokens 仅为已知回执，费用未知保持 `null`。
- 已知用量按调用 ID 独立幂等累计，任务取消、租约移交或结果写入失败不会丢弃回执。未知消费保守占用预算，可能提前停止重试；不保证供应商计费恰好一次。

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
