---
name: dev-env
description: 诊断或按授权准备 tsz-agent API/worker、本地独立 Postgres、Rust 委托接口和模型配置，核实两进程版本及凭据隔离；不执行服务器部署。
---

# Agent 本地环境

遵循 [AGENTS.md](../../../AGENTS.md)。诊断默认只读；已授权本地准备时可启动本任务独立资源，不能接管未知进程或沿用生产/其他任务数据库。

1. 从 Git 身份核实任务 checkout、SHA/脏差异、Python 3.13 与 uv 锁文件；分别记录 API 和 worker 的 PID、cwd、启动时间、构建来源。默认 8390 仅为定位起点，当前 HEAD 不证明旧进程加载版本。
2. 读取 [运行说明](../../../README.md) 与 `src/tsz_agent/config.py`，脱敏核实 Agent 数据库、Rust base URL、两个方向的服务凭据来源。Agent 不连接业务数据库；密钥、完整 headers、原始异常及整份 env 不输出。
3. API/worker 启动不自动迁移。核实数据库归属和 schema 后，才显式执行 `uv run --env-file .env alembic upgrade head`；启动入口分别为 `uv run --env-file .env uvicorn tsz_agent.app:create_app --factory --host 127.0.0.1 --port <port>` 和 `uv run --env-file .env python -m tsz_agent.worker`。不以启动试探未知库。
4. API/worker 核对实际选择的模型与 provider；ChatGPT 模式需要二者使用同一私有 auth 目录，测试使用独立目录，绝不复制/打印用户凭据。诊断不自动登录、探测模型或调用真实额度；真实调用沿用用户明确授权及预算。
5. `/healthz` 只证明进程，`/readyz` 只证明 API 数据库及表；还需分别证明 worker、授权词库请求与目标模型调用，不能由模型目录或 ready 推断推理成功。

Rust 接口或页面代理问题按实际涉及 checkout 读取各仓 dev-env；版本和接口以真实调用、当前 OpenAPI 与 [对接约定](../../../docs/integration.md) 核实。测试数据库与 provider 隔离细节见 [test](../test/SKILL.md)。
只停止本任务拥有且身份明确的进程，不批量 kill、清库或删除共享 auth 目录。记录服务句柄、日志与临时资源清理方式，结束定向清理并复查。
交付 API/worker 各自版本、URL、数据与服务去向、模型来源和已验证/待验证项。服务器发布仍需核实其专用流程和对应授权，不套用 Rust 部署脚本。
