# 双向 Channel 抽象 + 飞书 Webhook 接入设计

## 目标

把现有的「project 事件 → 单向外发 HTTP webhook」能力（`WebhookRegistry` /
`WebhookDispatcher`）抽象成**双向 channel**：外部 IM 工具（飞书 / Slack /
Discord …）可以：

1. **入站**：用户在飞书群里发消息 → 飞书回调我们的 webhook → 解析验签
   → 把消息当作 `user_input` 注入到绑定的 lead session，触发一轮 agent
   turn。
2. **出站**：lead / teammate 产生的关键事件（`text` / `teammate_message` /
   `lead_nudged` / `tool_result`）→ 渲染为文本 → 推回飞书会话。

抽象层之后再加 Slack / Discord 只需新增一个 `channels/slack.py` 文件，
不动 server / session / 项目管理层。

## 关键设计决定

### 为什么不和 `WebhookRegistry` 合并？

`WebhookRegistry` 是**纯单向外发**：project 事件 → 外部 URL。它的语义、
认证模型、payload 形状都基于这个假设。Channel 是**双向**的，需要：

- 每种 transport 的入站签名验证（飞书 `X-Lark-Signature`、Slack
  `X-Slack-Signature`、Discord `Ed25519` …）。
- 每种 transport 的凭证管理（飞书 app_id+app_secret 出站 token 刷新）。
- 入站路由：外部服务打到我们的 webhook，必须能定位 binding、解析 payload、
  路由到 session。

把这些塞进 `WebhookDispatcher` 会让它知道每种 transport 的细节，违背单一
职责。拆出独立的 `channels/` 层后，每家 IM 的逻辑各居一文件，server /
session / 项目管理层完全 transport-agnostic。

### `Channel` 协议

```python
class Channel(Protocol):
    kind: str
    def handle_inbound(self, body: bytes, headers: dict[str, str]) -> InboundResult: ...
    def deliver(self, event: dict) -> None: ...
```

两个方法的语义：

- `handle_inbound` 解析 + 验签一个入站 HTTP 请求，返回 `InboundResult`：
  - `verification_response` 不为空 → 是 setup 时的 url_verification 握手
    （飞书 / Slack 通用），HTTP 层直接 echo 这个 body。
  - `user_input` 不为空 → 是真实用户消息，注入到绑定 session。
  - 两者都为空 → 忽略（重复事件、不关心的 event_type 等），200 OK 空响应。
- `deliver` 把一个 session 事件推到外部服务。失败抛异常会被
  `ChannelDispatcher._safe_deliver` 吞掉，单个坏 channel 不会拖累其他
  channel。

### ChannelBinding 持久化

```python
@dataclass
class ChannelBinding:
    id: str               # "chan_<12 hex>"
    kind: str             # "feishu"
    config: dict          # opaque per kind
    session_id: str | None  # 绑定的 lead session；None = project 默认
    event_types: list[str]   # 出站过滤；空 = 全订阅
    created_at: str
```

落盘到 `<state_root>/<project_id>/channels.json`，与 `webhooks.json`
并列。`config` 是不透明的 dict —— registry 不关心 schema，每个 channel
实现自己负责读取/校验。

### ChannelRegistry 与 transport 解耦

`ChannelRegistry` 不知道任何具体 transport。每个 transport 文件
（`feishu.py`、未来的 `slack.py`）在 import 时调用
`register_channel_kind("feishu", factory)` 把自己的构造器注册进全局表。

服务器在 `lifespan` 启动时调一次 `_ensure_feishu_loaded()`，触发 import
完成自注册。这样：
- 不用的 transport 不付 import 成本（懒加载）。
- 加新 transport = 一个新文件 + 一行 `register_channel_kind`。

### 出站 fan-out 接入 SessionManager

仿 `WebhookDispatcher` 的模式：`_wrap_on_event` 把 session 的 `on_event`
依次包成 `(WebhookDispatcher|ChannelDispatcher)?(inner)` 的洋葱。两个
dispatcher 各自 fan-out，互不耦合。加 channel 不需要改 webhook 代码。

### 入站 webhook 的认证问题

入站端点 `/channels/{channel_id}/webhook` 是**公开**的 —— 外部 IM 调用时
不可能带我们的 `Authorization: Bearer mck_...`。安全靠：

1. `channel_id` 全局唯一（`chan_<12 hex>`）。
2. 每个 binding 自己存 transport 级别的凭证（飞书 `encrypt_key`、Slack
   `signing_secret`），由 `Channel.handle_inbound` 做签名验证。
3. 跨项目扫描定位 binding —— `pm.list()` 线性扫一遍。项目数低（每租户个
   位数），线性扫比维护全局索引更简单且足够快。

### 入站注入：fire-and-forget

`SessionManager.send` 是阻塞的 generator —— 直接在 HTTP worker 里跑会
让飞书等不到第一个 token 就超时（飞书 ~3s 超时）。所以入站路由把 turn
丢到后台线程，立刻返回 200，飞书侧看到的是「即时 ack」，后续响应通过
`ChannelDispatcher` 异步回流到飞书会话。

### 默认 session 路由

binding 的 `session_id=None` 时入站消息走 project 默认 session：

1. 有非 `teammate-` 前缀的 lead session → 取 `created_at` 最新的。
2. 否则创建一个名为 `chan` 的新 session。

未来可以让 project 标记一个 explicit default session；当前匹配用户手工
操作的行为。

## 飞书实现要点

### 入站签名

`encrypt_key` 配置时，必须验证 `X-Lark-Signature`：

```python
sig = sha256(timestamp + nonce + encrypt_key + body).hexdigest()
```

未配置 `encrypt_key`（plain 模式）时跳过验签 —— 匹配飞书 console 的
两种模式。

### 加密信封

`encrypt_key` 配置时，body 是 `{"encrypt":"<base64>"}`：

- `key = SHA256(encrypt_key)` (32 字节)
- `ciphertext = base64decode(encrypt_field)`
- `iv = ciphertext[:16]`, `data = ciphertext[16:]`
- AES-256-CBC + PKCS7 unpad

依赖 PyCryptodome（已在项目里）。未装则跳过 —— 比手搓 AES 安全。

### URL 验证握手

`{"type":"url_verification","challenge":"<str>","token":"<str>"}` →
直接 200 OK `{"challenge":"<str>"}`。

### 消息事件 `im.message.receive_v1`

- `text` 消息：解析 `content` JSON → 取 `text` → 用正则去掉 `@_user_N`
  机器人 mention 标记。
- 非 text 消息：发占位符 `[Feishu non-text message: type=image]`，避免
  静默丢弃。

### 出站 token 管理

`tenant_access_token` 缓存在 channel 实例上，`threading.Lock` 串行化刷新
避免并发 dispatchers 触发两次刷新。过期前 60s 自动刷新。

## HTTP 接口

### 项目级 CRUD（tenant auth required）

```
GET    /tenants/{tid}/projects/{pid}/channels
POST   /tenants/{tid}/projects/{pid}/channels
DELETE /tenants/{tid}/projects/{pid}/channels/{channel_id}
```

`GET` 返回的 `config` 会**掩码** `app_secret` / `encrypt_key` /
`verification_token` 等敏感字段。

### 公开入站

```
POST /channels/{channel_id}/webhook
```

无 tenant auth；通过 `channel_id` 全局唯一性定位 binding。

## 创建一个飞书 channel 的完整流程

1. 在飞书开放平台建一个自建应用，配置 `im:message` 权限，启用机器人能力，
   记录 `app_id` / `app_secret`。
2. （可选）启用加密模式，记录 `encrypt_key`、`verification_token`。
3. 启动 mini_cc server，调 API 创建 binding：

   ```bash
   curl -X POST http://127.0.0.1:8002/tenants/$TENANT/projects/$PID/channels \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{
       "kind": "feishu",
       "config": {
         "app_id": "cli_xxx",
         "app_secret": "secret_xxx",
         "encrypt_key": "enc_xxx",
         "verification_token": "tok_xxx",
         "chat_id": "oc_xxx"
       },
       "session_id": null,
       "event_types": ["text", "teammate_message", "lead_nudged"]
     }'
   ```
4. 拿到 `channel_id`，把 webhook URL `https://your-host/channels/<channel_id>/webhook`
   填到飞书「事件订阅」配置里，飞书会发 `url_verification` 握手 —— server
   自动 echo。
5. 群里 @ 机器人说话 → 飞书推到我们 webhook → 解析验签 → 注入到默认
   session → AgentLoop 跑一轮 → 出站事件通过 `ChannelDispatcher` 推回
   飞书。

## 测试覆盖

- `tests/test_channels.py`（25 用例）：registry 持久化 / 事件过滤 / kind
  注册；飞书入站 url_verification / 文本解析 / 非文本回退 / mention
  剥离 / 签名验证 / 加密；飞书出站 token 缓存 / 渲染规则 / chat_id 门控；
  `ChannelDispatcher` 路由 + 故障隔离。
- `tests/test_channels_http.py`（6 用例）：HTTP 路由 CRUD、未知 kind 400、
  入站 url_verification、入站文本注入（mock enqueue）、未知 channel 404。
- 现有 `test_functional_webhooks.py` 的 dispatcher 安装测试调整为「沿着
  `_inner` 链查找 WebhookDispatcher」—— 反映新的
  `WebhookDispatcher → ChannelDispatcher → inner` 洋葱结构。

## 已知限制 / 未来扩展

- **默认 session 启发式**：当前是「最新非 teammate session 或新建 `chan`」。
  未来应该让 project 元数据标记 explicit default。
- **入站重试**：fire-and-forget 线程挂了就丢了。重要消息可考虑落盘
  retry queue。
- **群 vs 单聊区分**：飞书 `chat_type` 字段当前没用到 —— 未来可以做
  per-chat session 路由。
- **多 channel 共配置**：当前每个 binding 独立存一份 app_secret。可以
  抽 credential set 复用。
- **Slack / Discord adapter**：按 `feishu.py` 同样的模板，注册
  `register_channel_kind("slack", ...)`。
