# 小红书支持完善计划

日期：2026-10-02。范围：独立 Reader 与 ParseHub Worker 的 ext 扩展层；参考原浏览器扩展，保持只读，不修改 site-packages 或上游原始目录。保留两仓库既有未提交改动。

- [x] ✅ 核对现有 Worker、ParseHub 2.2.4 小红书解析器及 Reader 的输入、媒体、导出和错误处理。
- [x] ✅ Worker 视频流兼容：缺少 h264、其他流名称、无有效流；通过现有 hooks 安装，保留原请求/认证/下载流程。
- [x] ✅ Reader 分享链接：识别中文分享文本边界，保留 xsec_token，HTTP 短链使用 HTTPS，限制小红书匹配到真实域名。
- [x] ✅ Reader 图集与实况图：完整顺序预览，不丢弃第 11 项以后内容；限制单批内存，保留失败计数及取消行为。
- [x] ✅ Reader 解析失败：仅传播已知 Worker 错误代码，区分登录、内容不可用、上游格式变化与下载失败。
- [x] ✅ 添加固定小红书图文/视频/实况图与错误样例，验证 Markdown/HTML/Notion 保留正文和媒体顺序；运行必要回归与静态检查。
- [x] ✅ 更新验证记录并交付，区分本地模拟验证与真实平台验收。

尚未提供真实笔记样例；不读取 Cookie，不启动 Docker，不部署、不发送 Telegram 或写入 Notion。真实访问依赖平台响应及部署中的 Worker，不能用本地样例测试替代。

## 验证记录

- Reader：小红书、Worker 接口、文章工作流、核心转换、Bot/OAuth、Notion 相关测试共 60 passed。
- Worker：小红书兼容固定样例 7 passed；启动钩子及 Worker engine 相关回归 24 passed。涉及 core 的测试使用临时目录、虚构凭据和内存 SQLite，不读取平台 Cookie 配置。
- Reader 修改文件 Ruff 通过；项目 mypy 检查 42 个 Python 文件通过。
- Worker 修改文件 Ruff 通过；小红书兼容层、hooks 和新增测试的 mypy 检查通过。
- 两仓库 git diff --check 通过。保留 Pyrogram 已有 event-loop deprecation warning。

## 使用与边界

Reader 接受小红书正式笔记地址、discovery/item 地址和 xhslink.com/cn 分享短链；签名查询参数原样送给 Worker。请从分享按钮复制完整链接，不手工删除参数。

图集按最多 10 个媒体和约 20 MB 每批分组，超过批次限制继续发送下一批，不截断总图片数。保留已有单文件 20 MB 下载限制。发送响应失败时统计受影响媒体，不盲目重发可能已经送达的相册。实况图表现为相邻图片和视频。

Worker 兼容逻辑保留原 h264、av1、h265、h266 优先顺序；可接受同样条目结构的重命名流组，包括 EF4 样例。此修复不保证所有未知响应格式均可解析，也不绕过登录、验证码或访问限制。

Markdown/HTML 保留正文换行和完整媒体引用顺序；Worker 没有公开媒体 URL 时仍为附件占位，尚不是携带媒体文件的离线打包导出。Notion 的媒体上传使用既有 lease 流程，本轮验证转换结果而非真实上传。

启用视频流修复需要部署本次 Worker 代码并重启进程；分享链接、图集和导出改善需要部署本次 Reader 代码。本轮未提交、推送或部署。
