[ < [15](15-evolution-migration.md) ] · [中文版](../zh/16-feishu-channel.md)

# 16 — Feishu Bidirectional Channel Tutorial

> The `mini_cc/channels/` subsystem wires Feishu groups (and future
> Slack / Discord) as **bidirectional channels**: @-mention the bot in a
> chat → triggers an agent turn → key events
> (text / teammate_message / lead_nudged / tool_result) flow back to the
> chat. This chapter is the end-to-end walkthrough: both inbound
> transports (WS long-connection / Webhook), credential setup, event
> filtering, and operational troubleshooting.

---

## 1. Which inbound transport?

| Transport | How it works                            | Deployment requirement | When to pick                              |
|-----------|-----------------------------------------|------------------------|--------------------------------------------|
| `ws`      | Process opens outbound wss to Feishu    | Outbound-only, no public IP | **Default & recommended**: local dev, on-prem, home lab |
| `webhook` | Feishu POSTs to our public URL          | Public IP + domain + HTTPS | Already running a public ingress / Cloudflare Tunnel |

Under the hood:

- **WS mode** uses the official [`lark-oapi`](https://pypi.org/project/lark-oapi/)
  SDK — handshake / ack / auto-reconnect handled. A process-wide
  `ChannelReceiverSupervisor` singleton (mirrors `MCPPool`) owns one
  thread per ws-mode binding.
- **Webhook mode** is pure stdlib + PyCryptodome. Signature verification
  via `X-Lark-Signature` (sha256(timestamp + nonce + encrypt_key + body));
  AES-256-CBC for the encrypted envelope.

Both modes share: outbound push (POST `/open-apis/im/v1/messages` +
`tenant_access_token`), message parsing (`@_user_N` mention strip,
non-text fallback), and token cache (single-instance lock, refreshes
hourly).

---

## 2. Feishu Open Platform setup

The platform steps are identical for both transports.

### 2.1 Create a custom app

Open the [Feishu Open Platform](https://open.feishu.cn/) → 「Developer Console」
→ **Create a custom app**. After naming + creating, open the app details.

Record from 「Credentials & Basic Info」:

- **App ID** (`cli_xxx`)
- **App Secret** (`xxx`)

### 2.2 Enable the bot capability

「Add application capability」→ **Bot**. The bot name + avatar will
appear in chats.

### 2.3 Configure permissions

「Permission Management」→ request (search keywords):

| Scope                                | Purpose                              |
|--------------------------------------|--------------------------------------|
| `im:message`                         | Receive group messages (required)    |
| `im:message.group_at_msg`            | Read @-bot messages in group         |
| `im:message:send_as_bot`             | Send messages to chat (outbound)     |
| `im:chat:readonly` / `im:chat`       | Read chat info (optional, debug)     |

Tenant admin approves the request.

### 2.4 Subscribe to events

「Events & Callbacks」→ 「Event Subscription」→ add:

- **Receive Message v2.0** (`im.message.receive_v1`) — the inbound-text keystone event

If you picked **WS mode**: at the top of the 「Event Subscription」page,
switch to 「Receive events via long connection」. Feishu doesn't need a
public URL — the SDK dials out.

If you picked **Webhook mode**: leave the URL field empty for now;
you'll fill it in after step 3 (and optionally enable encryption, which
surfaces `Encrypt Key` + `Verification Token` on the page).

### 2.5 Publish the app

「Version Management & Publish」→ create version → submit for review →
admin approval. **Unpublished apps receive zero events** — this is the
#1 cause of "@-mentioning the bot does nothing".

---

## 3. WS long-connection setup (recommended)

### 3.1 Start the server

```bash
# Install deps (lark-oapi is required for WS mode)
pip install -e .

# Run
export MINI_CC_PORT=8002
export ANTHROPIC_API_KEY=...
python -m mini_cc.server serve
```

After startup the log shows:

```
INFO  mini_cc.channels.supervisor  ws binding chan_xxx: receiver started
```

### 3.2 Register the binding

**(Recommended) Web UI:**

1. Browser → `http://127.0.0.1:8002/` → pick project → sidebar `channels` tab
2. Click `＋ new`
3. **Kind** = `feishu`
4. **Transport** = `ws` (default; UI shows "— recommended, long connection, no public URL")
5. Fill in:
   - **App ID** = `cli_xxx`
   - **App Secret** = app secret
   - **Chat ID** (optional) = target chat `oc_xxx`; empty = inbound-only
6. (Optional) bind to specific session; empty = project default
7. (Optional) check outbound event types (default: `text / teammate_message / lead_nudged`)
8. Create

**(Equivalent API:)

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

### 3.3 Verify

Server logs show `ws: connecting → connected` once the handshake completes.

@-mention the bot in the chat (`@<botname> hello`). The server:

1. WS receiver gets the `im.message.receive_v1` event
2. `parse_message_event` strips `@_user_N`, extracts `user_input`
3. `enqueue_inbound_turn` runs `sm.send()` on a daemon thread — doesn't
   block the SDK callback
4. Agent loop completes → emits `{"type":"text",...}` →
   `ChannelDispatcher` routes to all bindings subscribed to `text` →
   `FeishuWsChannel.deliver` POSTs to `/open-apis/im/v1/messages`

The bot's reply appears in the chat.

### 3.4 Troubleshooting

| Symptom                                  | Diagnosis                                                                                          |
|------------------------------------------|----------------------------------------------------------------------------------------------------|
| Created but no `receiver started` log    | lark-oapi missing (`pip show lark-oapi`); or App ID/Secret empty                                   |
| `receiver started` but @-mentions no-op  | App not published; or `im.message.receive_v1` not subscribed; or bot not added to the chat         |
| Receives but no reply in chat            | `chat_id` empty (inbound-only); or `text` not in `event_types`; or `im:message:send_as_bot` missing |
| token refresh 401                        | App Secret is wrong                                                                                |
| Multiple bindings → duplicate replies    | Multiple bindings all subscribe to the same event; same `chat_id` posts multiply                  |

---

## 4. Webhook mode setup

For deployments that already have a public ingress.

### 4.1 Expose the server publicly

You need `https://your-host/` routing to server port 8002. Three common options:

- Direct on a host with public IP, nginx + Let's Encrypt in front
- Cloudflare Tunnel (`cloudflared tunnel`) — no public IP required
- For dev: ngrok (`ngrok http 8002`)

### 4.2 Register the binding

Web UI works (Transport = `webhook`). Equivalent API:

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
```

The web UI row displays the full webhook URL + copy button:

```
https://your-host/channels/chan_xxx/webhook
```

### 4.3 Paste back to Feishu

Feishu console → 「Event Subscription」→ paste the URL into the request
address. Feishu sends `url_verification`; the server echoes `challenge`
automatically.

If you enabled encryption mode, copy the `Encrypt Key` and
`Verification Token` from the Feishu page **back into** the binding
config (via `update` or recreate).

### 4.4 Webhook troubleshooting

- **`url_verification` fails** — URL unreachable / self-signed cert / typo
  (it's `/channels/<channel_id>/webhook`, not `/webhooks/`)
- **Signature verification fails (Encrypt Key on)** — `encrypt_key`
  missing or wrong. `X-Lark-Signature` =
  `sha256_hex(timestamp + nonce + encrypt_key + body)`; server matches
  that formula exactly
- **Encrypted envelope won't decrypt** — PyCryptodome missing
  (`pip install pycryptodome`); decryption is AES-256-CBC,
  key = SHA256(encrypt_key)

---

## 5. Config schema reference

```jsonc
{
  "kind": "feishu",
  "transport": "ws",                // or "webhook", defaults to "ws"
  "config": {
    "app_id": "cli_xxx",            // required
    "app_secret": "secret_xxx",     // required
    "encrypt_key": "enc_xxx",       // optional, required for webhook encrypted mode; ignored for ws
    "verification_token": "tok_xxx",// optional, webhook secondary check; ignored for ws
    "chat_id": "oc_xxx",            // optional, empty = inbound-only
    "open_base": "https://open.feishu.cn"  // optional, Lark overseas: https://open.larksuite.com
  },
  "session_id": null,               // null = project default; specific session routes inbound there
  "event_types": ["text", "teammate_message", "lead_nudged"]
}
```

### Outbound event types (event_types)

Which session events get pushed to the chat:

| event_type           | Meaning                                  | Default |
|----------------------|------------------------------------------|---------|
| `text`               | agent text reply                         | ✅       |
| `teammate_message`   | teammate→teammate (`[from] content`)     | ✅       |
| `lead_nudged`        | watcher fired lead turn                  | ✅       |
| `tool_result`        | tool execution result (>40 chars only)   | ❌       |

Empty list = subscribe to all event types. When `chat_id` is empty, the
binding is inbound-only regardless of `event_types`.

### How to find the Chat ID

Easiest: add the bot to the chat, send any message — the server log
(DEBUG level) prints the incoming event; `event.message.chat_id` is the
`oc_xxx`. Or use the Feishu console's debug tool to call
`im/v1/chats` and list all chats.

---

## 6. Security model

The inbound endpoint `POST /channels/{channel_id}/webhook` is **public**
— Feishu (or any transport) can't carry our Bearer key during event
subscription. Security comes from two layers:

1. **`channel_id` is globally unique** (`chan_<12 hex>`)— guess-resistant
   and namespace-independent of tenant / project.
2. **Per-binding transport-level signature verification**:
   - Webhook mode: Feishu `X-Lark-Signature` (HMAC-SHA256 over body)
     + optional AES encrypted envelope
   - WS mode: SDK performs handshake verification; signatures handled
     by the SDK

GET endpoints mask **every secret field** (`app_secret` / `encrypt_key`
/ `verification_token`) as `***` (see `routes/channels.py:_mask_secrets`),
so even a leaked admin key can't exfiltrate secrets via the API.

---

## 7. Operations & lifecycle

### 7.1 Receiver supervision (ChannelReceiverSupervisor)

Process-wide singleton indexing every WS receiver by
`(tenant_id, project_id, channel_id)`. Lifecycle hooks:

| Trigger                       | Action                                                              |
|-------------------------------|---------------------------------------------------------------------|
| Server startup                | `attach_session_manager(sm)` so `start_for` can inject messages     |
| `pm.get()` first assembly     | `_assemble` iterates ws bindings → `supervisor.start_for`           |
| `POST /channels` (ws mode)    | After `reg.add`: `supervisor.start_for` immediately                 |
| `DELETE /channels/{id}`       | Before `reg.remove`: `supervisor.stop_for` to avoid race            |
| `pm.invalidate(project)`      | Before dropping cache: `supervisor.stop_project(tid, pid)`          |
| Server shutdown               | After MCP `disconnect_all`: `supervisor.stop_all`                   |

`start_for` is idempotent — same key called twice does stop-then-start,
so editing a binding (e.g. new App Secret) gets a clean restart via
`invalidate`.

### 7.2 The SDK no-stop-API limitation

`lark.ws.Client.start()` has no public `stop()`. Our `stop_receiver`
just sets a `threading.Event`; the SDK exits on its next reconnect loop.
**In production, deleting a binding may leave the WS socket open for
5~10 s; in pathological cases a server restart is required to fully
release it.** `daemon=True` ensures process exit never blocks. See
`feishu_ws.py` docstring.

### 7.3 Do multiple bindings share a WS connection?

No. Two bindings with the same `app_id` open separate connections. The
SDK doesn't expose a multiplexing API; low-volume scenarios (a handful
of bots) are fine. For high-volume, use webhook mode.

### 7.4 Failure handling

| Failure              | Automatic behavior                                                         |
|----------------------|----------------------------------------------------------------------------|
| Network disconnect   | SDK auto-reconnects (exponential backoff)                                  |
| App Secret invalid   | Token refresh 401 → `deliver` skips silently (no retry); inbound still OK  |
| lark-oapi missing    | `start_for` raises RuntimeError, supervisor catches + logs; binding stays  |
| Event loss in flight | SDK only marks ack'd events as done; on no-ack the SDK redelivers          |

---

## 8. Adding a new IM transport

The `channels/` abstraction's goal: one kind = one file + one
`register_channel_kind`. Example for Slack Socket Mode (hypothetical):

```python
# mini_cc/channels/slack.py
from .base import Channel, ChannelBinding, InboundResult, register_channel_kind

class SlackChannel:
    kind = "slack"
    supported_transports = ("socket-mode",)

    def handle_inbound(self, body, headers) -> InboundResult: ...
    def deliver(self, event: dict) -> None: ...
    # Optional: long-connection receiver
    def start_receiver(self, on_inbound) -> None: ...
    def stop_receiver(self) -> None: ...

def _factory(binding: ChannelBinding) -> Channel:
    return SlackChannel(binding)

register_channel_kind("slack", _factory,
                      supported_transports=("socket-mode",))
```

Then in `mini_cc/server/app.py:lifespan`, after `_ensure_feishu_loaded`:

```python
from . import slack  # noqa: F401  triggers register_channel_kind
```

On the UI side, add an entry to `ChannelsPanel.tsx:KIND_CONFIG`.
The full contract lives in `mini_cc/channels/base.py:Channel` protocol
docstring.

---

## 9. Further reading

- Original design plan: `docs/plans/2026-07-07-channels-feishu-design.zh.md`
- WS receiver plan: `docs/plans/2026-07-07-channels-ws-receiver.zh.md`
- Channel protocol source: `mini_cc/channels/base.py`
- Feishu Python SDK docs: <https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/sdk/server-side-sdk/python>
