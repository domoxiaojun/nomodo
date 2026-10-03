# 全流程交互验收矩阵

本轮覆盖现有 Bot 的全部交互入口与主要状态。正常操作优先使用按钮，命令保留为快捷入口；每个等待、失败、空白和过期状态都提供下一步。

| 流程 | 成功路径 | 失败或边界 | 自动验证 |
| --- | --- | --- | --- |
| 首次使用 | 首页、继续阅读、帮助分组 | 未授权显示 ID；未知命令有导航；上传文件说明输入要求 | onboarding / primary_navigation / unsupported_file |
| 导航 | 首页、任务、文章、设置、Notion、帮助原地切换 | 面板不可编辑时只补一条替代面板；过期按钮返回可用入口 | panel_edit_expiry / callback_acknowledged_once |
| 等待与忙碌 | 当前任务、阶段、已用时间、取消 | 其他聊天不泄露任务内容；忙碌时新链接不自动执行，可点击继续 | busy_request_status_and_explicit_retry |
| 链接解析 | 支持文本、媒体说明、隐藏链接，多链接逐篇返回 | 失败链接提供绑定用户与聊天的一次性重试按钮 | read_failure_retry / hidden_and_caption_urls |
| 结果阅读 | 一篇原生 Rich Message，正文媒体按钮合并 | 单条限制明确说明；未知发送结果不自动重发 | rich_native_blocks / one_result / uncertain_send |
| 最近文章 | 当前文章标记、剩余时间、分页、重新打开 | 同链接有效期内复用；显式重新解析获取新结果 | opened_article / read_failure_retry_and_cache_refresh |
| AI | 摘要、翻译、更多操作，按参数匹配已有结果 | 失败提供重试/导出/设置；过期模型输入不触发 AI | ai_reuses_matching_result / ai_retry_button / expired_model_input |
| 模型与设置 | 从真实接口读取模型，分页选择；思考强度、语言和恢复默认 | 模型列表失败可手动输入；选择令牌绑定用户和聊天 | model_picker_select_reset_and_failure_routes |
| 文件导出 | 按文章标题命名，标明 Markdown 编辑或 HTML 阅读 | 文件名过滤路径/控制字符，并限制 UTF-8 字节长度 | export_filename_and_caption |
| Notion 连接 | 授权页返回机器人，选择位置，继续原文章确认 | 未配置/失效有明确出口；断开连接必须确认，不删除已有页面 | oauth_browser / target_selection_resumes / disconnect_requires_owner |
| Notion 保存 | 指定目标后确认保存，进度原地完成 | 失败保留媒体；重试绑定原目标；未知写入先核对页面、再确认恢复 | failed_notion_save / unknown_notion_write_recovery |
| 取消与权限 | 取消任务或输入，不意外移除文章 | 移除单独确认；过期输入、跨用户与跨聊天操作不能越权 | cancel_button / idle_cancel / expired_recovery_input |

## 统一交互规则

- 回调只向 Telegram 确认一次；后续错误使用可操作的消息反馈。
- 长任务期间可以查看状态、首页和帮助；新的解析请求不会被假装加入后台队列。
- 重试、模型选择、恢复与确认使用短期令牌，并绑定 Telegram 用户和聊天。重复点击不会重复消费。
- 同链接复用只发生在同用户、同聊天和文章有效期内。用户可从更多操作显式重新解析。
- Notion 结果未知时不提供盲目重发：先输入页面链接核对，再确认恢复原目标。
- 提供文件或截图但不带链接时会说明当前能力。此任务改善交互，不新增文件正文解析、OCR 或未实现的 Notion Token 登录。

## 验证记录

- 全量 113 项测试通过；Ruff、mypy（46 个文件）通过。
- 测试包括实际 Kurigram 的 Rich Message 序列化和发送请求构造，网络发送使用模拟传输。
- 模型列表使用真实接口只读验证；未为测试发起模型推理、发送 Telegram 消息或写入 Notion。
- 真实手机/桌面 Telegram 的视觉显示及用户完成授权/保存后的体验仍需要实际使用确认；服务健康检查不替代这些验证。
