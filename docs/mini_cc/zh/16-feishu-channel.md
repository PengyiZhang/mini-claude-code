[ < [15](15-evolution-migration.md) ] · [English version](../en/16-feishu-channel.md)

# 16 — 飞书双向 Channel 接入教程

> `mini_cc/channels/` 子系统把飞书群(以及未来的 Slack / Discord)接入成
> **双向 channel**:用户在群里 @ 机器人 → 触发一轮 agent turn →
> 关键事件(text / teammate_message / lead_nudged / tool_result)再推回群。
> 本章手把手覆盖两种入站模式(WS 长连接 / Webhook)、凭证配置、事件过滤、
> 运维排查。

---

## 1. 选哪一种入站模式?

| 模式       | 工作方式                              | 部署要求                | 何时选                                    |
|------------|---------------------------------------|-------------------------|-------------------------------------------|
| `ws`       | 进程主动 wss 出站连到飞书,飞书推事件 | 仅需出站网络,无公网 IP | **默认推荐**:本地开发、内网部署、家用宽带 |
| `webhook`  | 飞书 POST 到我们的公网 URL            | 公网 IP + 域名 + HTTPS  | 已有公网 ingress / Cloudflare Tunnel      |

底层差异:

- **WS 模式** 用 [`lark-oapi`](https://pypi.org/project/lark-oapi/) SDK,
  自带握手 / ack / 自动重连。`ChannelReceiverSupervisor` 单例管理所有
  WS-mode binding 的 receiver 线程(类似 `MCPPool` 的结构)。
- **Webhook 模式** 用纯 stdlib + PyCryptodome 实现,签名验证靠
  `X-Lark-Signature` (sha256(timestamp + nonce + encrypt_key + body));
  加密信封用 AES-256-CBC。

两种模式共享:出站推送(POST `/open-apis/im/v1/messages` +
`tenant_access_token`)、消息解析(`@_user_N` 提及剥离、非文本占位符)、
token 缓存(单实例锁,1 小时刷新一次)。

---

## 2. 飞书开放平台配置

无论选哪种模式,前置步骤一致:

### 2.1 创建自建应用

打开 [飞书开放平台](https://open.feishu.cn/) → 「开发者后台」→
**创建企业自建应用**。填名称 / 描述 / 图标,创建后进入应用详情页。

记下「凭证与基础信息」里的:

- **App ID**(`cli_xxx`)
- **App Secret**(`xxx`)

### 2.2 启用机器人能力

「添加应用能力」→ **机器人**。机器人名字 / 头像会显示在群里。

### 2.3 配置权限

「权限管理」→ 申请以下权限(搜索关键字即可):

| Scope                                | 用途                              |
|--------------------------------------|-----------------------------------|
| `im:message`                         | 接收群消息(必选)                 |
| `im:message.group_at_msg`            | 读取群里 @ 机器人的消息           |
| `im:message:send_as_bot`             | 发送消息到群(出站推送)          |
| `im:chat:readonly` / `im:chat`       | 拉取 chat 信息(可选,排错用)    |

申请后由租户管理员审批通过。

### 2.4 订阅事件

「事件与回调」→ 「事件订阅」→ 添加事件:

- **接收消息 v2.0**(`im.message.receive_v1`)— 这是入站文本消息的关键事件

如果你选 **WS 模式**:在「事件订阅」页顶部切到「使用长连接接收事件」。
此时飞书不需要公网 URL,客户端会主动 wss 连接。

如果你选 **Webhook 模式**:在「事件订阅」页填请求地址(下一步获取 URL)
+ 可选启用加密模式(`Encrypt Key`、`Verification Token` 会出现在页面上)。

### 2.5 发布应用

「版本管理与发布」→ 创建版本 → 申请发布 → 租户管理员审批。**未发布的
应用收不到任何事件**——这是最常见的「群里 @ 了机器人没反应」原因。

---

## 3. WS 长连接模式接入(推荐)

### 3.1 启动 server

```bash
# 装依赖(lark-oapi 是 WS 模式的硬依赖)
pip install -e .

# 启 server
export MINI_CC_PORT=8002
export ANTHROPIC_API_KEY=...
python -m mini_cc.server serve
```

server 启动日志里会看到:

```
INFO  mini_cc.channels.supervisor  ws binding chan_xxx: receiver started
```

### 3.2 注册 binding

**(推荐) Web UI:**

1. 浏览器打开 `http://127.0.0.1:8002/` → 选项目 → 左侧 sidebar `channels` tab
2. 点 `＋ new`
3. **Kind** = `feishu`
4. **Transport** = `ws`(默认;UI 会显示「— 推荐,长连接,无需公网 URL」)
5. 填入:
   - **App ID** = 飞书应用 `cli_xxx`
   - **App Secret** = 飞书应用 secret
   - **Chat ID**(可选)= 目标群 chat_id;留空 = inbound-only(只接消息不推送)
6. (可选)绑定到具体 session:留空走 project default
7. (可选)勾选 outbound event types(默认 `text / teammate_message / lead_nudged`)
8. 创建

**(等价 API 调用:)

```bash
curl -X POST http://127.0.0.1:8002/tenants/$TENANT/projects/$PID/channels \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
 -d '{
   "kind": "feishu",
   "transport": "ws",
   "config": {
     "app_id": "cli_xxx",
     "app_secret": "secret_xxx",
     "chat_id": "oc_xxx"
   },
   "session_id": null,
   "event_types": ["text", "teammate_message", "lead_nudged"]
 }'
# → { "id": "chan_abcdef123456", "kind": "feishu", "transport": "ws", ... }
```

### 3.3 验证联通

server 日志里看到 `ws: connecting → connected` 即握手成功。

在飞书群里 @ 机器人说一句话(`@<机器人名> 你好`),server 会:

1. WS receiver 收到 `im.message.receive_v1` 事件
2. `parse_message_event` 剥离 `@_user_N` token,解析出 `user_input`
3. `enqueue_inbound_turn` 在 daemon 线程里跑 `sm.send()` —— 不阻塞 SDK 的回调
4. agent loop 处理完一轮 → emit `{"type":"text",...}` →
   `ChannelDispatcher` 路由到所有订阅了 `text` 的 ws binding →
   `FeishuWsChannel.deliver` → POST 到 `/open-apis/im/v1/messages`

群内会看到机器人的回复。

### 3.4 排查

| 症状                                   | 排查路径                                                                                          |
|----------------------------------------|---------------------------------------------------------------------------------------------------|
| 注册成功但日志没有 `receiver started`  | lark-oapi 没装(`pip show lark-oapi`);或 App ID/Secret 空                                        |
| `receiver started` 但 @ 不响应         | 应用未发布;或事件订阅未启用 `im.message.receive_v1`;或机器人没被拉进群                          |
| 收到消息但群里没回复                    | `chat_id` 空(inbound-only);或 `event_types` 没勾 `text`;或权限缺 `im:message:send_as_bot`     |
| token 刷新 401                          | App Secret 错了                                                                                   |
| 多个 binding 同时跑,消息重复推送       | `event_types` 重复订阅。每个 binding 都是独立 receiver,共享 chat_id 会重复                       |

---

## 4. Webhook 模式接入

适合已有公网 ingress 的部署。

### 4.1 启动 server 并暴露公网

需要把 server 的 8002 端口暴露成 `https://your-host/`。常用三种:

- 直接部署在有公网 IP 的服务器,前面挂 nginx + Let's Encrypt
- Cloudflare Tunnel(`cloudflared tunnel`)—— 无需公网 IP
- 开发期 ngrok(`ngrok http 8002`)—— 临时调试用

### 4.2 注册 binding

Web UI 同样可用,选 Transport = `webhook` 即可。等价 API:

```bash
curl -X POST http://127.0.0.1:8002/tenants/$TENANT/projects/$PID/channels \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{
    "kind": "feishu",
    "transport": "webhook",
    "config": {
      "app_id": "cli_xxx",
      "app_secret": "secret_xxx",
      "encrypt_key": "enc_xxx",
      "verification_token": "tok_xxx",
      "chat_id": "oc_xxx"
    },
    "event_types": ["text", "teammate_message", "lead_nudged"]
  }'
# → { "id": "chan_xxx", "kind": "feishu", "transport": "webhook", ... }
```

注册后,Web UI 卡片会显示完整 webhook URL + 复制按钮:

```
https://your-host/channels/chan_xxx/webhook
```

### 4.3 填回飞书

回到飞书开放平台 → 「事件订阅」→ 把上面的 URL 填入「请求地址」。
飞书会发 `url_verification` 握手,server 自动 echo `challenge`。

如果开了加密模式,把页面上的 `Encrypt Key` 和 `Verification Token`
**填回** binding 的 config(用 PUT `update` 或重建 binding)。

### 4.4 Webhook 模式排错

- **`url_verification` 失败** — URL 不可达 / 自签证书 / URL 拼错
  (注意是 `/channels/<channel_id>/webhook`,不是 `/webhooks/`)
- **签名校验失败(开了 Encrypt Key)** — `encrypt_key` 没填或填错;
  `X-Lark-Signature` = `sha256_hex(timestamp + nonce + encrypt_key + body)`,
  server 会原样按这个公式验签
- **加密信封解不开** — PyCryptodome 没装(`pip install pycryptodome`),
  解密走 AES-256-CBC,key = SHA256(encrypt_key)

---

## 5. 配置 Schema 详解

```jsonc
{
  "kind": "feishu",
  "transport": "ws",                // 或 "webhook",默认 "ws"
  "config": {
    "app_id": "cli_xxx",            // 必填,飞书 App ID
    "app_secret": "secret_xxx",     // 必填,飞书 App Secret
    "encrypt_key": "enc_xxx",       // 可选,webhook 加密模式必填;ws 模式忽略
    "verification_token": "tok_xxx",// 可选,webhook 二次校验;ws 模式忽略
    "chat_id": "oc_xxx",            // 可选,留空 = inbound-only(不推送)
    "open_base": "https://open.feishu.cn"  // 可选,Lark 海外域名是 https://open.larksuite.com
  },
  "session_id": null,               // null = project default;指定则路由到该 session
  "event_types": ["text", "teammate_message", "lead_nudged"]
}
```

### 出站事件类型 (event_types)

控制哪些会话事件会被推送到飞书 chat:

| event_type           | 含义                                    | 默认勾选 |
|----------------------|-----------------------------------------|----------|
| `text`               | agent 文本回复                          | ✅        |
| `teammate_message`   | teammate 间消息(`[from] content`)      | ✅        |
| `lead_nudged`        | watcher 触发 lead turn                  | ✅        |
| `tool_result`        | 工具执行结果(>40 字符才推)            | ❌        |

空列表 = 订阅所有事件类型。`chat_id` 留空时,无论 `event_types` 是什么
都不会推送(inbound-only 模式)。

### Chat ID 怎么拿?

最简单:把机器人拉进群里,在群里发一句话 —— server 日志(开 DEBUG)
会打印 incoming event,`event.message.chat_id` 就是 `oc_xxx`。或者用
飞书开放平台的「调试工具」调 `im/v1/chats` API 列出所有群。

---

## 6. 安全模型

入站端点 `POST /channels/{channel_id}/webhook` 是 **公开** 的 ——
飞书(或任意 IM transport)在事件订阅流程里不可能带我们的 Bearer key。
安全性靠两点:

1. **`channel_id` 全局唯一** (`chan_<12 hex>`)—— 难猜,且独立于
   tenant / project 命名空间。
2. **每个 binding 自带 transport 级签名验证**:
   - Webhook 模式:飞书 `X-Lark-Signature`(HMAC-SHA256 over body)
     + 可选 AES 加密信封
   - WS 模式:SDK 自动握手验证,签名校验由 SDK 完成

GET 接口返回时,**所有 secret 字段** (`app_secret` / `encrypt_key` /
`verification_token`)会被掩码成 `***`(详见 `routes/channels.py:_mask_secrets`),
所以即便 admin key 泄露,秘钥也不会从 API 出去。

---

## 7. 运维与生命周期

### 7.1 Receiver 监督 (ChannelReceiverSupervisor)

进程级 singleton,按 `(tenant_id, project_id, channel_id)` 索引每个 WS
receiver。生命周期挂钩:

| 触发点                       | 行为                                                              |
|------------------------------|-------------------------------------------------------------------|
| Server 启动                  | `attach_session_manager(sm)` 后续 start_for 用它注入消息           |
| `pm.get()` 首次装配项目      | `_assemble` 遍历 ws-mode bindings → `supervisor.start_for`        |
| `POST /channels`(ws mode)   | `reg.add` 之后立刻 `supervisor.start_for`,binding 创建即可用      |
| `DELETE /channels/{id}`      | `reg.remove` 之前 `supervisor.stop_for`,避免竞态                  |
| `pm.invalidate(project)`     | 丢 cache 前 `supervisor.stop_project(tid, pid)`,下次 get 重启     |
| Server shutdown              | MCP `disconnect_all` 之后 `supervisor.stop_all`                   |

`start_for` 是幂等的 —— 同 key 二次调用先 stop 再 start,所以 binding
配置改了(比如换 App Secret)走 `invalidate` 路径会得到干净重启。

### 7.2 SDK 无 stop API 的限制

`lark.ws.Client.start()` 没有公开的 `stop()` 方法。我们的 `stop_receiver`
只设 `threading.Event`,靠 SDK 在下次重连循环检查时退出。
**生产场景下删除 binding 后,WS 连接可能要等 5~10 秒才真正断开;
极端情况下要重启 server 才彻底释放 socket。** daemon=True 保证进程
退出不会卡住。详见 `feishu_ws.py` docstring。

### 7.3 多 binding 是否共享 WS 连接?

不共享。同 `app_id` 的两个 binding 各开一条 WS 连接。SDK 未暴露复用 API,
低频场景(几个机器人)可接受。批量场景请用 webhook 模式。

### 7.4 故障转移

| 故障                  | 自动行为                                                             |
|-----------------------|----------------------------------------------------------------------|
| 网络断开              | SDK 自动重连(指数退避)                                              |
| App Secret 失效       | token 刷新 401 → `deliver` 静默 skip(不重试),inbound 仍可接收     |
| lark-oapi 未安装      | `start_for` raise `RuntimeError`,supervisor 捕获并日志,binding 仍在 |
| 飞书事件丢失          | SDK ack 之后才认为完成;无 ack 时 SDK 会重投                          |

---

## 8. 添加新的 IM transport

`channels/` 抽象的设计目标是「一个 kind 一个文件 + 一次 `register_channel_kind`」。
以 Slack Socket Mode 为例(假想):

```python
# mini_cc/channels/slack.py
from .base import Channel, ChannelBinding, InboundResult, register_channel_kind

class SlackChannel:
    kind = "slack"
    supported_transports = ("socket-mode",)

    def handle_inbound(self, body, headers) -> InboundResult: ...
    def deliver(self, event: dict) -> None: ...
    # 可选:WS / Socket Mode 接收器
    def start_receiver(self, on_inbound) -> None: ...
    def stop_receiver(self) -> None: ...

def _factory(binding: ChannelBinding) -> Channel:
    return SlackChannel(binding)

register_channel_kind("slack", _factory,
                      supported_transports=("socket-mode",))
```

然后在 `mini_cc/server/app.py:lifespan` 里加一句:

```python
from ..channels import _ensure_feishu_loaded
_ensure_feishu_loaded()
# 新增:
from . import slack  # noqa: F401  触发 register_channel_kind
```

UI 端在 `ChannelsPanel.tsx:KIND_CONFIG` 加一条 entry 即可。
完整的契约见 `mini_cc/channels/base.py:Channel` protocol docstring。

---

## 9. 进一步阅读

- 设计原始计划:`docs/plans/2026-07-07-channels-feishu-design.zh.md`
- WS receiver 计划:`docs/plans/2026-07-07-channels-ws-receiver.zh.md`
- Channel protocol 源码:`mini_cc/channels/base.py`
- 飞书 SDK 文档:<https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/sdk/server-side-sdk/python>
