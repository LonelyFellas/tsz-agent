---
name: ship
description: 按授权审查、提交、推送 tsz-agent 改动并创建或更新 PR，复用 Python、迁移与委托协议验证；不自动合并或部署。
---

# Agent 交付

遵循 [AGENTS.md](../../../AGENTS.md) 和会话终点。读取本任务完整 diff、Git 身份、remotes、用户改动与已有 PR；fetch 后固定 origin/main 基线，复用任务分支/worktree，只交付本任务文件。仅提交不包含 push，创建 PR 不自动授权合并或部署。

## 审查和验证

沿调用链检查：来源必须为有效已发布词义、人工审核边界、幂等/租约、取消与迟到结果、审核撤销/乱序、服务授权和日志脱敏。模型费用未知保留 null，不能用模型自评替代业务规则。
迁移确认显式执行与回退影响；API/schema 按实际 Rust 消费者检查兼容，读取 [对接约定](../../../docs/integration.md)。

按 [test](../test/SKILL.md) 复用定向结果并完成本仓最终门；纯规则文档只查格式、引用和行为一致性。正常执行现有 hooks/CI，不添加或借用其他仓库的检查命令。需要独立审查时按适用项目/宿主规则执行，不把自审冒充独立审查，也不为每个技能重复审查。

## 提交与 PR

`git diff --check` 后精确暂存、核对 staged diff，使用 conventional commit；正常 commit/push，不禁用 hooks、普通 force push 或改全局配置规避失败。
失败读取真实输出，区分代码、hooks、网络和权限；新改动补必要验证。开 PR 前再次 fetch 并处理与目标分支的冲突；已有同任务 PR 就更新，正文说明问题、行为、迁移/契约、验证与未验证项，多行正文使用结构化参数或 --body-file。

报告实际提交、PR/CI 状态，分别说明模拟、真实 Rust/模型验证及跳过项。仅实施请求不执行本节外部交付；合并与部署须对应授权，不能将 push 成功当作 CI 或发布成功。
