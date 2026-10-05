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

- 普通链接、隐藏文字链接、图片/视频说明文字中的链接或 `/read <url>`：严格按 Worker 当前能力解析，多链接显示逐项进度；不支持的 URL 不创建 Worker job。也可以回复原消息后发 `/read`，只读取已收到的引用消息，不额外抓取聊天历史。
- `/articles` 列出本聊天有效文章，`/select <文章ID>` 切换当前文章；选择按用户/聊天持久保存。新解析文章自动选中；选中文章过期时提示重新选择，不自动转到另一篇文章。
- 文章预览含标题、来源和媒体；Markdown/HTML 文件可直接导出。
- `/notion connect|status|targets|target <id>|disconnect`：每用户独立授权和目标。目标是 Page 或官方 Data Source；读取目标时包含类型。长目标列表自动分消息完整发送；仅在完整检索成功后移除失效目标，失败时保留原列表。
- 点击保存按钮：显示目标、缺失属性列表，确认后才添加属性/写入。保留原文、代码、列表、表格及来源，AI 结果放在独立区域。
- `/notion jobs` 查看持久导出任务，`/notion recover <任务ID> <页面ID>` 核对未知写入，`/notion retry <任务ID>` 确认后继续保存。使用原文章快照及原目标，进程重启、切换文章/目标、待处理文章过期不会让恢复选错对象。找不到已完成批次标记时仍保持 `unknown`，禁止直接重试创建页面。
- 兼容 `/notion recover <页面ID>`：以当前选中文章和当前目标核对。旧任务没有快照时不会推测文章；需重新解析原文章并尝试保存以关联快照，再按任务 ID 恢复。历史内容哈希、页面/批次标记不变。
- `/summary [brief|detailed]`、`/translate [语言]` 或摘要/标签/翻译/标题建议/排版按钮：生成独立派生结果，不覆盖原始 Article；支持长文分块和完整文件下载。
- `/ask <问题>` 或无链接的普通私聊文字：只依据当前文章问答，回答附原文引句；无依据时明确说明。`/ai` 查看进度和费用，`/ai result <任务ID>` 取回结果/依据/下载按钮，`/ai retry <任务ID>` 继续未完成分块。
- 排版按钮使用本地规则，不调用模型；长文问答使用临时检索并在证据不足时扩大范围，后续问答复用增量历史。并发、术语表与可选证据复核可配置，默认评测入口不联网。
- `/settings llm on|off`、`/settings language zh-CN`：每用户独立开关和翻译语言。
- `/settings model <模型> <输入单价> <输出单价>`：每用户模型设置，单价单位为美元/百万 token。
- `/settings key <key|clear>`：使用 AES-GCM 保存个人 Key，尽力删除包含 Key 的 Telegram 消息。管理员需先配置 `NOTION_CREDENTIALS_KEY`；不配置个人 Key 时使用服务器默认 Key。
- `/agent <目标>`：显式请求针对当前选中文章生成最多 4 步计划；用户确认后串行执行注册动作。模型不能选择 URL/Notion ID，不能调用 Shell、任意 HTTP/SQL 或读取文件。普通私聊文字现在用于阅读问答。
- `/cancel`：只在发起任务的聊天中取消正在运行的用户请求；没有运行请求时释放本聊天当前文章租约并使按钮过期。

Notion、LLM、授权/设置与工作流操作仅限白名单用户私聊。群组只允许白名单用户预览和导出文件；文章与确认按钮同时绑定用户和聊天。

## Notion 与 LLM 配置

Notion 配置需要 `NOTION_CLIENT_ID`、`NOTION_CLIENT_SECRET`、`NOTION_OAUTH_REDIRECT_URI` 和 `NOTION_CREDENTIALS_KEY`。回调必须为 HTTPS，详细代理示例见 [部署说明](deploy/README.md)。OAuth state 一次性使用，绑定 Telegram 用户及发起浏览器的 HttpOnly/Secure cookie。官方 token 文档未声明 PKCE，默认关闭可选 PKCE；不要把配置开关当作官方支持证明。

LLM 默认关闭，既需要 `LLM_ENABLED=true`，也需要用户 `/settings llm on`。同一 OpenAI SDK 显式支持 Responses/Chat Completions，端点由管理员配置，默认仍为官方 Responses。正文逐字段脱敏；长文按结构分块，不设置全文字符或分块总数上限。个人 Key 绑定端点，端点变化需重新设置。完整配置、能力声明、错误处理与临时数据边界见 [LLM 阅读助手](docs/llm-reader.md)。

每日美元预算按请求预留、按返回 usage 和配置单价结算；超时、取消或缺少 usage 时保留预留并标记费用不确定。已知模型使用对应 tokenizer，未知模型使用 UTF-8 字节保守估计。重复操作命中缓存不重新计费；重新生成明确计费。管理员/用户必须填写所选模型的正确价格，估算不等于供应商账单。

## 数据与边界

`LLM_REASONING_EFFORT` 留空时不发送思考参数。设置强度前须在 `LLM_REASONING_EFFORTS` 或对应模型的 `LLM_MODEL_CAPABILITIES` 中明确声明支持；不会推测未知模型能力。`/settings reasoning default` 可使用模型默认行为。既有部署如保留 `LLM_REASONING_EFFORT=low`，需要补能力声明或清空该值；`LLM_MAX_INPUT_CHARS` 不再限制全文。

- Article 层使用 PyPI ParseHub 的纯本地 matcher，再要求平台存在于 Worker 能力快照；不调用本地 ParseHub parse/download。版本必须与 Worker 一致。
- Worker 返回的结果经过字段白名单过滤。旧 Worker 即使返回内部路径，Reader 也不保存或传给模型。
- Pending Article、过滤后的原始结果和派生内容保存在 `data/reader/reader.sqlite3`；默认 30 分钟过期，维护循环续租，过期/取消释放。正在执行用户任务时暂缓过期清理并继续续租，任务完成后恢复清理。网络故障时最终由 Worker TTL 回收。
- Notion 保存成功或失败都保留待处理文章的媒体租约，以便重试或另存目标；由文章过期/取消统一释放。Notion 401 会清除失效授权，403 只报告资源权限不足并保留授权。
- Notion 凭据、个人 Key、目标、导出 checkpoint 保存在 `data/notion/notion.sqlite3`。写入结果不明会停止并保留 `unknown`，不会重试创建页面；恢复依靠页面/批次标记。
- 新导出任务同时持久保存原文章和派生内容快照，快照不持有媒体租约，不延长 Worker 文件寿命。恢复可利用已保存 blocks；未上传/已过期的媒体仍受租约及 Notion 上传文件时效约束，不能把恢复命令当成永久媒体备份。
- Notion 单文件上传限制为 20 MB，失败媒体降级为安全外链或说明；一张图片失败不会丢失正文。Live Photo 以相邻图片/视频处理。
- Telegram 预览使用 Worker 处理后的字节，不重新编码；最多预览 10 项、每项 20 MB，超限会明确提示并保留来源。没有复用旧仓库的 Telegram 投递实现。
- Markdown/HTML 保留语义块；嵌套列表会展开为列表项文本，粗体/斜体等行内样式简化，行内链接独立保留。这不是 SimpRead 浏览器扩展的逐像素复刻。
- Worker 同时提供 HTML 与纯文本时优先保留 HTML 结构（Markdown 优先级最高）；标题、列表、引用和表格内的安全链接/图片独立保留。LLM 输入按字段脱敏，再序列化，避免敏感行后的正常正文被误删。
- 停止服务时先停止接收新任务、取消并等待活跃任务完成清理，再停止 Telegram 和关闭数据库/HTTP 客户端。健康检查同时检查 Telegram 连接和维护任务存活状态；维护任务退出时返回 503。该检查不代表所有外部服务均可用。

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

实施状态与证据见 [plan.md](plan.md)，Bot 专项审查与后续方向见 [审查记录](docs/review-2026-10-02.md)。
