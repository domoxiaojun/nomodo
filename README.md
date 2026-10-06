# nomodo

独立项目，路径为 `/Users/kkl/Documents/claude/simpread_tgbot`。所有代码位于 `src/nomodo/`，不导入、不复制或修改 `parse_hub_bot` 源码。部署包含 Reader 和专用 Worker 两个容器，通过内部 HTTP 通信。

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
uv run python -m nomodo
```

本地运行时，`PARSEHUB_WORKER_URL` 指向实际可访问的 Worker，例如 `http://127.0.0.1:8080`。Compose 中的 `127.0.0.1` 指向 Reader 容器自身；网络配置见 [部署说明](deploy/README.md)。

Reader 与 Worker 使用同一个专用 Bot Token，但只有 Reader 接收 updates。Worker 的发送客户端需为 `no_updates=True`，停用持有同 Token 的其他 receiver。启动会校验 Worker Bot ID、协议 v3 和 ParseHub 版本。

## 功能与命令

- 普通链接、隐藏文字链接、图片/视频说明文字中的链接或 `/read <url>`：严格按 Worker 当前能力解析。多链接进度显示「正在解析第 n/m 个链接」。不支持的 URL 不创建 Worker job。回复原消息后发 `/read` 只读取已收到的引用消息，不额外抓取聊天历史。
- 一篇文章合并为一条原生 Rich Message，包含标题、结构化正文、媒体、来源和操作按钮。长文与媒体超限在同条消息提示，完整 Markdown/HTML 可导出。
- `/start` 打开按钮首页，`/help` 分类查看帮助，`/status` 查看任务阶段与已用时间，`/articles` 分页找回尚未过期的文章。
- `/select <文章ID>` 切换当前文章；选择按用户/聊天持久保存。新解析文章自动选中。选中文章过期时提示重新选择，不自动转到另一篇文章。
- 解析和 AI 生成显示可取消的进度消息；忙碌时仍可查看状态和帮助。未开始的链接可以点击继续，失败链接可直接重试。同链接在有效期内复用已有解析结果，需要更新时点击「更多操作 → 重新解析链接」。
- `/notion connect|status|targets|target <id>|disconnect`：每用户独立授权和目标。目标是 Page 或官方 Data Source；读取目标时包含类型。长目标列表自动分消息完整发送；仅在完整检索成功后移除失效目标，失败时保留原列表。
- 完成配置和授权后点击保存按钮：显示目标、缺失属性列表，确认后才添加属性/写入。保留原文、代码、列表、表格及来源，AI 结果放在独立区域。
- `/notion jobs` 查看持久导出任务，`/notion recover <任务ID> <页面ID>` 核对未知写入，`/notion retry <任务ID>` 确认后继续保存。使用原文章快照及原目标，进程重启、切换文章/目标、待处理文章过期不会让恢复选错对象。找不到已完成批次标记时仍保持 `unknown`，禁止直接重试创建页面。
- 兼容 `/notion recover <页面ID>`：以当前选中文章和当前目标核对。旧任务没有快照时不会推测文章；需重新解析原文章并尝试保存以关联快照，再按任务 ID 恢复。历史内容哈希、页面/批次标记不变。
- `/summary [brief|detailed]`、摘要按钮、`/translate [语言]` 或标签/翻译/标题建议按钮：生成独立派生结果，不覆盖原始 Article。摘要按全文写成可读的结论和要点；原文依据在「查看依据」里，不写进摘要正文，也不显示费用。结果按所选操作显示为富文本，可导出。支持长文分块和完整文件下载。
- `/ask <问题>` 或无链接的普通私聊文字：只依据当前文章问答，回答附原文引句；无依据时明确说明。`/ai` 查看进度和费用，`/ai result <任务ID>` 取回结果/依据/下载按钮，`/ai retry <任务ID>` 继续未完成分块。
- 排版按钮使用本地规则，不调用模型。长文问答使用临时检索并在证据不足时扩大范围，后续问答复用增量历史。并发、术语表与可选证据复核可配置，默认评测入口不联网。
- `/settings llm on|off`、`/settings language zh-CN`：每用户独立开关和翻译语言。用户 LLM 默认开启。
- 设置页从接口读取模型列表，可分页选择；读取失败仍可手动输入。支持思考强度、语言和恢复默认设置。也可用 `/settings model <模型>`。
- `/settings key <key|clear>`：使用 AES-GCM 保存个人 Key，尽力删除包含 Key 的 Telegram 消息。管理员需先配置 `NOTION_CREDENTIALS_KEY`；不配置个人 Key 时使用服务器默认 Key。个人 Key 绑定端点，端点变化需重新设置。
- `/agent <目标>`：仅在显式调用时，针对当前选中文章生成最多 4 步计划；用户确认后串行执行注册动作。模型不能选择 URL/Notion ID，不能调用 Shell、任意 HTTP/SQL 或读取文件。普通私聊文字用于阅读问答，不生成计划。
- `/cancel`：只在发起任务的聊天中取消正在运行的用户请求，并保留文章。移除文章需要在「更多操作」中点击并确认。

Notion、LLM、授权/设置与工作流操作仅限白名单用户私聊。群组只允许白名单用户预览和导出文件；文章与确认按钮同时绑定用户和聊天。

## Notion 与 LLM 配置

Notion 配置需要 `NOTION_CLIENT_ID`、`NOTION_CLIENT_SECRET`、`NOTION_OAUTH_REDIRECT_URI` 和 `NOTION_CREDENTIALS_KEY`。回调必须为 HTTPS，详细代理示例见 [部署说明](deploy/README.md)。OAuth state 一次性使用，绑定 Telegram 用户及发起浏览器的 HttpOnly/Secure cookie。官方 token 文档未声明 PKCE，默认关闭可选 PKCE；不要把配置开关当作官方支持证明。

LLM 默认启用，默认模型为 `gpt-6.1-sol`，思考强度为 `high`。管理员填写有效 `OPENAI_API_KEY` 和模型名后才能启动；用户可通过 `/settings llm off` 关闭或 `/settings llm on` 重新启用，服务器 `LLM_ENABLED=false` 可统一关闭。同一 OpenAI SDK 显式支持 Responses/Chat Completions，端点由 `OPENAI_BASE_URL` 配置（默认 `https://api.openai.com/v1`，自定义地址须包含协议及 API 路径）。正文逐字段脱敏；长文按结构分块，不设置全文字符或分块总数上限。完整配置、能力声明、错误处理与临时数据边界见 [LLM 阅读助手](docs/llm-reader.md)。

单价和每日预算可选，默认均为 0，未配置单价不阻止启动。配置了输入/输出单价后，每日美元预算按请求预留、按返回 usage 结算；超时、取消或缺少 usage 时保留预留并标记费用不确定。已知模型使用对应 tokenizer，未知模型使用 UTF-8 字节保守估计。重复操作命中缓存不重新计费；重新生成明确计费。估算不等于供应商账单。

## 数据与边界

默认思考强度为 `high`。使用非空强度前，须在 `LLM_REASONING_EFFORTS`（默认 `low,medium,high`）或对应模型的 `LLM_MODEL_CAPABILITIES` 中声明支持；不会推测未知模型能力。因此只设置 `LLM_REASONING_EFFORT=high`、沿用代码默认能力列表的部署可以启动。`/settings reasoning default` 使用模型默认行为，不发送思考参数。可填写 SDK 支持的 `none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`；未知值会在启动时被拒绝。`LLM_MAX_INPUT_CHARS` 不再限制全文。Compose 自动从根目录 `.env` 读取此项，修改后需重建 Reader 容器使环境变量生效。

- Article 层使用 PyPI ParseHub 的纯本地 matcher，再要求平台存在于 Worker 能力快照；不调用本地 ParseHub parse/download。版本必须与 Worker 一致。
- Worker 返回的结果经过字段白名单过滤。旧 Worker 即使返回内部路径，Reader 也不保存或传给模型。
- Pending Article、过滤后的原始结果和派生内容保存在 `data/reader/reader.sqlite3`；默认 30 分钟过期，维护循环续租，过期/取消释放。正在执行用户任务时暂缓过期清理并继续续租，任务完成后恢复清理。网络故障时最终由 Worker TTL 回收。
- Notion 保存成功或失败都保留待处理文章的媒体租约，以便重试或另存目标；由文章过期或确认移除统一释放。`/cancel` 不删除文章。Notion 401 会清除失效授权，403 只报告资源权限不足并保留授权。
- Notion 凭据、个人 Key、目标、导出 checkpoint 保存在 `data/notion/notion.sqlite3`。写入结果不明会停止并保留 `unknown`，不会重试创建页面；恢复依靠页面/批次标记。
- 新导出任务同时持久保存原文章和派生内容快照，快照不持有媒体租约，不延长 Worker 文件寿命。恢复可利用已保存 blocks；未上传/已过期的媒体仍受租约及 Notion 上传文件时效约束，不能把恢复命令当成永久媒体备份。
- Notion 单文件上传限制为 20 MB，失败媒体降级为安全外链或说明；一张图片失败不会丢失正文。Live Photo 以相邻图片/视频处理。
- Telegram 预览使用 Worker 处理后的字节，不重新编码；最多嵌入 20 项、每项 20 MB、总预算 40 MB，超限在结果内提示并保留来源。发送结果不明时不自动重发。
- Markdown/HTML 保留语义块；嵌套列表会展开为列表项文本，粗体/斜体等行内样式简化，行内链接独立保留。这不是浏览器扩展的逐像素复刻。
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

## 完整 Compose 部署

`deploy/compose.yaml` 同时启动 Reader 和专用 Worker。Worker 从 GitHub 固定提交构建，只在 Compose 内网提供服务，不占用宿主机端口。Reader 使用根目录 `.env`；Worker 使用不入库的 `worker.env`，设置 `BOT_TOKEN`、`API_ID`、`API_HASH`、`WORKER_SERVICE_KEY`。两者 Bot 身份相同，Worker 不接收 updates。Reader 的 `PARSEHUB_WORKER_SECRET` 必须与 Worker 密钥相同，`PARSEHUB_WORKER_URL=http://worker:8080`。Worker 数据独立保存在 `data/worker/`。

填写 `READER_ALLOWED_USER_IDS` 后运行 `docker compose -f deploy/compose.yaml up -d --build`；模型和平台 Cookie 按实际服务另行配置。

详细交互流程、降级规则与验证边界见 [交互审查](docs/interaction-review-2026-10-03.md)。

### 连续操作

从「最近文章」打开旧文章后，后续摘要和翻译会作用于这篇文章。已有 AI 结果按模型、思考强度和语言匹配后直接显示，点击「重新生成」才重新调用；更改相关设置也会生成新结果。

从文章开始保存到 Notion，遇到未授权或未选位置时，机器人保留这篇文章。授权网页和通知都有下一步按钮；选择位置后会继续展示原文章的保存确认，仍需用户确认才写入。

全部交互入口、状态及回归覆盖见 [全流程验收矩阵](docs/interaction-matrix-2026-10-03.md)。
