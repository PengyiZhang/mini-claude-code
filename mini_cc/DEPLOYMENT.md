# mini_cc 部署指南

这份文档聚焦**部署**与**`.env` 配置**。SDK / 架构 / API 列表请参考同目录的 [README.md](./README.md)。

> **运维 Runbook**:线上运维操作(健康检查、密钥轮换、备份恢复、容器沙箱运维、
> 事故响应、升级、容量规划、安全清单)请参考
> [`docs/zh/2026-06-28-deploy-runbook.zh.md`](../docs/zh/2026-06-28-deploy-runbook.zh.md)。
> 本文档只覆盖"怎么把它跑起来",**线上怎么管**去翻 runbook。

```
本仓库根目录的 .env.example 已经是一份完整模板，本文件解释每条字段的含义、
给出常见 LLM Provider 的可复制配置示例，并列出生产环境部署的最小检查清单。
```

---

## 目录

- [一、最小可运行部署（3 步）](#一最小可运行部署3-步)
- [二、`.env` 完整字段参考](#二env-完整字段参考)
- [三、模型路由：Anthropic vs litellm](#三模型路由anthropic-vs-litellm)
- [四、Provider 配方（复制即用）](#四provider-配方复制即用)
- [五、前端构建 + 后端挂载](#五前端构建--后端挂载)
- [六、Slash 命令](#六slash-命令)
- [七、生产环境部署清单](#七生产环境部署清单)
- [八、Docker / systemd / 反向代理示例](#八docker--systemd--反向代理示例)
- [九、排错](#九排错)

---

## 一、最小可运行部署（3 步）

```bash
# 1. 安装依赖（litellm 已包含在 requirements.txt 中）
pip install -r requirements.txt

# 2. 复制 .env 模板，填上你的 API key
cp .env.example .env
# 编辑 .env，至少把 ANTHROPIC_API_KEY 或 LITELLM_API_KEY 填上

# 3. 启动后端
python -m mini_cc.server
# → Server listens on http://127.0.0.1:8000
```

确认服务起来：

```bash
curl http://127.0.0.1:8000/health   # → {"ok": true}
```

首次使用需要给某个 tenant 生成一把 API key：

```bash
python -m mini_cc.server keygen my-tenant
# → mck_xxxxxxxxxxxxxxxx  tenant=my-tenant  scopes=*  expires=never
```

---

## 二、`.env` 完整字段参考

`.env` 由 `mini_cc/server/cli.py` 入口处的 `load_dotenv()` 自动加载。**`.env` 不会覆盖已存在的环境变量**——这意味着 CI / 容器编排里设过的 env 优先级更高。

> 所有字段都是可选的（除了 LLM 凭证二选一）。布尔类字段用 `1`/`0`、字符串 `true`/`false` 或留空来判断。

### 模型与凭证

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `MODEL_ID` | ✅ | `claude-sonnet-4-6` | 主模型名。**带不带 provider 前缀决定走哪条链路**——见 [§3](#三模型路由anthropic-vs-litellm) |
| `FALLBACK_MODEL_ID` | ❌ | — | 主模型 529（过载）连续失败 2 次后自动切换到的备用模型（仅 Anthropic 路径支持） |
| `ANTHROPIC_API_KEY` | 视情况 | — | Anthropic 原生 / 兼容端点的 key |
| `ANTHROPIC_BASE_URL` | ❌ | Anthropic 官方 | 用于 DeepSeek `/anthropic`、GLM、Kimi、MiniMax 等兼容端点 |
| `LITELLM_API_KEY` | 视情况 | — | litellm 路径的 key。优先级高于 `OPENAI_API_KEY` |
| `LITELLM_BASE_URL` | ❌ | — | litellm 路径的 base url。优先级高于 `OPENAI_BASE_URL` |
| `OPENAI_API_KEY` | ❌ | — | 兼容 fallback，等价于 `LITELLM_API_KEY` |
| `OPENAI_BASE_URL` | ❌ | — | 兼容 fallback，等价于 `LITELLM_BASE_URL` |

> **凭证二选一**：如果你的 `MODEL_ID` 带前缀（如 `openai/...`），用 `LITELLM_*`；不带前缀（如 `claude-sonnet-4-6`、`deepseek-v4-flash`），用 `ANTHROPIC_*`。

### 服务器

| 字段 | 默认 | 说明 |
|---|---|---|
| `MINI_CC_HOST` | `127.0.0.1` | 绑定地址。生产环境对外暴露时设为 `0.0.0.0` |
| `MINI_CC_PORT` | `8000` | HTTP 端口 |
| `MINI_CC_DATA_DIR` | `./mini_cc_data` | 项目、session、keys、metrics 的根目录 |
| `MINI_CC_CORS_ORIGINS` | `*` | 逗号分隔的允许跨域源。**同源部署（前端挂在后端）时设为空字符串** |
| `MINI_CC_WEB_DIST` | — | 显式指定前端构建产物路径。默认会自动探测 `mini_cc/web/dist` |

### 限流 / 日志 / 监控

| 字段 | 默认 | 说明 |
|---|---|---|
| `MINI_CC_RATE_LIMIT_RPM_DEFAULT` | `60` | 每个 tenant 的默认每分钟请求数（令牌桶） |
| `MINI_CC_RATE_LIMIT_RPM` | — | `tenant=rpm,tenant=rpm,...` 形式的覆盖项 |
| `MINI_CC_LOG_FORMAT` | `json` | `json` 或 `text` |
| `MINI_CC_LOG_LEVEL` | `INFO` | root logger 级别 |
| `MINI_CC_METRICS_ENABLED` | `1` | 是否开启 MetricsMiddleware |
| `MINI_CC_OTEL_EXPORTER` | — | `otlp` / `jaeger` / `console`，留空则只输出到日志 |
| `MINI_CC_OTEL_ENDPOINT` | `http://localhost:4317` | OTLP gRPC 端点 |
| `MINI_CC_OTEL_SERVICE_NAME` | `mini-cc` | OTel 资源属性 |

---

## 三、模型路由：Anthropic vs litellm

mini_cc 通过 **model 名字的前缀**自动选择 provider：

```python
# mini_cc/core/llm.py
LITELLM_PREFIXES = ("openai/", "deepseek/", "qwen/", "groq/",
                    "mistral/", "gemini/", "azure/", "command-r/", "cohere/")
```

| `MODEL_ID` 写法 | 走哪条路径 | 用的凭证 |
|---|---|---|
| `claude-sonnet-4-6`（裸名） | Anthropic SDK | `ANTHROPIC_API_KEY` + `ANTHROPIC_BASE_URL` |
| `deepseek-v4-flash`（裸名） | Anthropic SDK + DeepSeek 的 `/anthropic` 兼容端点 | 同上，需配 `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic` |
| `glm-5`（裸名） | Anthropic SDK + GLM 的 `/api/anthropic` 端点 | 同上 |
| `openai/gpt-4o-mini` | litellm → OpenAI | `LITELLM_API_KEY` + `LITELLM_BASE_URL=https://api.openai.com/v1` |
| `deepseek/deepseek-chat` | litellm → DeepSeek 原生 OpenAI 端点 | `LITELLM_API_KEY` + `LITELLM_BASE_URL=https://api.deepseek.com/v1` |
| `qwen/qwen-plus` | litellm → 通义 DashScope | `LITELLM_API_KEY` + `LITELLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `gemini/gemini-1.5-pro` | litellm → Google AI Studio | `LITELLM_API_KEY`（Google AI key），无需 base_url |

> **DeepSeek 的两种走法**：DeepSeek 同时暴露了 `/anthropic`（Anthropic 协议）和 `/v1`（OpenAI 协议）两套端点。
> - 想用 `deepseek-chat` / `deepseek-reasoner` 的完整模型列表 → 走 litellm（`deepseek/deepseek-chat`）
> - 想用 `deepseek-v4-flash` 这种 Anthropic 兼容专用模型 → 走 Anthropic SDK（裸名）

**判断当前用的是哪条路径**：在 chat 中输入 `/model`，会返回：
```
model: `deepseek-v4-flash`
backend: `anthropic`     ← 或 litellm
fallback: `—`
```

---

## 四、Provider 配方（复制即用）

下面 6 个配方都假设你已经 `cp .env.example .env`，**只需要替换 `<your-key>` 即可**。

### 配方 A：Anthropic 原生

```dotenv
MODEL_ID=claude-sonnet-4-6
ANTHROPIC_API_KEY=<your-key>
# FALLBACK_MODEL_ID=claude-haiku-4-5-20251001
```

### 配方 B：DeepSeek（Anthropic 兼容端点，用 v4-flash 等专用模型）

```dotenv
MODEL_ID=deepseek-v4-flash
ANTHROPIC_API_KEY=<your-deepseek-key>
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
```

### 配方 C：DeepSeek（OpenAI 兼容端点，用 deepseek-chat / reasoner）

```dotenv
MODEL_ID=deepseek/deepseek-chat
LITELLM_API_KEY=<your-deepseek-key>
LITELLM_BASE_URL=https://api.deepseek.com/v1
```

### 配方 D：OpenAI 官方

```dotenv
MODEL_ID=openai/gpt-4o-mini
LITELLM_API_KEY=<your-openai-key>
LITELLM_BASE_URL=https://api.openai.com/v1
```

### 配方 E：通义千问（DashScope）

```dotenv
MODEL_ID=qwen/qwen-plus
LITELLM_API_KEY=<your-dashscope-key>
LITELLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

### 配方 F：本地 Ollama / vLLM（OpenAI 兼容）

```dotenv
MODEL_ID=openai/qwen2.5:14b
LITELLM_API_KEY=anything
LITELLM_BASE_URL=http://localhost:11434/v1
```

> **GLM / Kimi / MiniMax** 都提供了 Anthropic 兼容端点，照配方 B 的模式改 `MODEL_ID` 和 `ANTHROPIC_BASE_URL` 即可。完整对照表见 `.env.example` 第 47-54 行。

---

## 五、前端构建 + 后端挂载

mini_cc 后端会自动探测并挂载 `mini_cc/web/dist/`，部署时**只需一个 origin**。

```bash
# 1. 构建前端
cd mini_cc/web
npm install
npm run build
# → dist/ 目录生成

# 2. 启动后端（会自动挂载 dist/）
cd ../..
python -m mini_cc.server

# 3. 浏览器访问 http://127.0.0.1:8000/
#    前端、API、SSE 都从同一个 origin 提供，无需 CORS
```

后端探测顺序（`mini_cc/server/app.py:141-157`）：

1. 环境变量 `MINI_CC_WEB_DIST`（绝对或相对路径）
2. `<cwd>/mini_cc/web/dist`
3. `<package>/web/dist`（已安装包场景）

**生产部署时同源访问**：把 `MINI_CC_CORS_ORIGINS` 留空或注释掉，浏览器就不需要跨域：

```dotenv
# 同源部署：留空 = 不发 CORS 头，浏览器按同源策略走
MINI_CC_CORS_ORIGINS=

# 分离部署：列出允许的前端 origin
# MINI_CC_CORS_ORIGINS=https://mini-cc.example.com,http://localhost:5173
```

SPA fallback 已实现：任何未命中 API 的 GET 都会返回 `index.html`，所以直接访问 `/projects/abc` 这样的深链接也能正确加载。

---

## 六、Slash 命令

聊天框输入 `/` 会弹出命令菜单。当前内置 5 条（详见 `mini_cc/commands/registry.py`）：

| 命令 | 别名 | 作用 |
|---|---|---|
| `/help` | `/?` | 列出所有可用命令 |
| `/clear` | `/cls` | 清空当前 session 的聊天历史（内存 + 磁盘） |
| `/sessions` | — | 列出当前 project 下所有 session，标记当前活跃的 |
| `/model` | — | 显示当前模型 + provider 后端 |
| `/compact` | — | 手动触发上下文压缩 |

**用法**：
- 输入 `/` 即弹出菜单，继续输入会过滤
- `↑`/`↓` 选择，`Enter` 执行，`Tab` 补全，`Esc` 关闭
- 也可以直接输入完整命令 + 空格 + 参数，例如 `/compact now`

**扩展自己的命令**：在 Python 侧用 `CommandRegistry.register()`，前端会自动从 `GET /sessions/{sid}/commands` 拉取并展示。

---

## 七、生产环境部署清单

上线前过一遍：

- [ ] **绑定地址**：`MINI_CC_HOST=0.0.0.0`（默认是 `127.0.0.1`，外部访问不到）
- [ ] **CORS**：同源部署留空；分离部署明确列出域名。不要保留默认的 `*`
- [ ] **Tenant keys**：为每个 tenant 生成独立 key，按需收紧 scope（`read:*` / `sessions:write` 等）
- [ ] **限流**：根据实际负载设 `MINI_CC_RATE_LIMIT_RPM_DEFAULT`，对重负载 tenant 单独覆盖
- [ ] **数据目录**：`MINI_CC_DATA_DIR` 指向持久化卷；不要用默认的 `./mini_cc_data`（容器重启会丢）
- [ ] **日志**：生产用 `MINI_CC_LOG_FORMAT=json`，便于 ELK / Loki 解析
- [ ] **前端构建**：`cd mini_cc/web && npm run build`，确认 `dist/index.html` 存在
- [ ] **API key 轮转**：`python -m mini_cc.server keys rotate <key> --grace-hours 24` 可平滑换 key
- [ ] **HTTPS**：mini_cc 自身只跑 HTTP，外层用 nginx / Caddy 终结 TLS

---

## 八、Docker / systemd / 反向代理示例

### Dockerfile（最小版）

```dockerfile
FROM python:3.11-slim
WORKDIR /app

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 源码
COPY mini_cc ./mini_cc
COPY .env.example .

# 前端构建（多阶段：在 node 镜像里 build 完再拷过来）
COPY --from=node:20-slim /build/mini_cc/web/dist ./mini_cc/web/dist

EXPOSE 8000
CMD ["python", "-m", "mini_cc.server"]
```

构建：
```bash
# 先在本地构建前端
cd mini_cc/web && npm run build && cd ../..

# 用 docker buildx 起一个 node builder，或直接 COPY 本地 dist/
docker build -t mini-cc:latest .
docker run -d --name mini-cc \
  -p 8000:8000 \
  -v /var/lib/mini_cc:/app/mini_cc_data \
  --env-file .env \
  mini-cc:latest
```

### systemd unit

`/etc/systemd/system/mini-cc.service`：

```ini
[Unit]
Description=mini_cc agent framework
After=network.target

[Service]
Type=simple
User=mini-cc
WorkingDirectory=/opt/mini-cc
EnvironmentFile=/opt/mini-cc/.env
ExecStart=/opt/mini-cc/.venv/bin/python -m mini_cc.server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mini-cc
journalctl -u mini-cc -f
```

### nginx 反向代理（HTTPS + SSE）

```nginx
server {
    listen 443 ssl http2;
    server_name mini-cc.example.com;

    ssl_certificate     /etc/letsencrypt/live/mini-cc.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mini-cc.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE 关键：禁用缓冲，拉长超时
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }
}
```

> **Caddy 更简单**：`mini-cc.example.com { reverse_proxy 127.0.0.1:8000 }` 一行搞定 HTTPS + SSE。

---

## 九、排错

### 启动报错：`ModuleNotFoundError: No module named 'litellm'`

```bash
pip install -r requirements.txt   # 确认 litellm>=1.50.0 装上了
```

### `.env` 里的变量没生效

- 确认 `.env` 在**启动 `python -m mini_cc.server` 时的工作目录**下（`load_dotenv()` 默认从 cwd 查找）
- 已经 export 的环境变量优先级更高，会覆盖 `.env`——用 `env | grep MODEL_ID` 检查
- Windows 下用 git-bash 时，路径里的 `~` 不会被展开，用绝对路径

### 聊天报 401 / 403

- 401 → API key 错误，重新 `keygen`
- 403 → key 对应的 tenant 跟 URL 里的 `{tid}` 不一致，或 key scope 不足

### 聊天回复报 litellm 错误

```bash
# 后端日志里会打印具体错误。常见原因：
# - LITELLM_API_KEY 没配（model 带前缀但没给 litellm 凭证）
# - LITELLM_BASE_URL 错了（OpenAI 兼容端点必须以 /v1 结尾）
# - 模型名前缀写错（应该是 deepseek/deepseek-chat，不是 deepseek-chat）
```

### 前端访问 404

- 确认 `mini_cc/web/dist/index.html` 存在（`npm run build` 跑过）
- 后端启动日志里找 `serving web dist from <path>`；没找到就是探测失败
- 显式指定：`MINI_CC_WEB_DIST=/opt/mini-cc/mini_cc/web/dist`

### `/help` 等命令不生效

- 路由 `GET /tenants/{tid}/projects/{pid}/sessions/{sid}/commands` 是否能返回命令列表？不能就是后端没注册
- 前端 input 框输入 `/` 应自动弹出菜单；不弹就检查浏览器 console 是否有报错

### 想清空某个 session 但不想用 UI

```bash
# 直接删 session 文件（项目目录：$MINI_CC_DATA_DIR/projects/<pid>/）
rm $MINI_CC_DATA_DIR/projects/<pid>/messages/<sid>.json
# 或在 chat 里用 /clear 命令
```
