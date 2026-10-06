# 独立阅读助手实施计划

## 统一部署线与阅读线

`main` 合并 `codex/env-compose-deploy`。交互、富消息、Notion 引导和 Reader+Worker Compose 以部署线为准。正文结构、导出任务快照和分块 AI 阅读接在这套 Telegram 外壳上。

- LLM 默认开启，模型 `gpt-6.1-sol`，思考强度 `high`。单价可选；配置单价后仍按每日预算预留和结算。
- `LLM_REASONING_EFFORTS` 默认 `low,medium,high`。只设置 `LLM_REASONING_EFFORT=high` 的部署可以启动。
- 普通私聊文字对当前文章提问；`/agent` 才生成计划。`/cancel` 不删除文章。过期或已删除的显式选择不会自动改到另一篇。
- 预览是一条富消息。超过 20 MB 的媒体不下载、不另发视频，并在同一条消息里说明。

全量 pytest **245 passed**（1 个既有 Pyrogram warning），Ruff 与 mypy 通过。未推送，未部署，未删除 `simpread_tgbot-deploy` worktree。

## LLM 评测失败费用补齐

- ✅ 补齐失败任务编号、费用不确定性和单次新增费用，区分缓存结果与新增请求。
- ✅ 模拟验证成功、超时、usage 缺失、预算不足及建任务前失败，更新文档并完成必要检查。

本轮新增 6 个回归用例；全量 pytest **188 passed**（1 个既有 Pyrogram warning），Ruff、mypy（55 文件）、文档离线链接检查通过。未执行真实付费调用、提交、推送或部署。

## LLM 第二轮：质量、速度与费用

继续保留其他未提交改动；不调用付费模型、不提交或部署。

- ✅ 精确任务预算、整段翻译上下文、术语表复用和本地确定性排版。
- ✅ 临时段落检索（无依据继续全量）、增量历史及校验后的中间结果缓存。
- ✅ 受控并发、限流降速、原子预算与取消清理。
- ✅ 论断/证据风险检查、可选独立复核和模型评测运行入口。
- ✅ 回归与验证、配置/文档更新及性能证据；真实质量验收保留未执行。

本轮结果：全量 pytest **182 passed**，保留 1 个既有 Pyrogram warning；Ruff 全仓通过、mypy 通过（55 个 Python 文件）、Taplo、uv 锁文件、Lychee 离线和 diff 检查通过。默认评测入口已实际运行，报告 `live=false`、`network_requests=0`。

合成对照（仅 MockTransport）：11,949 字符文章按旧规则产生 11 个提取组；新实现简短摘要实际模拟 HTTP 请求 1 次，详细摘要再调用 1 次并复用已校验提取结果。不将此推算成真实模型的质量、时延或账单改善比例。

新增 `docs/llm-reader.md` 调优与评测说明；`uv run python -m simpread.eval_reader` 默认只检查本地样本，真实调用需要显式 `--live --max-usd`，本轮未运行。没有提交、推送或部署。

## LLM 阅读助手完整实施

按本次确认的方案实施；保留已有小红书/渲染等未提交修改。仅临时阅读，不部署、不付费调用。

- ✅ Responses/Chat 适配、独立输出结构、能力配置及端点绑定个人 Key。
- ✅ 临时 AI 任务、分块 checkpoint、缓存、问答记录、费用预留与结算。
- ✅ 全文分块、翻译还原、排版校验、带依据摘要/问答及取消续作。
- ✅ Telegram 命令、结果/依据/下载/重生成按钮、临时数据清理。
- ✅ 固定质量样本、协议及用户流程回归、Ruff/mypy/配置/文档验证。
- ✅ 更新说明、结果和真实服务验收边界：`docs/llm-reader.md`、README、`.env.example`。

本轮验证记录：LLM 阅读助手及既有 SDK/失败边界子集 **72 passed**；全量测试快照 **140 passed**，1 个既有 Pyrogram event-loop deprecation warning。Ruff 全仓通过；mypy 通过（检查时 49 个 Python 文件）；Taplo、`uv lock --check`、Lychee 离线检查及 `git diff --check` 通过；src 脱敏 Gitleaks 扫描未发现泄露。工作区同时有平台内容功能更新，保留其实现，仅修正通过全仓检查所需的少量导入/换行问题。

实现默认仍连接官方 Responses，可显式配置 Chat/CPA、JSON 模式、模型能力与思考强度；新增 tiktoken，不安装数据库服务。固定中英文、代码、表格、嵌套、无答案、注入样本已建立；协议与恢复测试使用模拟服务，不代表真实模型事实一致性、翻译质量或 CPA 已验收。未提交、推送、部署或发起真实付费调用。

## 2026-10-02：第一阶段落地（继续上一轮修改）

范围：持久导出任务与恢复、文章选择和完整列表、Telegram 链接输入、优雅退出。保留旧内容哈希及 checkpoint，不改变 Worker，不部署。

- ✅ 核对工作区与审查记录，确认上一轮未提交修改；只读核对 Worker 幂等合同。
- ✅ 实现可持久定位的 Notion 导出任务、按任务确认恢复/重试，兼容旧记录。
- ✅ 实现按用户/聊天隔离的文章选择、完整列表输出、隐藏/引用链接和多链接进度。
- ✅ 清理失效目标、检测异常分页、完善维护健康状态与退出时活跃任务清理。
- ✅ 添加回归测试，运行 pytest、Ruff、mypy 与文档校验。
- ✅ 更新使用说明及后续边界，完成本阶段后停止。

本阶段验证：`uv run --no-sync pytest -q` **82 passed**，1 个既有 Pyrogram event-loop deprecation warning；Ruff 通过；mypy 通过（41 个 Python 文件）；Lychee 离线 4 个链接通过；`git diff --check` 通过。重点验证 unknown 导出在重启、文章过期、默认目标切换后按原任务恢复，创建请求只发生一次；旧 SQLite checkpoint 能添加任务索引并保留原标记/批次；跨用户任务不可恢复；长列表无截断；关闭时先等待清理再关客户端。

未执行提交、推送、部署或真实外部写入。Worker 请求持久恢复、内容哈希版本迁移、上传文件过期重建、EPUB/PDF/Web App 和真实服务验收继续保留为后续阶段。

## 2026-10-02：以 Telegram Bot 为目标的审查与完善

浏览器扩展仅作功能参考。本轮只修改本独立 Bot 仓库，不改 ParseHub Worker，不执行部署或真实外部写入。

- ✅ 核对目标目录、干净工作区、项目配置及现有功能与测试。
- ✅ 检查解析、Telegram 任务/取消、文章有效期、Notion 导出恢复及内容保真。
- ✅ 修复明确缺陷，增加覆盖用户行为的回归测试：租约所有权、跨聊天取消、Worker 异常清理、富文本/预览、Notion 权限区分、LLM 脱敏保真。
- ✅ 完成项目声明的最小测试、静态检查和类型检查：pytest 70 passed；Ruff、mypy 通过。
- ✅ 更新 README 的功能边界及 `docs/review-2026-10-02.md` 的专项审查、后续优先级和验证限制。

验证记录：`uv run --no-sync pytest -q` 70 passed，保留依赖 Pyrogram 的 1 个 event-loop deprecation warning；`uv run --no-sync ruff check src tests` 通过；`uv run --no-sync mypy src tests` 通过（38 个 Python 文件）；Lychee 离线检查 4 个链接通过；`git diff --check` 通过。未提交/推送，未执行真实 Telegram、Worker、Notion 或 LLM 验收；未改变历史内容哈希和导出 checkpoint，以免使旧幂等记录失配。

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
- ✅ 发布并部署 3b81979，Reader/Worker 均 healthy，Reader ready=true；Telegram API 实际返回 12 项菜单。真实客户端显示待用户新链接验收，未代发测试消息。

## 2026-10-03：操作连续性与授权闭环

- ✅ 核对线上版本，识别授权后、选择目标后、模型设置及重复 AI 操作的流程断点。
- ✅ 改进首页实际状态、模型与思考强度按钮、输入取消，避免 /cancel 意外清除文章。
- ✅ Notion 授权页面提供明确反馈与返回入口，授权通知可点击，选择目标后继续原文章保存确认。
- ✅ AI 缓存结果可直接查看、显式重新生成；文章移除单独确认。
- ✅ 85 项测试、Ruff、mypy 通过；覆盖原文章续存、用户/聊天选择、缓存匹配、输入取消、授权页面和一次确认边界。
- ✅ 已部署 416c276，Reader/Worker 均 healthy；实际菜单已更新，公网 OAuth 页面返回预期 400 和返回机器人入口；默认模型仍为 gpt-6.1-sol/high。真实客户端授权/保存操作未代为执行。

## 2026-10-03：全流程交互统一与验收

- ✅ 核对线上版本和既有改进，建立全流程范围：入口、导航、任务、文章、AI、导出、Notion、设置、异常与权限。
- ✅ 统一菜单返回、回调反馈、忙碌状态和可操作的错误提示，消除无出口状态。
- ✅ 完善解析复用/重试、当前任务状态、文章上下文及导出文件命名与说明。
- ✅ 完善 AI 失败重试、模型列表选择、参数状态、恢复默认设置。
- ✅ 完善 Notion 连接/断开/保存/恢复和取消设置流程，保留确认与原文章绑定。
- ✅ 全量 113 项测试、Ruff、mypy（46 个文件）通过；新增旅程测试和全流程验收矩阵，覆盖权限、过期、取消、重试和未知发送结果。
- ✅ 已更新 README 和全流程验收矩阵；部署 d521612，双容器 healthy、Reader ready=true，13 项菜单含 /status；模型列表只读实测 44 项且包含默认模型；OAuth 页面返回预期 400 与返回机器人入口。真实客户端显示、用户授权/保存仍需实际使用确认，未代发测试消息或写入 Notion。

## 2026-10-03：修复 Notion 等待连接无指引

- ✅ 将首页未授权状态改为明确说明，并直接提供个人 OAuth 链接与三步操作指引；授权后显示选择保存位置。
- ✅ 未连接时点击 Notion 菜单直接进入授权指引，提供重新开始、继续和返回入口。
- ✅ 36 项相关测试、Ruff、mypy 通过，覆盖授权链接用户绑定、未授权/已授权状态切换、群聊隔离；c9aef6c 已部署，Reader healthy。真实个人授权仍由用户完成。
