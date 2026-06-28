# mini_cc 运维 Runbook

> 这份文档面向**已经把 mini_cc 部署到生产**的运维同学,聚焦 day-2 操作:
> 排错、备份恢复、密钥管理、监控告警、事故响应。首次安装部署看
> [`mini_cc/DEPLOYMENT.md`](../../mini_cc/DEPLOYMENT.md);功能细节看
> [`2026-06-28-functional-features.zh.md`](./2026-06-28-functional-features.zh.md)。

---

## 目录

- [一、健康检查与日常巡检](#一健康检查与日常巡检)
- [二、日志与可观测性](#二日志与可观测性)
- [三、密钥管理](#三密钥管理)
- [四、备份与恢复](#四备份与恢复)
- [五、容器沙箱运维](#五容器沙箱运维)
- [六、事故响应(SRE 手册)](#六事故响应sre-手册)
- [七、升级流程](#七升级流程)
- [八、容量规划](#八容量规划)
- [九、安全清单](#九安全清单)

---

## 一、健康检查与日常巡检

### 1.1 存活探针

```bash
# 进程存活 + 路由可达
curl -fsS http://<host>:8000/health | jq .
# 期望:{"ok": true}
```

`/health` 是唯一无鉴权的 GET 端点,适合放在 LB / k8s livenessProbe 上。
建议 5 秒一次,连续 3 次失败才标记不健康,避免误杀。

### 1.2 就绪探针(深度)

`/health` 只证明进程在跑。要确认**租户可以发请求**,带 key 探一下只读端点:

```bash
curl -fsS -H "Authorization: Bearer $HEALTH_KEY" \
     http://<host>:8000/tenants/$HEALTH_TENANT/projects | jq length
# 期望:数字(可能是 0)
```

建议专门建一个 `health` 租户和一把 `read:*` scope 的 key,只用于 readiness。

### 1.3 Metrics 三件套

```bash
curl -s http://<host>:8000/metrics           | head -20   # Prometheus 文本
curl -s http://<host>:8000/metrics.json      | jq .       # JSON 快照
curl -s -H "Authorization: Bearer $ADMIN_KEY" \
     http://<host>:8000/tenants/$TID/admin/metrics.json    # 租户视角
```

**核心指标 + 推荐告警阈值**(Prometheus 规则):

```yaml
groups:
  - name: mini_cc
    rules:
      - alert: MiniCcHighErrorRate
        # 5xx 占比 5 分钟内超过 5%
        expr: |
          sum(rate(http_requests_total{job="mini_cc",status=~"5.."}[5m]))
          /
          sum(rate(http_requests_total{job="mini_cc"}[5m])) > 0.05
        for: 5m
        annotations:
          summary: "mini_cc 5xx 比例 > 5%"

      - alert: MiniCcAnthropicFailures
        # 1 分钟内 Anthropic 调用失败 10 次以上
        expr: |
          increase(anthropic_request_total{status="error"}[1m]) > 10
        for: 1m
        annotations:
          summary: "Anthropic API 错误率高,检查 key/base_url/限额"

      - alert: MiniCcProjectBusy
        # 同一项目持续被占用(并发 send 排队)
        expr: |
          increase(http_requests_total{status="409"}[5m]) > 20
        for: 5m
        annotations:
          summary: "多个 send 排队,某些项目繁忙"

      - alert: MiniCcTokenBudgetBurning
        # 5 分钟内输入 token 烧过 2M
        expr: |
          increase(anthropic_tokens_total{kind="input"}[5m]) > 2e6
        annotations:
          summary: "Token 消耗异常"
```

### 1.4 进程基础指标

mini_cc 不自带 process exporter,建议用 node_exporter 或 systemd 收集:

- CPU:大模型流式期间 CPU 不应饱和(SDK 是 IO-bound),饱和到 80%+ 说明有别的问题。
- RSS:AgentLoop + 缓存的 Project 对象。**单租户的 RSS 长期 > 1GB 是异常**,
  考虑重启或加 `MINI_CC_RATE_LIMIT_RPM`。
- FD:Linux 下 `cat /proc/$(pidof mini_cc)/status | grep FDSize`。MCP 子进程、
  后台任务、文件流都会占 FD。> 1000 时检查 `lsof -p $(pidof mini_cc) | wc -l`。

### 1.5 磁盘水位

```bash
du -sh $MINI_CC_DATA_DIR/*/*/{messages,todos,transcripts,tool_results} 2>/dev/null \
  | sort -h | tail -20
```

热点路径:

- `<data_dir>/<tid>/projects/<pid>/messages/<sid>.json` —— 每条会话的完整历史。
- `.../transcripts/transcript_<ts>.jsonl` —— 压缩前快照,体积最大,**可以批量删**。
- `.../tool_results/<tool_use_id>.txt` —— 大型 bash 输出可能上 MB。

清理脚本(保留 30 天):

```bash
find $MINI_CC_DATA_DIR -name 'transcript_*.jsonl' -mtime +30 -delete
find $MINI_CC_DATA_DIR -name '*.txt' -path '*/tool_results/*' -mtime +30 -delete
```

---

## 二、日志与可观测性

### 2.1 日志格式

```bash
MINI_CC_LOG_FORMAT=json      # 默认;机器友好
MINI_CC_LOG_FORMAT=text      # 人读;调试用
MINI_CC_LOG_LEVEL=INFO       # root level
```

JSON 行字段:`ts / level / event / tenant / project / session / msg / ...`。
推荐 Loki / ELK 查询:

```logql
{job="mini_cc"} |= "ERROR" | json | level >= "ERROR"
{job="mini_cc"} | json | event = "anthropic_request" | status = "error"
```

### 2.2 关键事件日志

| event                  | 触发时机                            | 含义                                          |
| ---------------------- | ----------------------------------- | --------------------------------------------- |
| `anthropic_request`    | 每次 Anthropic stream 结束          | 含 token usage、status(success/error/cancel) |
| `http_request`         | 每个 HTTP 请求结束                  | method / route_template / status / dur_ms     |
| `permission_request`   | 工具调用进入交互式权限闸门          | 等待用户决定                                  |
| `cron_fired`           | 定时任务触发                        | job_id + 注入的 prompt                        |
| `bg_notification`      | 后台任务完成、通知下一轮            | bg_id                                          |
| `span.end`             | 每个 log_span 退出                  | dur_ms + 调用方字段                           |

### 2.3 分布式追踪

```bash
MINI_CC_OTEL_EXPORTER=otlp                      # jaeger / console 也可
MINI_CC_OTEL_ENDPOINT=http://otel-collector:4317
MINI_CC_OTEL_SERVICE_NAME=mini-cc-prod
```

不设 `OTEL_EXPORTER` 时退化为**日志 span**(零额外依赖)。生产建议接 OTLP,
trace_id 会出现在日志和响应头 `X-Trace-Id` 上,便于跨服务串联。

### 2.4 启动期关注

服务启动时,关注以下日志:

- `serving web dist from <path>` —— 前端挂载成功;不出现说明 `dist/` 探测失败。
- `metrics enabled` / `metrics disabled` —— MetricsMiddleware 状态。
- `sandbox backend: container|subprocess` —— 沙箱后端选择;如果设了
  `MINI_CC_SANDBOX_DEFAULT=container` 但显示 subprocess,说明 Docker 不可用,
  走了降级路径。
- `using default share secret` —— F7.1 / F7.2 的密钥未配置,**生产必须修掉**。

---

## 三、密钥管理

mini_cc 涉及三类密钥,优先级和轮换策略各不相同。

### 3.1 租户 API key(`mck_*`)

存储在 `<MINI_CC_DATA_DIR>/keys.json`,rename-on-write 原子写。

```bash
# 列出某租户所有 key
python -m mini_cc.server keys list <tenant>

# 新建带限制的 key
python -m mini_cc.server keygen <tenant> \
    --scopes 'sessions:write,files:read' \
    --expires-in 7d \
    --label "ci-pipeline"

# 轮换:旧 key 还能用 24 小时,新 key 立刻生效
python -m mini_cc.server keys rotate <old_key> --grace-hours 24

# 立刻吊销
python -m mini_cc.server revoke <key>
```

**轮换流程(标准操作)**:

1. `rotate` 拿到新 key + 老 key 的过期时间。
2. 把新 key 部署到调用方(CI、客户端应用)。
3. 等 grace period 过去,老 key 自动失效。
4. 监控 401 错误,确认没有遗漏的调用方。

### 3.2 Share token / Webhook HMAC 密钥(F7)

这是**全局密钥**(不是 per-tenant),用来签 share token 和 webhook payload。

```bash
# 强烈建议用 openssl 生成
openssl rand -hex 32 > /etc/mini_cc/share_secret
chmod 600 /etc/mini_cc/share_secret
```

```ini
# /etc/systemd/system/mini-cc.service 或 .env
MINI_CC_SHARE_SECRET_FILE=/etc/mini_cc/share_secret
# 或直接放环境变量(适合 docker run -e)
MINI_CC_SHARE_SECRET=<64-hex>
```

> ⚠️ **代码当前只读 `MINI_CC_SHARE_SECRET` 环境变量**,不读 `_FILE` 后缀。
> 容器场景请用 docker secret / k8s secret 把值注入环境变量,而不是文件路径。

**轮换流程**:

```ini
# 旧密钥仍然在前,新密钥排第二
MINI_CC_SHARE_SECRETS=<new-secret>,<old-secret>
```

```bash
systemctl restart mini-cc
# 等 7 天(默认 share token TTL),所有老 token 过期
# 然后改成单值
MINI_CC_SHARE_SECRET=<new-secret>
systemctl restart mini-cc
```

**泄露应急**:见 [§6.4](#64-密钥泄露)。

### 3.3 LLM provider key(`ANTHROPIC_API_KEY` / `LITELLM_API_KEY`)

放在 `.env` 或 systemd `EnvironmentFile`,**不要进 git**。
建议用云厂商的 secret manager(Vault / AWS Secrets Manager / k8s Secret)拉取。

Provider 限速故障会在日志里出现 `429` / `529`,见 [§6.2](#62-llm-provider-限速或挂掉)。

---

## 四、备份与恢复

### 4.1 备份范围

`$MINI_CC_DATA_DIR` 一个目录包含所有状态,**备份它就够了**:

```
<MINI_CC_DATA_DIR>/
├── keys.json               ← 租户 API key(关键!)
├── tenants/
│   └── <tid>/
│       ├── sandbox.toml    ← 容器沙箱配置(可选)
│       └── projects/
│           └── <pid>/
│               ├── meta.json
│               ├── messages/     ← 会话历史
│               ├── todos/
│               ├── tasks/
│               ├── sessions/index.json
│               ├── transcripts/  ← 可选,体积大
│               ├── tool_results/
│               ├── memory/MEMORY.md
│               ├── cron/jobs.json
│               ├── workflows/
│               └── webhooks.json  ← F7.2 订阅
└── ...
```

### 4.2 备份策略

**热备份**(服务运行中):

```bash
# rsync 增量,跑两遍确保一致
rsync -a --delete $MINI_CC_DATA_DIR/ /backup/mini_cc/
rsync -a --delete $MINI_CC_DATA_DIR/ /backup/mini_cc/

# 打 tarball(可选,利于异地保存)
tar czf /backup/mini_cc-$(date +%F).tar.gz -C /backup mini_cc
```

跑两遍的原因:AgentLoop 写文件是 rename-on-write,中途 rsync 可能抓到中间态,
第二遍就能拿到最终态。

**冷备份**(停服务):

```bash
systemctl stop mini-cc
tar czf /backup/mini_cc-cold-$(date +%F).tar.gz -C / mini_cc_data
systemctl start mini-cc
```

建议每周一次冷备,每天一次热备;transcripts 单独每周归档。

### 4.3 恢复流程

1. **停服务**:`systemctl stop mini-cc`
2. **替换数据目录**:
   ```bash
   mv $MINI_CC_DATA_DIR ${MINI_CC_DATA_DIR}.broken
   mkdir $MINI_CC_DATA_DIR
   tar xzf /backup/mini_cc-2026-06-27.tar.gz -C $MINI_CC_DATA_DIR --strip-components=1
   ```
3. **检查权限**:`chown -R mini-cc:mini-cc $MINI_CC_DATA_DIR`
4. **启动**:`systemctl start mini-cc`
5. **冒烟测试**:`curl /health` + 用一个真实租户 key 拉一次 sessions。

### 4.4 单会话级别恢复

只想恢复某个被误删的 session,把对应文件放回去:

```bash
# 从备份里捞特定 session
tar xzf /backup/mini_cc-2026-06-27.tar.gz \
    mini_cc_data/tenants/<tid>/projects/<pid>/messages/<sid>.json
mv mini_cc_data/tenants/<tid>/projects/<pid>/messages/<sid>.json \
   $MINI_CC_DATA_DIR/tenants/<tid>/projects/<pid>/messages/

# 同步 sessions index,触发重建:
rm $MINI_CC_DATA_DIR/tenants/<tid>/projects/<pid>/sessions/index.json
# 下次 GET /sessions 会从 messages/*.json 重建索引
```

---

## 五、容器沙箱运维

仅当开了 `MINI_CC_SANDBOX_DEFAULT=container`(P5)时适用。

### 5.1 日常检查

```bash
# 列出所有租户的容器
python -m mini_cc.server sandbox status

# 单个租户
python -m mini_cc.server sandbox status --tid <tid>

# 从 docker 侧看
docker ps --filter "name=mini_cc-"
docker stats --no-stream $(docker ps -q --filter "name=mini_cc-")
```

**关注**:

- 容器状态:`Up` 是正常,`Exited` / `Restarting` 需要进一步排查。
- 单租户容器 CPU 长期满载说明用户跑了高负载任务,考虑设 `cpu_quota`。
- 磁盘:容器层 + bind-mount 的项目目录都在主机上,小心 inode 耗尽。

### 5.2 容器降级事件

服务启动时如果 `docker info` 失败,会**静默回落到 subprocess 沙箱**并记一条
`DegradeEvent`。查询:

```bash
curl -s http://<host>:8000/metrics.json | jq '.degrades'
```

```json
[
  {"event": "container_unavailable", "reason": "docker daemon not running",
   "ts": "2026-06-27T10:23:11Z"}
]
```

出现降级时:

1. 检查 docker daemon:`systemctl status docker`
2. 检查 WSL2 backend(Windows 主机)
3. 修好 docker 后,**需要重启 mini_cc** 才会重新探测。

### 5.3 镜像管理

```bash
# 看现有镜像
docker images | grep mini_cc-sandbox

# 重建基础镜像
python -m mini_cc.server sandbox build-image

# 用自定义 Dockerfile
python -m mini_cc.server sandbox build-image \
    --dockerfile ./sandbox/Dockerfile.custom \
    --tag my-registry/sandbox:v2

# 推到私有 registry 后,改 tenants/<tid>/sandbox.toml 里的 image_tag
```

**升级流程**:

1. 构建新镜像,推到 registry。
2. 改 `sandbox.toml` 的 `image_tag`(冷加载,需要重启服务)。
3. 停掉所有运行中的容器:`python -m mini_cc.server sandbox stop <tid>`。
4. 重启 mini_cc。下次该租户的 `execute()` 会自动起用新镜像。

### 5.4 单租户隔离失效

如果怀疑某租户的容器被攻破(例如跑出了 `sandbox` namespace):

```bash
# 立刻停掉该租户容器
python -m mini_cc.server sandbox stop <tid>

# 锁掉该租户的所有 key
python -m mini_cc.server keys list <tid> | awk '{print $2}' | \
  xargs -I{} python -m mini_cc.server revoke {}
```

然后从 docker logs / 审计日志复现攻击路径,详见 [§6.5](#65-容器逃逸疑似)。

---

## 六、事故响应(SRE 手册)

每个事故按「现象 → 快速诊断 → 缓解 → 根因」四步走。

### 6.1 全站 500 / 服务不响应

**现象**:`/health` 慢或不回,大量 5xx。

**快速诊断**:

```bash
# 进程在不在
systemctl status mini-cc

# FD 数 / 内存
ps -o pid,rss,fname -p $(pidof mini_cc)
ls /proc/$(pidof mini_cc)/fd | wc -l

# 当前在干什么
py-spy dump --pid $(pidof mini_cc)
# 或 strace:
strace -p $(pidof mini_cc) -f -e trace=network,read,write 2>&1 | head -50
```

**缓解**:

- FD 暴涨 → 重启服务,排查 MCP server / 后台任务。
- 内存涨 → 重启,检查是不是有死循环(查 transcript 日志的最近 prompt)。
- 完全死锁 → `systemctl restart mini-cc`(用户的当前 SSE 流会断,但磁盘上的
  状态保留,下次 send 自动 warm)。

### 6.2 LLM provider 限速或挂掉

**现象**:聊天返回错误,日志有大量 `status="error"`。

```bash
# 看 Anthropic 错误细分
curl -s http://<host>:8000/metrics.json | \
  jq '.counters.anthropic_request_total.series'
```

**分类处置**:

| 错误       | 含义                          | 操作                                          |
| ---------- | ----------------------------- | --------------------------------------------- |
| `429`      | 限速                          | 降配额、加 retry、检查有没有循环调用          |
| `529`      | provider 过载                 | 自动会触发 fallback,确认 `FALLBACK_MODEL_ID` |
| `401/403`  | key 失效                      | 换 key、检查 base_url                         |
| `timeout`  | 网络问题                      | 检查出口网络、provider 状态页                |

**应急**:把模型切到本地(LiteLLM + Ollama)或备用 provider:

```ini
MODEL_ID=openai/qwen2.5:14b
LITELLM_BASE_URL=http://internal-llm:11434/v1
LITELLM_API_KEY=anything
```

### 6.3 数据目录满了

**现象**:写文件失败、新建项目 500。

```bash
df -h $MINI_CC_DATA_DIR
du -sh $MINI_CC_DATA_DIR/*/*/transcripts 2>/dev/null
```

**紧急清理**:

```bash
# 7 天前的压缩前快照
find $MINI_CC_DATA_DIR -name 'transcript_*.jsonl' -mtime +7 -delete

# 30 天前的大块 tool 输出
find $MINI_CC_DATA_DIR -name '*.txt' -path '*/tool_results/*' \
     -size +1M -mtime +30 -delete
```

**长期**:加 cron 自动清理,见 [§1.5](#15-磁盘水位)。

### 6.4 密钥泄露

#### API key 泄露

```bash
# 立刻吊销
python -m mini_cc.server revoke <leaked_key>

# 给受影响租户签新 key
python -m mini_cc.server keygen <tenant> --scopes '*' --label "post-leak-$(date +%F)"
```

吊销是即时的,不需要重启服务。

#### Share secret 泄露

更严重 —— 攻击者能伪造任意 session 的 share token。

```bash
# 1. 立刻换密钥(单值,不轮换)
NEW_SECRET=$(openssl rand -hex 32)
echo "MINI_CC_SHARE_SECRET=$NEW_SECRET" >> /etc/mini_cc/env

# 2. 重启服务,所有老 token 立刻失效
systemctl restart mini-cc

# 3. 通知用户:之前发出去的 share link 全部失效,需要重新签发
```

#### Webhook secret 泄露

攻击者能伪造 webhook payload 注入到接收方。

```bash
NEW_SECRET=$(openssl rand -hex 32)
# 改 MINI_CC_WEBHOOK_SECRET 或 MINI_CC_SHARE_SECRET
systemctl restart mini-cc

# 通知所有 webhook 接收方更新校验逻辑(虽然他们用的是同一份 secret)
```

### 6.5 容器逃逸疑似

**现象**:容器里的进程写出了 `/workspaces/` 之外的文件,或拿到了宿主机的
docker socket。

**紧急处置**:

```bash
# 1. 停所有容器
for tid in $(ls $MINI_CC_DATA_DIR/tenants); do
    python -m mini_cc.server sandbox stop $tid
done

# 2. 切回 subprocess 沙箱(临时)
export MINI_CC_SANDBOX_DEFAULT=subprocess
systemctl restart mini-cc

# 3. 保存证据
docker logs <suspected-container> > /tmp/forensic.log
docker export <suspected-container> | gzip > /tmp/forensic.tar.gz
```

**根因**:通常是 Dockerfile 装了不该装的东西、bind-mount 范围太大、或租户
的 `sandbox.toml` 配了危险选项。审计所有 `tenants/*/sandbox.toml` 的
`extra_mounts` 和 `apt_packages`。

### 6.6 某租户被滥用

```bash
# 1. 锁 key
python -m mini_cc.server keys list <tid> | awk '{print $2}' | \
  xargs -I{} python -m mini_cc.server revoke {}

# 2. 设限流为 0(比删租户温和,可恢复)
echo "MINI_CC_RATE_LIMIT_RPM=<tid>=0" >> /etc/mini_cc/env
systemctl restart mini-cc

# 3. 导出该租户近期活动,做审计
grep "\"tenant\":\"<tid>\"" /var/log/mini_cc/*.jsonl > /tmp/audit-<tid>.log
```

---

## 七、升级流程

### 7.1 标准滚动升级

```bash
# 1. 备份(永远第一步,见 §4.2)
systemctl stop mini-cc
tar czf /backup/pre-upgrade-$(date +%F).tar.gz $MINI_CC_DATA_DIR

# 2. 拉新代码
cd /opt/mini-cc
git fetch && git checkout <new-tag>

# 3. 装依赖(如果 requirements.txt 变了)
pip install -r requirements.txt

# 4. 跑数据库/状态迁移(如果有,看 release note)

# 5. 重建前端(如果 web/ 变了)
cd mini_cc/web && npm install && npm run build && cd ../..

# 6. 起服务
systemctl start mini-cc

# 7. 冒烟
curl /health
journalctl -u mini-cc -n 50 --no-pager
```

### 7.2 回滚

```bash
systemctl stop mini-cc
git checkout <old-tag>
cd mini_cc/web && npm run build && cd ../..    # 如果回滚跨了前端版本
tar xzf /backup/pre-upgrade-<date>.tar.gz -C /  # 数据也回滚
systemctl start mini-cc
```

> ⚠️ **数据回滚要谨慎**:如果升级期间用户产生了新会话,回滚数据会丢失这些会话。
> 优先**只回滚代码**,数据保留。仅当数据格式不兼容时才回滚数据。

### 7.3 零停机升级(单实例做不到)

mini_cc 当前**不支持多实例**(in-memory state 没共享)。要零停机:

1. 在 LB 后挂新旧两个实例。
2. 把新实例权重设为 0,启动,healthcheck 通过。
3. 慢慢把权重切到新实例。
4. 旧实例 drain(等当前 SSE 流结束)后下线。

注意:`SessionManager._sessions` 是进程内存,跨实例不共享。SSE 流期间用户
切到另一个实例会触发冷 warm(从磁盘重建),不会丢消息但会有短暂延迟。

---

## 八、容量规划

### 8.1 单实例容量参考

测试基线(单租户、DeepSeek backend、单 session 流式):

| 资源      | 1 并发  | 10 并发  | 50 并发  |
| --------- | ------- | -------- | -------- |
| CPU       | < 10%   | 30-50%   | 接近饱和 |
| RSS       | ~200 MB | ~500 MB  | 1-2 GB   |
| 文件描述符 | ~50     | ~300     | ~1500    |
| 出口带宽  | ~50 KB/s| ~500 KB/s| 2-5 MB/s |

**瓶颈**:

- **CPU**:SSE 序列化 + JSON 解析。50 并发以上考虑多实例 + LB。
- **RSS**:每个 warm session 持有 messages list,大对话(> 100k tokens)很吃内存。
- **FD**:每个 MCP server 子进程 + 后台任务占 3-5 个 FD。Linux 默认 1024 上限
  很快就到,生产建议 `ulimit -n 65536`。

### 8.2 磁盘

经验值(单 session 平均):

- messages JSON:5-50 KB/100 条消息。
- tool_results:取决于 bash 输出,**没有上限**,建议每个 < 1 MB。
- transcripts:每条会话压缩前快照 ≈ messages 大小,但保留多次。

每租户每月(中度使用)≈ 100 MB - 1 GB。

### 8.3 LLM 配额

监控 `anthropic_tokens_total`,按租户分组:

```logql
sum by (tenant) (
  increase(anthropic_tokens_total{kind=~"input|output"}[1h])
)
```

提前给租户设 `MINI_CC_RATE_LIMIT_RPM`,避免一个租户烧光配额。

---

## 九、安全清单

### 9.1 上线前必查

- [ ] **MINI_CC_HOST** 是 `0.0.0.0` 或 LB 内网 IP,不是 `127.0.0.1`(外访问不到)。
- [ ] **HTTPS** 在 nginx / Caddy / LB 层终结,mini_cc 自身只跑 HTTP。
- [ ] **CORS** 同源部署留空;分离部署明确列出域名,**不要保留默认 `*`**。
- [ ] **MINI_CC_SHARE_SECRET** 已设,且 `warn_if_default_secret()` 在启动日志里
      没报警。
- [ ] **MINI_CC_DATA_DIR** 指向持久化卷,权限 `0700`,owner 是 mini_cc 用户。
- [ ] **keys.json** 在备份里,但**不**在 git 仓库里。
- [ ] **沙箱**:`MINI_CC_SANDBOX_DEFAULT` 设为 `container`(对不受信代码)。
- [ ] **限流**:`MINI_CC_RATE_LIMIT_RPM_DEFAULT` 设为合理值(默认 60 偏宽)。
- [ ] **日志级别**:生产用 `INFO`,不用 `DEBUG`(会泄露 prompt 内容)。
- [ ] **ulimit**:`nofile=65536`、`nproc=4096`。

### 9.2 定期审计(每月)

```bash
# 列出所有 * scope 的 key —— 应该最少,且都有明确 label
python -m mini_cc.server keys list <tid> | grep '\*'

# 看过期 key,清理
python -m mini_cc.server keys list <tid> | \
  awk -F'"expires_at":' '$2 ~ /null/ {print}'

# 审计 webhook 订阅
for tid in $(ls $MINI_CC_DATA_DIR/tenants); do
    for pid in $(ls $MINI_CC_DATA_DIR/tenants/$tid/projects); do
        cat $MINI_CC_DATA_DIR/tenants/$tid/projects/$pid/webhooks.json 2>/dev/null
    done
done | jq '.[].url'

# 看 sandbox 配置,关注 extra_mounts
grep -r "extra_mounts" $MINI_CC_DATA_DIR/tenants/*/sandbox.toml
```

### 9.3 安全公告订阅

watch 上游仓库的 release note,关注:

- `sandbox/` 子系统变更(可能影响隔离边界)。
- `auth/keys.py` 变更(key 格式或验证逻辑)。
- `sharing/` 子系统(F7 系列,token 安全)。
- 任何 CVE 相关 issue。

### 9.4 渗透测试建议

定期做以下演练:

1. **路径穿越**:用 `../../../etc/passwd` 作为 project_id / session_id 试 create,
   应当被 400 拒绝。
2. **跨租户访问**:用 tenant A 的 key 访问 tenant B 的 project_id,应当 404。
3. **Share token 篡改**:修改 token payload 任意字节,应当 BadShareToken。
4. **Webhook 签名绕过**:发不带 `X-MiniCC-Signature` 的 POST,接收方应当拒绝。
5. **Scope 提升**:用 `read:*` key 尝试 POST,应当 403。
6. **容器逃逸(若开 sandbox)**:在租户容器里尝试 `docker ps`,应当失败。

---

## 附录:常用命令速查

```bash
# 服务生命周期
systemctl {start|stop|restart|status} mini-cc
journalctl -u mini-cc -f --since "10 minutes ago"

# Key 管理
python -m mini_cc.server keygen <tenant> [--scopes ...] [--expires-in 7d]
python -m mini_cc.server keys list <tenant>
python -m mini_cc.server keys rotate <key> --grace-hours 24
python -m mini_cc.server revoke <key>

# 沙箱
python -m mini_cc.server sandbox status [--tid <tid>]
python -m mini_cc.server sandbox stop <tid>
python -m mini_cc.server sandbox build-image [--tag ...] [--dockerfile ...]

# 健康检查
curl /health
curl -H "Authorization: Bearer $KEY" /tenants/$TID/projects | jq length

# Metrics
curl /metrics | grep -E "http_requests_total|anthropic_tokens_total"
curl /metrics.json | jq .

# 备份
tar czf /backup/mini_cc-$(date +%F).tar.gz $MINI_CC_DATA_DIR

# 紧急清理磁盘
find $MINI_CC_DATA_DIR -name 'transcript_*.jsonl' -mtime +7 -delete
find $MINI_CC_DATA_DIR -name '*.txt' -path '*/tool_results/*' -mtime +30 -delete

# 看实时 SSE 流(调试用)
curl -N -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d '{"user_input":"hi"}' \
     http://<host>:8000/tenants/$TID/projects/$PID/sessions/$SID/send
```
