[ < [07](07-http-server.md) ] [ [09](09-auth.md) > ] · [English version](../en/08-sse-streaming.md)

# 08 — SSE 流与断线恢复

> `/send` 是唯一的流式端点。它把*同步的* `SessionManager.send()` 迭代器
> 包到工作线程里,通过一个有界 `asyncio.Queue` 桥接到 async SSE 响应。
> 本章覆盖线路格式、`Last-Event-Id` 重放协议、B8 "仅恢复"代码路径
> (在不重跑 LLM 的前提下恢复断流),以及支撑这两者的按会话事件日志。

---

## 问题与动机

Agent loop 会发射一串事件:assistant 文本增量、工具调用、工具结果、todo 更新、错误。
HTTP 上传输有两种自然方式:WebSocket 或 Server-Sent Events。mini_cc 选 SSE,有三个具体理由:

1. **Agent loop 是同步且按项目加锁的**。`SessionManager.send()` 是个普通 generator,
   在整个 turn 期间持有项目锁。WebSocket 要么把 loop 重写成 async(影响面巨大),
   要么反正还是要在线程里跑同步 loop——既然如此,SSE 用更简单的客户端就能完成同样的桥接。
2. **单向流量就够**。客户端通过 POST 发一个 user turn,服务端流式回 N 个事件。
   turn 中途没有第二条客户端消息,不足以撑起双向 socket。
3. **HTTP/2 多路复用 + 标准 `Last-Event-Id` 恢复**。浏览器和代理都已经在用 SSE;
   `EventSource` API 自动重连并免费重发 `Last-Event-Id`。

但用 SSE 包同步迭代器会带来四个生产风险:客户端慢时服务端缓冲无限增长、
代理 60 秒空闲断连、客户端中途断开导致事件丢失、重连后"LLM 是不是跑了两遍?"的歧义。
`mini_cc/server/sse.py` 的实现与 `mini_cc/server/routes/sessions.py` 里的 B8 修复就是答案。

---

## 设计与原理

### 同步→异步桥接

`sse_stream`(`mini_cc/server/sse.py:36`)接收事件 dict 的同步迭代器,
产出 SSE 格式的 chunk。一个专用 `ThreadPoolExecutor(max_workers=1)`
排空迭代器,把 item 推到一个有界 `asyncio.Queue(maxsize=512)`。
异步侧用带心跳超时的 `await queue.get()` 消费。

```
   sync_iter(项目加锁,跑在工作线程里)
        │  for ev in iter: _dispatch_sync(queue, loop, ("event", ev))
        ▼
   asyncio.Queue(maxsize=512)        ← 有界;客户端慢 → 拆连接
        │  await asyncio.wait_for(queue.get(), timeout=15s)
        ▼
   yield f"id: {seq}\ndata: {json}\n\n"   ← SSE 线路格式
```

有界队列是承重设计:慢客户端无法让服务端缓冲无限制增长。
当 `queue.put` 会阻塞(队列满)时,工作线程的
`run_coroutine_threadsafe(...).result()` 会阻塞;
异步侧要么在产出心跳要么在产出 chunk;客户端断开会被观察为
`CancelledError`,进而触发 `on_cancel`(调用 `sess.stop()`)。

### 线路格式与分帧

每个事件按(`sse.py:32`)渲染:

```
id: 42
data: {"type":"assistant_message","text":"..."}

```

注意结尾的空行——它分隔 SSE 事件。`id:` 字段是 `EventSource` 捕获的内容,
重连时作为 `Last-Event-Id` 重发。流以一个 sentinel 结束(`sse.py:26`):

```
data: [DONE]

```

这样客户端可以干净退出,无需启发式判断。心跳(`sse.py:29`)是 SSE 注释——
以 `:` 开头的行——客户端会忽略,但代理会视为活跃:

```
: keepalive

```

### `Last-Event-Id` 重放

`/send` 流出的每个事件,在*流出的同时*追加到按会话的 append-only 日志(`sessions.py:223`):

```python
for ev in sm.send(pid, sid, body.user_input):
    try:
        sess.loop.project.storage.append_session_event(pid, sid, ev)
    except Exception:
        pass
    yield ev
```

写入是 best-effort——磁盘失败不会打断实时流。重连时客户端发 `Last-Event-Id: 42`,
路由从磁盘读出 seq 42 *之后*的所有事件(`sessions.py:187` 调用
`storage.read_session_events_since`),作为 `replay=` 传给 `sse_stream`。
桥接先发 replay 事件,*然后*再订阅实时迭代器——这样客户端看到的是
一条有序的、无空缺、无重复的流。

序列连续性在 `sse.py:66` 计算:
`next_seq = int(last_event_id) + 1 if last_event_id is not None else 1`。

### B8 仅恢复路径:不要重跑 LLM

棘手场景:客户端在 100 个事件的 turn 的第 50 个断开。LLM 在服务端继续跑
(工作线程继续排空迭代器);事件 51-100 落到按会话日志里。客户端重连时,
*不能*触发新的 LLM 调用——那会双扣费、双发射,污染对话记录。

`SendMessageRequest.resume`(`schemas.py:31`)就是那个标志。为 true 时,
`/send` 处理器走另一条分支(`sessions.py:196`):

```python
# B8 仅恢复模式:跳过加锁与派发——只从磁盘重放并关闭。
# 不需要项目锁:这是纯读。
if body.resume:
    def _replay_only_iter():
        for ev in replay:
            yield ev
    return StreamingResponse(
        sse_stream(_replay_only_iter(),
                   last_event_id=last_seq or None,
                   replay=None),
        media_type="text/event-stream",
        headers={..., "X-mini_cc-Resume": "1"},
    )
```

`X-mini_cc-Resume: 1` 响应头让客户端确认服务端按"仅恢复"(而非新派发)处理了请求。
如果第二个 `/send`(非 resume)正在持有项目锁,resume 路径照样能跑——它根本不碰锁。

### 并发派发守卫

当 `resume` 为 *false* 且请求一次正常派发时,`sm.try_lock(pid)`(`sessions.py:213`)
为 turn 把关:同一项目内一次进行中时,第二次 send 返回 `409 project_busy`,
而不是排队。SSE 不在项目内多路复用 turn。

### 客户端重连契约

官方客户端在断开时按指数退避重试:**1s → 2s → 4s,最多 3 次**,
之后向用户抛出错误。每次重试带上 `Last-Event-Id: <最后见到的>`
与 `{"user_input": "", "resume": true}`。这与 B8 路径配对:
服务端重放空缺,如果原 turn 仍在跑,客户端会在 replay 之后立即看到新的实时事件。

### 修复历史(见 git log)

- `3b27fb7` HTTP/SSE 传输层(P3)——初始 `/send`。
- `d1ad886` `fix(round2/batch4): SSE queue bound + heartbeat + Last-Event-Id replay`
  ——加入 512 上限队列、15 秒心跳、replay 管线。
- `aaa5793` `feat(sse): B8 resume-only path + client auto-reconnect on drop`
  ——加入 `resume` 标志、仅恢复分支、`X-mini_cc-Resume` 头、客户端退避/重试循环。

---

## 操作与配置

### `/send` 契约

| 元素 | 值 | 来源 |
|---|---|---|
| 方法与路径 | `POST /tenants/{tid}/projects/{pid}/sessions/{sid}/send` | `sessions.py:157` |
| 鉴权与 scope | `sessions:write`(同时消费一个限流令牌) | `sessions.py:161` |
| 请求体 | `{"user_input": "...", "resume": false, "model": null}` | `schemas.py:25` |
| 请求头 | `Last-Event-Id: <int>`(可选,用于重放) | `sessions.py:162` |
| 响应 | `text/event-stream` | `sessions.py:245` |
| 响应头 | `X-Accel-Buffering: no`(禁用 nginx 缓冲) | `sessions.py:253` |
| 响应头 | `X-mini_cc-Resume: 1`(仅在 `resume=true` 时) | `sessions.py:208` |
| 忙时响应 | `409 {"code":"project_busy"}` | `sessions.py:214` |

### SSE 调优常量

| 常量 | 默认值 | 含义 | 来源 |
|---|---|---|---|
| `DEFAULT_MAXSIZE` | `512` | 每条流的队列容量 | `sse.py:27` |
| `HEARTBEAT_SECONDS` | `15.0` | 发 keepalive 注释前的空闲间隔 | `sse.py:28` |
| `HEARTBEAT_COMMENT` | `: keepalive\n\n` | SSE 注释帧 | `sse.py:29` |
| `_DONE_SENTINEL` | `data: [DONE]\n\n` | 流结束标记 | `sse.py:26` |

这些目前未暴露为环境变量;可通过修改 `sse.py`,或调用 `sse_stream` 时
传入 `maxsize=` / `heartbeat_seconds=`(函数签名已支持)覆盖。

### 代理 / nginx 备注

- 禁用缓冲:路由会发 `X-Accel-Buffering: no`,但如果你的 nginx 配置覆盖了它,
  请在 `/send` location 上额外设 `proxy_buffering off`。
- 空闲超时:把 `proxy_read_timeout` 调到 60s 以上。15 秒心跳会重置 nginx 的空闲计时,
  但显式配置更安全。
- HTTP/1.1:确保 `proxy_http_version 1.1`——HTTP/1.0 上的 SSE 会被缓冲。

---

## 验证步骤

```bash
# 1. 流式基线:发一个 turn,观察 id:/data: 分帧。
curl -N -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"user_input":"say hi in one word"}' \
  http://127.0.0.1:8002/tenants/tA/projects/p1/sessions/s1/send
# id: 1
# data: {"type":"..."}
#
# ...
# data: [DONE]
#

# 2. 心跳:打开一个事件产出很慢的流;空闲 15 秒内会看到
#    ": keepalive" 行维持连接。

# 3. 重放:记下步骤 1 最后的 id,带着它重连。
curl -N -H "Authorization: Bearer $KEY" \
  -H 'Last-Event-Id: 3' \
  -H 'Content-Type: application/json' \
  -d '{"user_input":"more"}' \
  http://127.0.0.1:8002/tenants/tA/projects/p1/sessions/s1/send
# 服务端从 seq 4 开始发,然后接实时流。

# 4. 仅恢复:中途断开,带 resume=true 重连。
curl -N -H "Authorization: Bearer $KEY" \
  -H 'Last-Event-Id: 50' \
  -H 'Content-Type: application/json' \
  -d '{"user_input":"","resume":true}' \
  http://127.0.0.1:8002/tenants/tA/projects/p1/sessions/s1/send \
  | head -c 200
# 响应包含头:X-mini_cc-Resume: 1
# body:仅错过的 51..N 事件,然后 [DONE]。没有新的 LLM 运行。

# 5. 并发守卫:对同一项目并发两次 send。
( curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $K" \
   -d '{"user_input":"long..."}' $URL/send & \
  curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $K" \
   -d '{"user_input":"second"}' $URL/send & wait )
# 一个返回 200 流式,另一个返回 409 project_busy。

# 6. 取消传播:开启一个流,中途杀客户端。
#    服务端的 on_cancel → sess.stop() 应被触发;查服务端日志,
#    确认 loop 已 teardown(没有卡死的 Anthropic 客户端线程)。
```

---

## 常见坑与调试

1. **"客户端重连后 LLM 又跑了一遍"**。重连请求体里忘了 `resume: true`。
   不带这个标志,服务端会按新派发处理,如果原 turn 还在持有项目锁,会返回 409。
2. **"重连后事件重复"**。你的 `Last-Event-Id` 已过期或为零。服务端从
   `int(last_event_id)+1` 开始重放;如果你发 `Last-Event-Id: 0`(或省略),
   你会拿到从 seq 1 起的*所有*事件,包括客户端已渲染过的。务必在客户端
   持久化已成功渲染的最大 id。
3. **"流恰好在 60 秒卡住"**。是某个代理(nginx、Cloudflare、ALB)在切空闲连接。
   15 秒心跳本应阻止这种情况,但如果代理激进缓冲,注释永远到不了它。
   请确认 `X-Accel-Buffering: no` 被遵守,或调高代理的空闲超时。
4. **"队列满 → 静默断连"**。当 512 上限的队列满(消费者慢)时,桥接会拆连接。
   症状:客户端看到流干净结束,但没有 `[DONE]`。诊断方法是查服务端日志里
   `CancelledError` 路径,确认 `on_cancel` 触发;根因永远是客户端排空不过来。
5. **"resume 对一个会话有效,对另一个 404"**。resume 路径仍会调用 `_ensure_warm`
   (`sessions.py:172`);如果会话既不在内存也不在磁盘,即使请求是 resume-only
   也会 404。按会话事件日志在项目下磁盘上——如果项目本身被删了,事件也就没了。

---

## 延伸阅读

- 源码:`mini_cc/server/sse.py`、`mini_cc/server/routes/sessions.py`、
  `mini_cc/server/schemas.py`,以及项目存储里的
  `append_session_event` / `read_session_events_since`(`mini_cc/projects/`)。
- Git:`aaa5793 feat(sse): B8 resume-only path + client auto-reconnect on drop`、
  `d1ad886 fix(round2/batch4): SSE queue bound + heartbeat + Last-Event-Id replay`。
- 姊妹篇:[07 — HTTP 服务与路由](07-http-server.md)、
  [09 — 认证、作用域与分享令牌](09-auth.md)
