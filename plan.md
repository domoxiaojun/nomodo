# 独立 SimpRead Telegram Bot 实施计划

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

SimpRead 浏览器扩展仅作功能参考。本轮只修改本独立 Bot 仓库，不改 ParseHub Worker，不执行部署或真实外部写入。

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
