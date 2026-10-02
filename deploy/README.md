# Reader 与专用 Worker 的 Compose 部署

从此独立项目根目录在部署服务器执行：

```bash
cp .env.example .env
cp .env.example worker.env
# worker.env 仅保留 BOT_TOKEN、API_ID、API_HASH、WORKER_SERVICE_KEY，参考下文。
chmod 600 .env worker.env
mkdir -p data/reader data/notion data/worker/data data/worker/downloads data/worker/logs
# 填好 .env：专用 Token、白名单、OPENAI_API_KEY、Worker URL 和服务认证密钥。
docker compose --env-file .env -f deploy/compose.yaml up -d --build
docker compose --env-file .env -f deploy/compose.yaml ps
```

Compose 包含 `reader` 和 `worker` 两个服务。Worker 从 GitHub 固定提交构建，不依赖其他本地工作区。

Reader `.env` 设置 `PARSEHUB_WORKER_URL=http://worker:8080`，`PARSEHUB_WORKER_SECRET` 与 `worker.env` 的 `WORKER_SERVICE_KEY` 一致（至少 32 字符）。Worker 使用 `BOT_TOKEN`、`API_ID`、`API_HASH`；其值与 Reader 的 `READER_BOT_TOKEN`、`READER_API_ID`、`READER_API_HASH` 相同。Worker 不接收 updates，数据独立保存。

## 网络

`PARSEHUB_WORKER_URL` 必须在 Reader 容器内可访问：

- Docker Desktop/macOS 上的宿主机 Worker 可以使用 `http://host.docker.internal:8080`。
- Linux 的 `host-gateway` 不会把宿主机只绑定 `127.0.0.1` 的服务变为可访问。应选择已开放的内部地址或将 Reader 接入已有 Worker 所在网络，使用相应服务名；不要把内部认证 API 暴露到公网。
- 远端 Worker 使用其可访问的私有地址或 HTTPS 地址。

Worker API 协议必须为 v3，ParseHub 版本需等于本项目 `uv.lock` 的版本。Worker 与 Reader Token 的 Bot ID 必须相同，旧 receiver 不能同时运行。Worker 发媒体的 session 与 Reader 接收更新的 session 分离。

## 本地持久化

```text
项目根目录/data/reader/reader.sqlite3
项目根目录/data/reader/sessions/reader_<bot_id>.session
项目根目录/data/reader/receiver.lock
项目根目录/data/notion/notion.sqlite3
```

Compose 显式 `type: bind`，`../data` 相对 `deploy/compose.yaml` 解析到项目根目录 `data/`。没有顶层命名卷定义。`env_file: ../.env` 同样指向项目根目录。

Reader 只把受限媒体保存在内存。Worker 数据、下载缓存和日志保存在 `data/worker/` 对应子目录，均为 bind mount。

备份时先停 Reader，完整复制 `data/`（含 SQLite WAL/SHM），并单独安全备份 `.env` 中的加密密钥。没有密钥就不能恢复 Notion/个人 OpenAI 凭据。重建容器不删除宿主机目录。

## HTTPS OAuth

Reader 容器监听 `0.0.0.0:8090`，宿主机端口只绑定 `127.0.0.1:${READER_HTTP_PORT:-8090}`。OAuth URI 示例：

```dotenv
NOTION_OAUTH_REDIRECT_URI=https://reader.example.com/notion/oauth/callback
```

Notion Integration 中填写完全相同的 URI。已有 TLS 反向代理转发 `/notion/oauth/` 到宿主机 `127.0.0.1:8090`，Nginx location 示例：

```nginx
location /notion/oauth/ {
    proxy_pass http://127.0.0.1:8090;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    access_log off;
}
```

临时 code/state 位于 query 中；应用关闭 HTTP access log，代理也按示例关闭此路径日志。`/health` 只反映 Reader Telegram 连接/初始化状态，不证明解析和 Notion 导出可用。

## 升级与检查

```bash
docker compose --env-file .env -f deploy/compose.yaml logs --tail=100 reader
docker compose --env-file .env -f deploy/compose.yaml up -d --build
```

不要把包含凭据的 Compose `config` 输出发到公共位置。镜像构建白名单只复制 `src`、`pyproject.toml` 和 `uv.lock`；`.env`、本地数据库、session 与 `.venv` 不进入构建上下文。

本机没有运行 Docker、构建镜像或登录外部服务。部署服务器还需验证实际构建、健康检查、支持平台预览、OAuth、Notion 长文/图片、重复保存幂等和 LLM 失败回退。
