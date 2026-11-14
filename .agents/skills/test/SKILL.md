---
name: test
description: 设计和验证 tsz-agent 的 Python、存储、worker 与委托协议行为，区分离线模拟、真实 Rust 联调和真实模型调用，核实数据库及凭据隔离。
---

# Agent 验证

沿用 [AGENTS.md](../../../AGENTS.md) 与主任务验收。仅测试设计不改代码或运行数据操作；Bug 先保留可重复失败，再验证最小修复。纯规则文档检查格式、引用与规则一致性，不运行 Python 套件。

## 按风险验证

- 校验/候选引用用纯模型或 validator 测试；API/幂等、取消、迟到结果、审核乱序/撤销与 worker 租约按实际调用链选择集成测试。
- 断言正式词义来源与 `requires_human_review=true` 边界；不得以 mock 自洽证明真实 Rust 授权接口或模型质量。
- 对契约变更读取本仓 [对接约定](../../../docs/integration.md) 与 schemas/客户端，按实际消费者核对选定 Rust 的 `docs/openapi.json`。Agent 不改写 Rust spec，严格字段/枚举变化须检查新旧组合。
- `uv run python -m tsz_agent.export_openapi --check` 离线检查 `docs/openapi.json` 和 `docs/tsz-input.schema.json`。需要同步且已有授权时去掉 `--check`，审查两份差异；只读核对不生成。

## 隔离与模型调用

先按 [dev-env](../dev-env/SKILL.md) 核实数据、进程与凭据来源。读取当前 `tests/conftest.py`：集成测试只接受 loopback 的 `tsz_agent_test`，使用 `TEST_DATABASE_URL`，缺少时会跳过。fixture 会 TRUNCATE Agent 测试表，运行前须确认该实例/库为本任务独占；名称符合不代表可清空共享实例。

在已确认独占测试库中显式执行迁移，并分别设置测试实际读取的 `TEST_DATABASE_URL` 与迁移使用的 `TSZ_AGENT_DATABASE_URL`，不得连 Rust 业务库。API/worker 启动不自动迁移。
离线测试保持 provider、外部 HTTP 与凭据目录隔离，不读取或改写用户现有登录态。OpenCode 等原生测试按对接文档的隔离要求执行，不把模拟供应商结果当成真实模型成功。
真实付费模型或套餐额度调用须在用户已授权的模型/任务范围内，明确预算与目标；普通测试请求不自动授权登录、探测所有模型或使用真实额度。

## 最终门与报告

代码/配置改动定向验证后，按当前 CI 完成：

```sh
uv run ruff check
uv run ruff format --check
uv run mypy
uv run alembic upgrade head
uv run alembic check
uv run pytest
uv run python -m tsz_agent.export_openapi --check
uv build
```

Alembic/数据库检查只在前述独占隔离库执行。已有同状态结果复用；缺环境导致跳过或未运行须明确，不写成全量通过。hooks/CI 始终正常执行。
真实 Rust 联调使用选定服务版本；涉及页面再读取选定 tsz 的真实后端验收流程。分别报告代码验证、模拟、真实 Rust、真实模型、跳过项和预算/用量未知项。
结束清理本任务临时记录、账号/会话、对象和凭据副本，恢复必要状态并独立复查；保留正常业务数据及用户原有登录态，不以归档代替临时数据彻底清理。
