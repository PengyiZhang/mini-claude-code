# Feishu（飞书）Webhook 兼容性分析

> **2026-07-07 更新：原生支持已上线。**
> 新增的 `mini_cc/channels/` 子系统抽象出**双向 channel** 协议，第一步就接入了
> 飞书。下文「不支持」结论针对的是 **workflow_v2 的 `webhook_wait` 步骤**（它
> 本身仍然是平台无关的通用入站接收器，不适合直接接飞书事件订阅），但飞书的
> 双向 IM 通信现在走 `channels/feishu.py` + `/channels/{channel_id}/webhook`
> 这条独立路径。详见
> [`docs/plans/2026-07-07-channels-feishu-design.zh.md`](../plans/2026-07-07-channels-feishu-design.zh.md)
> 与 `mini_cc/ARCH.zh.md` §5.10。
>
> 下文保留原 2026-06-29 分析作为历史背景，不再反映当前系统状态。

---

> 结论先行：mini_cc 当前的 `webhook_wait` 步骤 **不原生支持飞书 webhook**。
> 本文记录 2026-06-29 的代码核对结论与接入路径，不动代码。

## 现状 — mini_cc 的 webhook_wait 是通用契约

入站接口（`mini_cc/server/routes/workflow_v2.py:401-437` + `mini_cc/workflow_v2.py:568+`）：

- URL：`POST /tenants/{tid}/projects/{pid}/workflow-runs/{run_id}/webhook/{step_id}?webhook_id=<secret>`
- 鉴权：query 参数 `webhook_id`，与 `step.config.webhook_id` 做相等匹配（共享密钥模型）
- Body：`WebhookWaitIn { event?: str, data: dict }`，自由格式
- 事件过滤：可选 `step.config.event_filter`，匹配顶层 `payload.event`

全仓库搜索 `feishu / lark / x-lark / url_verification / challenge / signature verif`
—— **零命中**。`mini_cc/tools/` 目录下也没有 `integrations/` 或 `feishu.py`。
该步骤是平台无关的"通用入站 webhook 接收器"。

## 与飞书的根本性架构错配

| 维度 | mini_cc webhook_wait | 飞书事件订阅 | 是否兼容 |
|------|---------------------|--------------|---------|
| URL 粒度 | 每个 run 一个独立 URL（路径含 `run_id`） | 每个 app 一个固定 URL | ✗ 飞书没法为每次运行换 URL |
| URL 验证 | 不响应 `url_verification` challenge | 注册时必发 `{challenge, token, type:"url_verification"}`，必须原样回 `challenge` | ✗ 注册阶段就过不了 |
| 签名校验 | 仅 `webhook_id` 共享密钥 | `X-Lark-Signature` (HMAC-SHA256) + `X-Lark-Request-Timestamp` + `X-Lark-Request-Nonce` | ✗ 无 HMAC 校验代码 |
| 事件字段 | 顶层 `payload.event` | `header.event_type` 嵌套 + `header.token` | ⚠ `event_filter` 匹配不到飞书事件 |
| 加密 | 不支持 | 可选 AES 事件加密（`encrypt` 字段） | ✗ 无解密代码 |
| 鉴权模型 | 共享密钥在 query 里 | 签名 + 时间戳防重放 | ✗ 安全模型不同 |

## 出方向（mini_cc → 飞书机器人）

也没有专用工具。`mini_cc/tools/` 下无 `feishu.py` 或 `integrations/`。要发消息到飞书群机器人
（`https://open.feishu.cn/open-apis/bot/v2/hook/<id>`），目前只能走 `action` 步骤让 LLM
通过 `tools/web.py`（HTTP 工具）POST —— 间接、且依赖 LLM 自觉调用，不保证稳定。

## 接入飞书的两种现实路径

### 1. 适配器网关（推荐 — 改动最小）

写一个独立小服务（一个 FastAPI 文件即可）放在 mini_cc 前面：

- 接收飞书事件，处理 `url_verification` challenge（原样回 `challenge`）
- 校验 `X-Lark-Signature`（HMAC-SHA256，密钥 = 飞书 app secret）
- 路由 fan-out：根据 `header.event_type` 或自定义映射，找到对应 paused 的 mini_cc run，
  转发到 `.../workflow-runs/{run_id}/webhook/{step_id}?webhook_id=<mini_cc 共享密钥>`
- 这层就是"飞书侧 1 个 URL → mini_cc 侧 N 个 run URL"的分发器
- 缺点：需要一个外部组件维护"事件 → run_id"的路由表（可以是 Redis/SQLite，
  或简单做"广播给所有 paused 的 webhook_wait run，由 payload 内容决定匹配"）

### 2. mini_cc 原生集成（改动较大）

在 `mini_cc/integrations/feishu.py` 里加一个原生模块：

- 新增固定端点 `/integrations/feishu/event`，处理 challenge + 签名校验
- 把飞书事件入队（持久化或内存 queue）
- 扩展 `webhook_wait` 步骤语义：允许 `step.config.feishu_event_type` 作为触发条件，
  事件路由器消费队列并把匹配的事件推进对应 paused 步骤
- 这会引入"event queue → 匹配 paused 步骤"的新分发模型，目前 mini_cc 的 webhook 模型
  是"每个 run 自己暴露一个 URL 等 pull"，与"app 级 push"模型根本不同

## 一句话结论

**workflow_v2 的 `webhook_wait` 不支持接飞书事件订阅**（URL 粒度、URL 验证
challenge、HMAC 签名三处全部不匹配）。但**飞书双向 IM 通信**已经在
2026-07-07 通过独立的 `channels/` 子系统支持 —— 不走 workflow_v2，而是
`/channels/{channel_id}/webhook` 公开端点 + `channels/feishu.py` 实现。
原分析中提到的「适配器网关」方案已经不需要；如果要在 workflow 里把飞书
消息当 webhook_wait 触发条件，仍然需要一个适配器把飞书事件转成
`webhook_wait` 的 `{event, data}` 形状。

## 参考

- 入站路由：`mini_cc/server/routes/workflow_v2.py` 的 `resolve_webhook_wait` (line 401)
- 服务层解析：`mini_cc/workflow_v2.py` 的 `WorkflowService.resolve_webhook_wait` (line 568)
- 鉴权降级路径：`_service_for_unauth` (line 440) —— 共享密钥代替租户 bearer 的设计
- 飞书事件订阅文档：https://open.feishu.cn/document/server-docs/event-subscription-guide/overview
