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
