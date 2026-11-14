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

验证流程与隔离约束维护在 [test](.agents/skills/test/SKILL.md)，本地 API/worker、数据库和模型来源维护在 [dev-env](.agents/skills/dev-env/SKILL.md)，交付按 [ship](.agents/skills/ship/SKILL.md)。项目规则已覆盖的通用编码原则不重复加载。

`.agents/skills` 是流程维护位置，`.claude/skills` 仅作兼容入口。真实 tsz-rust 联调、真实模型额度调用与模拟测试分别报告；缺环境和跳过不算通过，普通本地测试不自动授权真实模型调用。
