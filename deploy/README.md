# Agent 测试服自动发布

GitHub 的「Deploy Agent to tshb-test」在本仓 main 的推送 CI 成功后触发。工作流核对实际 main、同一提交的最新 CI run 和成功 attempt。它为该提交构建 Linux amd64 镜像，在隔离 PostgreSQL 上显式迁移，并检查 API 和 worker。随后经 SSH 传送镜像压缩包。宿主机校验包摘要、提交标签和两个容器实际使用的镜像。

## 一次性接入

准备既有的 Agent Docker Compose 实例。服务器需要 Docker Compose v2 或更新版本、Bash、util-linux 的 flock 和 sha256sum。保留现有 Compose 项目名、独立数据库、配置路径和登录态卷，并确认部署账号能管理该 Compose 实例。目录应包含：

- .env，服务自己的数据库、Rust 地址和凭据。示例字段见上一级 .env.example。
- 默认使用 deploy/compose.yaml。现有实例使用多份 Compose 文件时，按现有顺序配置原文件列表。API 和 worker 共用 TSZ_AGENT_IMAGE。
- 已初始化的独立 Agent PostgreSQL 数据库，包括 Agent 表和 Alembic 版本。首次安装先显式初始化独立库，再接入 CD。上线前确认该库有可恢复的备份。

在本仓 GitHub 配置：

| 类型 | 名称 | 值 |
| --- | --- | --- |
| 仓库变量 | TSZ_AGENT_TSHB_TEST_DIR | 上述服务器部署目录的绝对路径。配置后启用此 CD |
| 变量 | TSZ_AGENT_TSHB_TEST_DATABASE | 服务器 .env 指向的独立 Agent 数据库名，例如 tsz_agent |
| 可选变量 | TSZ_AGENT_TSHB_TEST_PROJECT | 已部署的 Compose 项目名。默认 tsz-agent |
| 可选变量 | TSZ_AGENT_TSHB_TEST_COMPOSE_FILES | 逗号分隔的 Compose 文件绝对路径。按原启动顺序填写，文件位于部署目录内。默认只读取 deploy/compose.yaml |
| tshb-test 环境 Secrets | TSHB_TEST_SSH_HOST、TSHB_TEST_SSH_USER | 测试服地址、部署账号 |
| tshb-test 环境 Secrets | TSHB_TEST_SSH_KEY、TSHB_TEST_SSH_KNOWN_HOSTS | 部署专用 SSH 私钥、已核验的主机密钥 |

使用 current 软链接和 Compose 叠加文件时，部署目录配置为其稳定的上级目录，并将 current/deploy/compose.yaml 和各叠加文件按顺序列入变量。CD 从稳定目录读取 .env 并保存发布状态。

目录变量在任务入口读取，请配置为**仓库变量**。其余变量可放在仓库或 tshb-test 环境。CD 保留服务器的 .env、Compose 文件和挂载卷。若在合入 CD 以后才配置变量，可重跑当前 main 的推送 CI 来触发首次自动发布。

## 迁移与恢复

- 发布前核对独立数据库名称和当前 Alembic revision。先停止 API、worker，再由一次性容器显式执行 alembic upgrade head，完成后才启动新版服务。
- 同一宿主机同时只允许一个发布事务。部署前再次确认候选和 CI 版本，旧候选退出。
- 验收要求受保护的 /readyz 返回 ready，API 和 worker 运行在同一候选镜像且没有发生重启。这些检查覆盖启动和数据库就绪，实际模型和 Rust 业务调用另行验收。
- 数据库 revision 未变化时，候选启动失败可恢复已识别的旧镜像。若升级失败或新 schema 下服务启动失败，保留结果供恢复，CD 不自动降级数据库。
- 服务器的 .agent-cd/state 记录最后一次事务的提交、镜像、CI run/attempt、迁移前后版本和状态。accepted 表示验收通过。rolled_back 表示原镜像已恢复且本次发布失败。migration_failed、needs_recovery 和 stop_failed 需要先核对服务器状态。SSH 断开时按这份记录核实实际结果后再重跑。
- 手工管理部署时，以事务记录和 docker compose ps 的实际镜像为准。CD 在命令中指定本次镜像，.env 中原有的镜像设置仍由维护者管理。
