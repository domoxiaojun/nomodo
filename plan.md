# 独立 SimpRead Telegram Bot 实施计划

目标目录：`/Users/kkl/Documents/claude/simpread_tgbot`。不修改、导入或复制 `parse_hub_bot` 仓库。单 Reader 容器通过 HTTP 连接已有 Worker；本地目录 bind mount 持久化。

- [x] ✅ 创建独立 `src/simpread` Python 3.12/uv 项目。
- [x] ✅ Article 规范化、Markdown/HTML、严格平台预检、Worker 作业和 lease 生命周期。
- [x] ✅ Notion 官方 OAuth、用户隔离/AES-GCM、Page/data source、完整正文/媒体、幂等恢复与属性确认。
- [x] ✅ Responses SDK、默认关闭、派生字段、用户设置/预算、受限工作流与显式写入确认。
- [x] ✅ Telegram 命令/按钮、私聊边界、预览、导出、取消、健康检查/退出清理。
- [x] ✅ 单 Reader Compose、本地目录映射、Dockerfile、独立说明和配置模板。
- [x] ✅ 模拟服务测试、Ruff、mypy、Hadolint、Taplo、链接/锁文件验证；核对旧仓库未修改。

## 接口约束

Worker capabilities 只有平台 ID，没有完整匹配规则。独立项目使用 PyPI ParseHub 的纯本地 matcher 并要求版本与 Worker 相同；只使用 matcher，不执行 parse/download。Worker 返回的内部字段在 Reader 边界白名单过滤，不需要修改原 Worker。

Notion token 文档未声明 PKCE，因此 OAuth 使用一次性 state 和浏览器绑定；只有服务端明确支持时才启用可选 PKCE。Notion 写入超时会持久保存 `unknown`，禁止重试创建重复页面。

## 后续阶段与真实验收

- [ ] EPUB、PDF、分享卡、Telegram Web App（P4）。
- [ ] 部署环境真实 Telegram、ParseHub Worker、Notion OAuth/API 和 OpenAI 验收；本次不调用。

## 验证记录（2026-10-01）

- `uv run --no-sync pytest -q`：42 passed；1 个依赖 Pyrogram 的 event-loop deprecation warning。
- `uv run --no-sync ruff check src tests`、`uv run --no-sync mypy src tests`：通过（38 个 Python 文件）。
- `hadolint deploy/Dockerfile`、`taplo check pyproject.toml`、`uv lock --check`：通过。
- `lychee --offline README.md deploy/README.md plan.md`：3 个本地链接通过。
- `uv build --wheel`：生成 `dist/simpread_tgbot-0.1.0-py3-none-any.whl`。
- 旧仓库 `git status --porcelain --untracked-files=all` 无输出，未修改旧仓库。

没有启动 Docker、Telegram、Notion OAuth/API、实际 ParseHub Worker 或 OpenAI 请求。测试使用模拟服务与临时 SQLite。P4 和真实部署验收保持未完成。

## 2026-10-02：oraclesg Compose 部署

- ✅ 确认独立 Bot 与 GitHub main；隔离现有未提交功能。
- ✅ 支持 .env 的 OPENAI_BASE_URL 并复用 Compose env_file。
- ✅ 配置与 SDK 路由回归验证：28 个测试通过，Ruff 通过，mypy 32 个文件通过。
- ✅ 从 GitHub main 拉取部署版本，oraclesg Compose 镜像构建通过；服务器 .env 权限 600。
- [ ] 补齐 Worker 和用户配置，启动并检查健康状态。当前缺少 Worker URL/secret、用户白名单和 LLM 模型/价格，未启动容器，LLM 保持关闭。

## 2026-10-02：补齐专用 Worker

- ✅ 确认完整部署应包含 Reader 与专用 Worker，复用 GitHub worker 分支的固定提交，不修改原 ParseHub 工作区。
- ✅ 在 Compose 加入 Worker、独立持久目录和内部网络连接。
- ✅ 自动生成共享鉴权密钥，复用同一 Bot 身份，远端构建启动。
- ✅ 双容器 healthy；Reader ready=true；Worker protocolVersion=3、ready=true、deliveryReady=true、senderState=ready。Reader 启动校验通过，ParseHub 版本及 Bot 身份匹配。

当前部署已启动。用户白名单为空，等待用户 ID 后开放使用；LLM 未启用，等待模型与价格配置；平台 Cookie 尚未配置，未进行真实链接解析或 Notion 验收。此前“缺少 Worker，未启动”的记录已由本阶段解决。

## 2026-10-03：Telegram 命令菜单与首次使用

- ✅ 核对部署：缺少菜单注册，空白名单导致 /start 静默。
- ✅ 启动时注册九项中文菜单；私聊 /start、/help、/id 可提示用户 ID，保留业务白名单。
- ✅ 8 项回归测试、Ruff、mypy 通过；Reader 已更新且 ready=true；Telegram getMyCommands 实际返回九项中文菜单，getMe 确认 @domoparsebot。未代用户发送消息，首次使用回复由用户私聊验收。

## 2026-10-03：修复消息已收到但回复失败

- ✅ 线上日志定位到 Message.reply 不支持 disable_web_page_preview。
- ✅ 共用 reply 函数改用 LinkPreviewOptions，补真实 Kurigram Message 对象的 /start 回归。
- ✅ 9 项回归测试通过，包含真实库 Message.reply_text 调用；Ruff、mypy 通过。
- ✅ Reader 已更新，ready=true 且 healthy，新容器日志无启动异常；真实消息回复仍待用户重发验收。

## 2026-10-03：默认启用 LLM 并删除价格功能

- ✅ 按用户要求默认 gpt-6.1-sol / high，用户默认启用且可单独关闭。
- ✅ 删除价格参数、费用计算与美元预算拦截；模型命令不再要求价格。
- ✅ 全量 61 项测试、Ruff、mypy（39 个文件）、链接检查通过；推送并部署后 Reader healthy，容器配置 true / gpt-6.1-sol / high，旧价格环境变量已删除。
- ✅ 用户更新 Key 后 /models 返回 200，发现 gpt-6.1-sol；项目 Responses SDK 的真实摘要调用（合成短文章、high）成功返回结构化结果。未发送 Telegram 消息或写入 Notion。

## 2026-10-03：完整交互流程与 Rich Message 优化

- ✅ 审查现有入口、解析与媒体发送、按钮、AI、设置、Notion 和异常流程，问题与改进记录于 docs/interaction-review-2026-10-03.md。
- ✅ 原生 Rich Message：一篇文章一条结果，结构化正文/表格/媒体/按钮；有界长文及媒体降级，避免重复发送。
- ✅ 完善首次使用和菜单、解析/AI 进度、可执行错误提示、设置交互和 Notion 未配置引导。
- ✅ 79 项测试、Ruff、mypy（43 个文件）通过，覆盖真实 Kurigram 序列化和单次发送、权限/回调/重复发送、媒体及 Notion 失败恢复。
- [ ] 发布部署、确认服务健康与菜单，记录真实 Telegram 客户端渲染验收边界。
