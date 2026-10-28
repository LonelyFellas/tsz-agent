# tsz-agent

所有用户沟通使用简体中文。Python 3.13，uv 管理依赖，FastAPI/PydanticAI/SQLAlchemy。

## 边界

- 正确答案、干扰项都必须来自 tsz-rust 授权接口返回的有效已发布词义，模型仅选候选引用。
- Agent 不连接业务数据库，不批准题目，不自行修改正式词库。所有结果 requires_human_review=true。
- 不把模型自评或未确认总结写成已生效规则。审核撤销、乱序、来源变化必须使旧经验停止用于新任务。
- 禁止记录密钥、完整 HTTP headers 或原始异常消息。模型费用未知时保留 null，不伪报 0。
- 数据库迁移显式执行；API/worker 启动不得自动迁移。

## 工作流

从 origin/main 创建 codex/ 任务分支及独立 worktree，复用已有本任务 checkout。首次空仓初始化没有 main 时可直接使用任务分支。
未经授权不提交、推送、合并或部署。不绕过已有 hooks。

定向验证后最终执行 `uv run ruff check`、`uv run ruff format --check`、`uv run mypy`、`uv run pytest`、契约导出漂移检查及构建。
集成测试仅连接独立本地 `tsz_agent_test` 数据库。真实 tsz-rust 联调、真实付费模型调用与模拟测试分别报告；缺少环境不得将 mock 通过称为真实联调成功。
