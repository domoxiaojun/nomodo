# SimpRead Telegram Bot

独立项目，路径为 `/Users/kkl/Documents/claude/simpread_tgbot`。所有代码位于 `src/simpread/`，不导入、不复制或修改 `parse_hub_bot` 源码。部署只有一个 Reader 容器，通过 HTTP 使用已有 ParseHub Worker。

```text
Telegram → 本地平台预检 + Worker capabilities → Worker prepare job
         → Article → 预览 / Markdown / HTML
                   → 用户确认 → Notion Page / Data Source
                   → 用户启用并操作 → Responses 派生内容 / 受限工作流
```

## 使用

```bash
uv sync --frozen
cp .env.example .env
# 编辑 .env；填写 Telegram 专用 Token、API ID/Hash、白名单和 Worker 地址/密钥。
uv run python -m simpread
```

本地运行时，`PARSEHUB_WORKER_URL` 指向实际可访问的 Worker，例如 `http://127.0.0.1:8080`。Compose 中的 `127.0.0.1` 指向 Reader 容器自身；网络配置见 [部署说明](deploy/README.md)。

Reader 与 Worker 使用同一个专用 Bot Token，但只有 Reader 接收 updates。Worker 的发送客户端需为 `no_updates=True`，停用持有同 Token 的其他 receiver。启动会校验 Worker Bot ID、协议 v3 和 ParseHub 版本。

## 功能与命令

- 普通链接或 `/read <url>`：严格按 Worker 当前能力解析，多个链接逐项反馈；不支持的 URL 不创建 Worker job。
- 文章预览含标题、来源和媒体；Markdown/HTML 文件可直接导出。
- `/notion connect|status|targets|target <id>|disconnect`：每用户独立授权和目标。目标是 Page 或官方 Data Source；读取目标时包含类型。
- 点击保存按钮：显示目标、缺失属性列表，确认后才添加属性/写入。保留原文、代码、列表、表格及来源，AI 结果放在独立区域。
- `/notion recover <page-id>`：以最近待处理文章为基准核对未知写入的页面/批次标记；确认恢复后再点击保存继续。找不到已完成批次标记时仍保持 `unknown`，避免重复写入。
- `/summary` 或摘要/标签/翻译/标题建议/整理排版按钮：只生成派生字段，不覆盖原始 Article。
- `/settings llm on|off`、`/settings language zh-CN`：每用户独立开关和翻译语言。
- `/settings model <模型> <输入单价> <输出单价>`：每用户模型设置，单价单位为美元/百万 token。
- `/settings key <key|clear>`：使用 AES-GCM 保存个人 Key，尽力删除包含 Key 的 Telegram 消息。管理员需先配置 `NOTION_CREDENTIALS_KEY`；不配置个人 Key 时使用服务器默认 Key。
- `/agent <目标>` 或无链接的自然语言消息：针对最近已解析文章生成最多 4 步计划；用户确认后串行执行注册动作。模型不能选择 URL/Notion ID，不能调用 Shell、任意 HTTP/SQL 或读取文件。
- `/cancel`：取消正在运行的用户请求；没有运行请求时释放最近文章租约并使按钮过期。

Notion、LLM、授权/设置与工作流操作仅限白名单用户私聊。群组只允许白名单用户预览和导出文件；文章与确认按钮同时绑定用户和聊天。

## Notion 与 LLM 配置

Notion 配置需要 `NOTION_CLIENT_ID`、`NOTION_CLIENT_SECRET`、`NOTION_OAUTH_REDIRECT_URI` 和 `NOTION_CREDENTIALS_KEY`。回调必须为 HTTPS，详细代理示例见 [部署说明](deploy/README.md)。OAuth state 一次性使用，绑定 Telegram 用户及发起浏览器的 HttpOnly/Secure cookie。官方 token 文档未声明 PKCE，默认关闭可选 PKCE；不要把配置开关当作官方支持证明。

LLM 默认关闭，既需要 `LLM_ENABLED=true`，也需要用户 `/settings llm on`。使用官方 OpenAI Python SDK Responses API，显式 `store=false`、结构化输出、HMAC 用户标识、从 `.env` 的 `OPENAI_BASE_URL` 读取服务地址（默认 `https://api.openai.com/v1`，自定义地址须包含协议及 API 路径）。只发送 Article 文本字段；不序列化媒体租约、凭据、内部异常。输入过大直接失败并保留原文，不静默截断。

每日美元预算采用持久的保守预留：UTF-8 字节数作为输入 token 上界，加上提示词余量和最大输出 token。失败请求也不退回预留额度；这可能比实际账单更早停止调用。管理员/用户必须填写所选模型的正确价格，应用不自动拉取价格。

## 数据与边界

`LLM_REASONING_EFFORT=low` 设置全局思考强度，适用于摘要、翻译及工作流规划，也作用于用户自选模型。留空或不设置时不发送 `reasoning`，沿用模型默认值。可填写 SDK 支持的 `none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`；实际支持范围取决于模型，例如 `gpt-6.1-sol` 不支持 `none` 和 `minimal`。未知值会在启动时被拒绝，模型不支持的组合会触发现有 LLM 失败回退。Compose 自动从根目录 `.env` 读取此项，修改后需重建 Reader 容器使环境变量生效。

- Article 层使用 PyPI ParseHub 的纯本地 matcher，再要求平台存在于 Worker 能力快照；不调用本地 ParseHub parse/download。版本必须与 Worker 一致。
- Worker 返回的结果经过字段白名单过滤。旧 Worker 即使返回内部路径，Reader 也不保存或传给模型。
- Pending Article、过滤后的原始结果和派生内容保存在 `data/reader/reader.sqlite3`；默认 30 分钟过期，维护循环续租，过期/取消释放。网络故障时最终由 Worker TTL 回收。
- Notion 凭据、个人 Key、目标、导出 checkpoint 保存在 `data/notion/notion.sqlite3`。写入结果不明会停止并保留 `unknown`，不会重试创建页面；恢复依靠页面/批次标记。
- Notion 单文件上传限制为 20 MB，失败媒体降级为安全外链或说明；一张图片失败不会丢失正文。Live Photo 以相邻图片/视频处理。
- Telegram 预览使用 Worker 处理后的字节，不重新编码；最多预览 10 项、每项 20 MB，超限会明确提示并保留来源。没有复用旧仓库的 Telegram 投递实现。
- Markdown/HTML 保留语义块；嵌套列表会展开为列表项文本，粗体/斜体等行内样式简化，行内链接独立保留。这不是 SimpRead 浏览器扩展的逐像素复刻。

EPUB、PDF、分享卡和 Web App 为后续 P4，当前尚未实现。真实 Telegram、Worker、Notion OAuth/API 与 OpenAI 验收未执行。

## 验证

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src tests
uv lock --check
hadolint deploy/Dockerfile
taplo check pyproject.toml
```

实施状态与证据见 [plan.md](plan.md)。
