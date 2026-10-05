# 其他平台支持分析与后续计划

日期：2026-10-02。分析后用户已授权修复；实施范围为 Reader 与 Worker 扩展层，保留此前小红书修复及其他未提交工作。

## 已完成分析

- [x] ✅ 核对本机 ParseHub 注册表（20 个平台）及公众号、X、知乎、YouTube、B 站、抖音、Instagram、Threads、微博相关源码。
- [x] ✅ 对正式链接、短链、旧域名和相似域名做本地 matcher 探测。
- [x] ✅ 用合成文章复现富文本丢失、媒体重复表现和 animation/document 类型退化。
- [x] ✅ 核对 Reader 媒体下载与 Notion 上传路径、Worker 缓存响应形态，整理依赖和优先级。

## 确认的共性问题

1. 富文本结构丢失：domain/blocks.py 将 Markdown 转为 HTML，再以 itertext 收集纯文本。合成公众号样例证明粗体消失、原位链接被单独追加、二级列表退化。修复位置为 Reader 文档模型和解析/渲染层，不能只调整站点规则。
2. 正文图片与下载图片没有可靠关联：同一合成文章含 Markdown 图片和对应 Worker media 时，输出出现原 URL 图片加末尾附件占位。Worker 的本地媒体描述缺少用于和原文匹配的源引用；Reader 只能追加。需协议中有可靠媒体关联，不能按猜测删除内容。
3. 媒体类型退化：domain/normalize.py 将 photo/live_photo 之外的类型一律生成 video block。实测 animation 和 document 均变成 video；Notion media 模块将 GIF 上传结果包装为 video，PDF 则因类型不支持而降级。应明确 image、animation、video、audio、file 的跨输出映射，并依据实际 MIME 决定表现。
4. 视频大小限制：Telegram 预览和 Worker 下载客户端保留 20,000,000 字节限制；并非 Telegram 本身的统一文件大小上限。YouTube/B 站及其他大视频可能解析成功但 Reader 无法发送。需将 Telegram 和 Notion 的传输策略分开；Telegram 可评估流式临时文件，Notion 必须按自身上传能力选择上传或来源链接，不能简单去掉内存保护。
5. 域名判断不够严格：本地 matcher 把 evilyoutube.com/watch 和 notdouyin.com/video 识别为真实平台。Reader 上轮仅补了小红书域名核对；其他平台需基于结构化 hostname/path 校验，并保留正式短链和别名。本轮未证明可利用的 SSRF，仅确认错误识别。
6. 下载重复：预览下载媒体，保存 Notion 时 upload_media 再次读取同一 lease 媒体。可评估受用户/租约/内容版本约束的短期本地媒体复用；不能跨权限复用私有内容。

## 本地链接识别探测

以下仅表示本机 ParseHub matcher 是否接受输入，不代表线上页面可读取或下载成功。

- 公众号 /s/fixture：识别；/s?__biz=...&mid=...：不识别。
- X /user/status/123：识别；/i/article/123、t.co/fixture：不识别。现有 Twitter parser 可解析推文响应附带的 article，不能因此宣称直接 article 链接也支持。
- Threads 的 threads.com/@user/post：识别；旧 threads.net 地址：不识别。
- YouTube youtu.be 和 /shorts/：识别；/live/：不识别。需要区分正在直播与已结束的直播回放，不直接扩大到无限下载直播流。
- B 站 b23.tv：识别；Instagram /reel/：识别；知乎 question/answer：识别。

## 平台优先级

1. 公众号与知乎：共同验证富文本模型、正文图片关联与 Notion 排版。知乎当前解析器显式要求登录 Cookie；没有 Cookie 的失败不能包装为格式解析错误。
2. X/Twitter：复用富文本修复，补短链和文章链接入口设计、动画类型与正文媒体关联。转帖、引用帖、线程和完整会话属于不同内容范围，需单独定义，不能将单条推文支持等同于整个线程支持。
3. YouTube 与 B 站：先解决大媒体交付及说明文本/字幕边界。当前 YouTube 输出正文来自视频 description，字幕下载参数未启用，因此不能将对 description 的摘要称为视频内容摘要。
4. 抖音、Instagram、微博：复用图集分批，完善动画/视频/文件分类，按真实样例验证长文和混合媒体。没有真实响应证据前，不推断这些平台的上游 API 已失效。
5. Threads 与其他平台注册项：优先补入口回归和登录错误分类，再按真实使用频率深入 provider API。

## 错误与缓存接口注意事项

- Worker 登录原因匹配包含“需要登录”或“login required”；知乎缺少 Cookie 的文案为“知乎需要配置已登录的 Cookie”，可能落入其他错误类别。应使用明确错误类型/代码，不只维护零散字符串匹配。
- Worker 持久化媒体缓存结果可只有 telegramFileId，没有 Reader 所需的 mediaId/lease。当前 Reader 未要求 delivery，jobs.py 对此不启用 persistent cache；这是兼容前提，不应为提速直接打开该缓存。
- Worker/Reader 版本严格一致是当前契约；平台字段扩展要考虑旧 Reader 兼容和能力协商。

## 修复实施进度

- [x] ✅ 共性媒体类型映射及 GIF/PDF/音频等输出策略，并补跨 Telegram/Notion 的回归。
- [x] ✅ 富文本模型升级及旧 Article、导出 checkpoint 兼容，再修公众号/知乎/X 长文格式。
- [x] ✅ 正式域名、别名、短链和历史链接的结构化识别；重定向访问继续由 Worker 管理。
- [x] ✅ Worker 原文媒体引用到本地媒体的映射协议，保持图文顺序且避免重复。
- [x] ✅ 拆分 Telegram 与 Notion 文件传输限制，评估有生命周期的临时文件复用。
- [ ] 使用用户提供的各平台代表链接完成真实 Worker、Telegram、Notion 验收。

停止条件：上述代码修复完成且通过对应回归；真实平台验收另行标记。未部署、未对外发送内容。


## 修复交付与验证

- Reader 新增 Inline 和递归 Block，保留旧 text/items/rows 字段；旧文章可读，content_hash 沿用原身份计算。Markdown/HTML 保留粗体、斜体、行内代码、原位链接、换行和嵌套列表；相对 URL 按原文地址解析。
- Notion 使用 rich_text annotations；嵌套内容分层写入，每次请求独立 checkpoint，超时后必须核对对应父块内的标记才能继续。旧平面批次仍使用旧恢复机制。无法在单元格限制内表达的超长表格按完整行文本降级，不截断内容。
- GIF 按图片上传 Notion，PDF/普通文件按 file，音频按 audio；媒体 MIME 不符仍降级。Telegram 小动画单独发送，图集沿用分批策略。
- Worker 基础下载器保留每个下载文件的确切原始 URL，媒体描述新增可选 originalUrl。Reader 以 URL 关联已有正文媒体，只绑定未关联节点；无来源证据时保持附件，不猜测删除。
- Worker capabilities 广播 domains/urlPatterns；Reader 优先使用服务端规则，旧 Worker 使用已知域名回退。公众号参数式地址保留必要查询参数；Threads 旧域名转正式域名；t.co 在每次重定向前核对目标域名；YouTube 已结束回放可处理，直播与预告仍拒绝。
- Telegram 超过 20 MB 的媒体使用临时文件流式下载，沿用常规上传 2 GB 边界；取消或完成后清理。Notion 保留 20 MB 单次上传策略，不与 Telegram 共用上限。
- 小媒体缓存按 WorkerClient 实例、lease ID、media ID 隔离，预览后保存 Notion 可复用；释放租约或关闭客户端时删除。未直接启用只有 Telegram file ID 的 Worker 持久缓存。

验证：Reader 全量 149 passed；Worker 相关 hooks/engine/小红书/新平台兼容 49 passed。Reader mypy 50 个文件、Worker 修改范围 mypy 5 个文件通过；修改范围 Ruff 通过。保留 Pyrogram 的 event-loop deprecation warning。

边界：X /i/article/ 直链仍不能由现有 provider 直接解析，现在明确提示发送发布文章的原始推文，不能把它描述为已支持直链全文抓取。没有开启字幕提取，YouTube description 仍不是视频转写。未执行真实平台、Telegram、Notion 验收；未提交、推送或部署。其他并行工作产生的文件保持原样。
