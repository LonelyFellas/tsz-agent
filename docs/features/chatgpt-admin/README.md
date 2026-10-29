# 管理后台 ChatGPT 登录

目标：在超管的「系统设置 → AI 账本 → 模型与账号」中使用本机 ChatGPT 登录、查看账号/套餐授权状态、切换或退出账号，并通过既有探测流程选择出题模型。DeepSeek/OpenCode 保留原路径。

## 依赖与范围

- 复用：Agent 已实现的官方 OAuth、独立本地凭据、模型适配与账本；主线已有 Rust 超管门禁、模型探测/选择代理和 Ant Design 模型设置。
- 新增：Agent 账号与短期登录尝试接口、Rust 白名单投影、前端账号区域，以及 `chatgpt` 模型来源的三端严格枚举同步。
- 无新增数据库迁移。本次交付包含三个仓库的配套 PR，不合并或部署。

本任务 checkout：`tsz-agent/codex/chatgpt-login`；配套 `tsz-rust/codex/chatgpt-admin`、`tsz/codex/chatgpt-admin`。均为各自已核实主分支基线加本任务工作区差异。

## 交互与权限

页面从 Rust 超管 API 发起登录，Agent 启动 `127.0.0.1` 的短期回调监听，前端展示官方授权链接并轮询结果。浏览器必须位于运行 Agent 的同一台电脑。完成后重新读取账号与模型目录；探测和模型切换仍由用户显式触发。刷新页面可恢复同一待完成尝试；取消、过期或 API 重启后不会继续使用旧授权事务。

页面展示邮箱、注册标识、已保存登录状态、套餐权限与当前账号。状态反映已保存授权，实际模型可用性由探测回执确认。新增账号、重授权与切换均不改变已有任务固定的账号和模型。首次授权套餐后展示用途说明，浏览器仅保存此说明的确认标记。

所有后台接口要求超管；普通管理员和未登录请求不得触达 Agent。Agent 接口要求服务 token，账号与授权响应 `Cache-Control: no-store`。账号 DTO 严格拒绝额外字段，访问/刷新/ID token、主机凭据与上游异常原文不传到浏览器。Rust 和 api-client 同时校验官方授权源与 loopback 回调，恢复中的授权链接也遵守同一校验。

账号文件锁等待和远端撤销各有 5 秒预算；撤销超时后仍清除本地 token，并返回远端撤销未确认。ChatGPT 目录查询（含凭据续期）整体限制为 5 秒，失败仅隐藏该平台；相关 Rust 代理等待 20 秒，短于前端普通请求的 30 秒截止时间。

一次只允许一个页面登录尝试；账号切换/退出在待授权期间被拒绝。尝试留在单个 API 进程内，最多保留 32 个未过期记录；本机 API 应使用单进程。退出尝试远端撤销并清除本地 token；远端撤销未确认时明确提示到 ChatGPT 设置断开应用。

## 接口

| 后台接口 | Agent 接口 | 作用 |
|---|---|---|
| `GET /api/v1/admin/chatgpt` | `GET /v1/chatgpt/accounts` | 账号状态与待完成尝试 |
| `POST /api/v1/admin/chatgpt/login` | `POST /v1/chatgpt/login` | 添加账号或重新授权 |
| `GET/DELETE /api/v1/admin/chatgpt/login/{attempt_id}` | `GET/DELETE /v1/chatgpt/login/{attempt_id}` | 状态、取消 |
| `PUT /api/v1/admin/chatgpt/account` | `PUT /v1/chatgpt/account` | 切换目录所用账号 |
| `DELETE /api/v1/admin/chatgpt/accounts/{client_id}` | `DELETE /v1/chatgpt/accounts/{client_id}` | 退出指定账号 |

模型复用 `/admin/opencode/probes` 和 `/admin/opencode/model`，`source=chatgpt`，`provider_id` 为已授权注册的 client ID。须先探测再选择。API/worker 共用 `TSZ_AGENT_CHATGPT_AUTH_DIR`；ChatGPT 登录默认启用，显式设置 `TSZ_AGENT_CHATGPT_ENABLED=false` 可关闭。

## 验证与兼容

代码验证覆盖：账号投影与鉴权、真实 loopback 回调的拒绝/取消/恢复、UI 错误与退出提示、ChatGPT 模型保存后独立重读、严格 schema 与不可信链接拒绝。

本地验收使用本任务独立 PostgreSQL/Redis 和本任务构建的 Rust/Agent、Vite dev 代理。后台登录、Rust 超管权限、Rust → Agent 服务鉴权、模型探测登记、选择写库及刷新重读为真实三端 HTTP/存储路径。OpenAI 账号与模型供应商使用明确标记的合成 fixture，不证明真实 ChatGPT 授权或真实模型可用；未触发付费模型调用。测试结束清理独立库、Redis、合成凭据和进程。

2026-10-29 验收结果：Agent 完整质量门通过（204 项测试通过、4 项因缺少匹配 OpenCode v2.1.4 运行时跳过）；api-client 660 项测试通过；后台相关 UI 18 项测试通过，lint、类型检查、格式与构建通过；Rust fmt、全 target/all-feature clippy、10 项相关测试及 OpenAPI 导出通过。内置浏览器验证真实后台登录、发起/恢复/取消授权、模拟账号状态、探测后选择并保存模型、刷新重读、退出后不自动切换模型，以及 390px 账号区域无横向溢出。临时资源已清理并独立核实目录不存在、专用监听端口关闭；脱敏截图与回执保存在本任务 `output/chatgpt-admin-acceptance/`，不纳入发布包。

新前端遇到旧 Rust 的账号端点会显示账号状态不可用，原有模型路径仍可用。新 Rust 支持旧 Agent 的原有模型来源；旧 Agent 不支持账号端点时新增账号区不可用。旧 Rust/前端不能解析 `chatgpt` 模型来源，因此使用 ChatGPT 前需先升级消费者，最后升级本机 Agent 并完成账号授权；回退前显式切回旧版本可识别的模型，并处理固定为 ChatGPT 的在途任务。没有新增迁移需要回退。

官方套餐通道的单次输出硬上限限制及费用未知口径沿用本仓 README。本次不改变线上服务配置，也不将个人本地授权方式扩展为商业托管授权。
