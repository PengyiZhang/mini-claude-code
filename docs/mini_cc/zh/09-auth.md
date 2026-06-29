[ < [08](08-sse-streaming.md) ] [ [10](10-workflow-v2.md) > ] · [English version](../en/09-auth.md)

# 09 — 认证、授权与租户隔离

> HTTP 路由层把"你是谁"(tenant)、"你能干啥"(scope)、"这一刻能不能进来"
> (rate limit) 拆成三个正交的关卡,全部由一张 JSON 文件(`keys.json`)背书。
> 本章讲解 `mini_cc/auth/` 的 API key 注册表、scope 语法、Bearer 校验流水线,
> 工作流 V2 入站 webhook 的共享密钥 fallback (`_service_for_unauth`),以及
> `tenant_id` 如何一路下沉到 3 层插件/存储命名空间。工具级授权是另一个维度,
> 见第 06 章。

---

## 问题与动机

mini_cc 是一个多租户的 agent 后端:多个 `tenant_id` 共享同一个进程、同一个
文件系统、同一个 Anthropic API 配额。如果没有显式的租户边界,四种故障立刻爆开:

1. **租户 A 用租户 B 的 project**。`/tenants/{tid}/projects/{pid}/...` 路径里,
   `tid` 是租户从 URL 声明出来的,`pid` 是被操作的 project。如果路由只信 URL 的
   `tid` 而不校验它和 API key 绑定的租户一致,A 租户伪造 `tid=B` 就能读 B 的会话。
2. **只读 key 跑写操作**。给 CI 发一个 `read:*` 的 key 用于拉指标,但如果路由层
   不区分读/写,这个 key 就能 POST `/send` 烧 LLM 配额。需要按方法 (GET/非 GET)
   的粗粒度 scope 门控。
3. **过期/吊销的 key 继续工作**。运维轮换一个被泄露的 key,但旧的 key 还在 CI
   环境变量里。注册表必须支持惰性过期 + 即时吊销 + 宽限期轮换,且校验路径必须每次
   都查磁盘(或注册表缓存),不能依赖启动时的一次性快照。
4. **外部 webhook 进不来**。GitHub、Stripe、SendGrid 这些第三方往你这里 POST,
   它们不会带你的 Bearer token。如果 webhook 路由也强制 Bearer + 租户匹配,所有
   入站集成都断了。需要一条"无 Bearer 但有共享密钥"的旁路。

`mini_cc/auth/` 解决前三件,`mini_cc/server/routes/workflow_v2.py` 的
`_service_for_unauth` 解决第四件。授权(工具级)单独活在 `mini_cc/core/permissions.py`
和 `permissions.toml`,见第 06 章 —— 本章只讲传输层。

---

## 设计与实现

### 1. `KeyRecord`:一个 key 的完整身份

注册表里每个条目是一个 `KeyRecord`(`mini_cc/auth/keys.py:52-72`):

```python
@dataclass
class KeyRecord:
    key: str                     # "mck_<32 hex>"
    tenant_id: str               # 这个 key 属于哪个租户
    scopes: list[str]            # ["*"] / ["sessions:write", "files:read"] ...
    created_at: str              # ISO8601
    expires_at: str | None       # None = 永不过期
    label: str                   # 人类可读,如 "ci-readonly"
    rotated_from: str | None     # 若是 rotate 出来的,记录原 key
```

`is_expired`(`keys.py:63-69`)是惰性的:已过期的记录仍留在 `keys.json` 里
(让 `list_for` 能看到历史),只在 `lookup` 时被拒。注意 fail-open 的取舍:
解析失败/格式坏的 `expires_at` 视为**未过期**(`keys.py:67-68` 的注释
"malformed → not expired (fail-open)"),因为宽松的策略至少让 key 还能用,
紧的策略会因一时钟漂移锁掉所有人。

key 格式 `mck_<32 hex>`(`keys.py:30-31`,`_KEY_HEX_BYTES = 16` → 32 个 hex 字符)
由 `secrets.token_hex` 生成(`keys.py:104`),即 128 bit 的密码学随机数,
不可猜测、不可枚举。

### 2. `TenantKeyRegistry`:线程安全的 JSON 后端

整个注册表是一份 `keys.json`,结构是 `{api_key: KeyRecord_dict}`
(`keys.py:86-117`)。所有写操作走 `threading.Lock` + 原子 rename
(`_write` 在 `keys.py:267-285`:`tmp = path.with_suffix(...+".tmp")`,
`json.dump`,然后 `os.replace(tmp, path)`)。`os.replace` 在同一文件系统上
是原子的,所以跨进程也是安全的 —— 一个进程读到要么是旧文件要么是新文件,
永远不会读到半截 JSON。

七个公共方法对应 CLI 与 admin 路由的每个动作:

| 方法 | 行号 | 用途 |
|---|---|---|
| `generate(tenant_id, scopes, expires_in, label)` | `keys.py:97` | 新建 key |
| `lookup(key)` | `keys.py:119` | 校验时调用,**自动过滤过期** |
| `list_for(tenant_id)` | `keys.py:136` | admin 列表(含过期) |
| `revoke(key)` | `keys.py:147` | 即时删除 |
| `rotate(key, grace_hours, ...)` | `keys.py:157` | 轮换,可选宽限期 |
| `update(key, scopes, expires_in, label)` | `keys.py:205` | 增量 patch |

`rotate` 的关键设计(`keys.py:187-203`):`grace_hours > 0` 时,旧 key 被改写
成"在 `now + grace_hours` 过期"的新记录,而非删除。这样 CI 在轮换窗口里
两个 key 都能用,不会因部署时序问题断服。`grace_hours=0`(默认)是硬吊销,
旧 key 立刻不可用。

向后兼容:`_record_from_raw`(`keys.py:236-255`)容忍 v0 的 bare-string 格式
(老 `keys.json` 直接是 `{"mck_xxx": "tenantA"}`),自动提升成
`KeyRecord(scopes=["*"], label="migrated")`,且文件直到下次 mutation 才被规范化
写回 —— 老 fixture 字节级保持不变,测试不会因格式升级而崩。

### 3. Scope 语法:一种字符串,三处通配

Scope 是冒号连接的 `resource:verb`(`mini_cc/auth/scope.py:1-15`)。语义
在 `scope_allows(held, required, method)`(`scope.py:56-111`)里:

| 持有 | 匹配 |
|---|---|
| `*` | 任何 |
| `read:*` | 任何 GET |
| `write:*` | 任何非 GET |
| `sessions:*` | `/sessions/...` 上的任何方法 |
| `sessions:read` | `/sessions/...` 上的 GET |
| `sessions` | `sessions:*` 的简写 |

`read` ≡ `GET`,`write` ≡ 其余一切(`scope.py:52-53` 的 `_verb_for_method`)。
这个映射固定在 HTTP 层,路由声明 scope 时不必显式写动词 ——
`Depends(require_scope("sessions:write"))` 会按真实请求方法解析。

通配解析有三档(`scope.py:85-110`):
- **verb-prefixed 通配**(`read:*` / `write:*`):左侧是动词不是资源,匹配方法。
- **资源作用域**(`sessions:*` / `sessions:write`):左侧是资源,匹配 route 的
  `required` 资源。
- **全通配资源**(`*:read`):对称支持,罕见。

### 4. Bearer 校验流水线:`_resolve`

`mini_cc/server/deps.py:73-95` 是核心。每个受保护的端点都挂一个
`Depends(require_scope("xxx"))`,内部走这条链:

```
GET /tenants/A/projects/p1/sessions/s1  Authorization: Bearer mck_yyy
   │
   ▼
_parse_bearer(authorization)              deps.py:35-41
   │  分 "Bearer" + token;格式错 → None
   ▼
key 缺失?  → 401 Unauthorized "missing bearer token"     deps.py:76-77
   │
   ▼
reg.lookup(key)                           deps.py:79
   │  查 keys.json + 惰性过期
   ▼
rec is None? → 401 "unknown or expired api key"          deps.py:80-81
   │
   ▼
rec.tenant_id != tid? → 403 Forbidden "does not match tenant"  deps.py:82-83
   │  ← 这就是租户边界:A 租户的 key 碰 B 租户的 URL,在这里死掉
   ▼
scope_allows(rec.scopes, required, method)?              deps.py:84
   │  否 → 403 "insufficient_scope",带 WWW-Authenticate 头 + held/required detail
   ▼
request.state.tenant_id = tid; return tid                deps.py:94-95
```

三件关键事:
- **租户匹配在 scope 之前**(`deps.py:82` 先于 `:84`)。顺序很重要:
  先确认 key 属于这个租户,再谈它有没有权限。否则一个 `*` scope 的 B 租户 key
  能通过 scope 检查,只是被后面的租户检查拦下 —— 但错误信息会更含糊。
- **401 vs 403 的区分**。401 = "我不知道你是谁"(缺/未知/过期 key);
  403 = "我知道你是谁,但你不能这么干"(租户不符 / scope 不够)。客户端可据此
  区分"该重新登录"还是"该申请权限"。
- **`insufficient_scope` 带 `WWW-Authenticate` 头**(`deps.py:90-91`),
  遵循 RFC 6750 的 OAuth2 Bearer 错误格式,标准 OAuth2 客户端能自动解析。

`require_tenant`(`deps.py:44-53`)是 `require_scope("*")` 的向后兼容 shim,
留给尚未迁移的路由用。新代码一律用 `require_scope` 显式声明资源。

### 5. 路由侧消费:每个端点声明它要什么 scope

以 sessions 路由为例(`mini_cc/server/routes/sessions.py`):

```python
# sessions.py:35
def create_session(..., tid: str = Depends(require_scope("sessions:write"))):
# sessions.py:61
def get_session(..., tid: str = Depends(require_scope("sessions:read"))):
# sessions.py:161 — /send 既消费 scope 又消费限流令牌
def send(..., tid: str = Depends(check_rate_limit_scope("sessions:write"))):
```

`check_rate_limit_scope("sessions:write")`(`deps.py:131-140`)是组合依赖:
先跑 `require_scope`,再跑 `_apply_rate_limit`,失败抛 429 + `Retry-After`
(`deps.py:143-156`)。租户级令牌桶的 key 就是解析出来的 `tid`,所以一个
租户的 CI 跑疯不会拖垮另一个租户。

admin 路由用 `admin:read` / `admin:write`(`routes/admin.py:39, 46, 63, ...`),
且 `_check_key_belongs_to`(`admin.py:32-35`)再校验一次目标 key 属于当前
租户 —— **admin 不是超级用户**,只能管自己租户的 key。一个 `admin:write` 的
A 租户 key 不能 rotate B 租户的 key,会拿到 404(故意不暴露存在性)。

### 6. `_service_for_unauth`:外部 webhook 的共享密钥旁路

GitHub / Stripe / SendGrid 这些第三方会 POST 进来,它们没法带 Bearer。工作流
V2 的入站 webhook 路由刻意**不走** `require_scope`:

```python
# mini_cc/server/routes/workflow_v2.py:401-437
@runs_router.post("/{run_id}/webhook/{step_id}", response_model=RunOut)
def resolve_webhook_wait(run_id, step_id, body=None, webhook_id=None,
                          pid=..., pm=Depends(get_pm)):
    validate_id(pid)
    svc = _service_for_unauth(pm, pid)        # ← 不查 Bearer,不校验租户
    run = svc.get_run(pid, run_id)
    ...
    step = next((s for s in d.steps if s.id == step_id), None)
    expected_wh = step.config.get("webhook_id")
    if expected_wh:
        if not webhook_id or webhook_id != expected_wh:
            raise Unauthorized("invalid or missing webhook_id")  # 共享密钥门
    ...
```

`_service_for_unauth`(`workflow_v2.py:440-459`)就是 `_service_for` 去掉租户
检查 —— docstring 说得很直白:"The shared-secret (webhook_id query param)
gate replaces the tenant boundary for this specific endpoint."

**安全模型**:这里的门是 step 配置里的 `webhook_id` —— 一个随机字符串,作为
共享密钥嵌在 GitHub webhook URL 里 (`...?webhook_id=<secret>`)。攻击者要伪造
webhook,既要知道 `run_id`、`step_id`,还要知道这个 `webhook_id`,三者缺一不可。
对于外部集成这是标准做法(Slack 的 signing secret、Stripe 的 webhook token
都是同一思路)。如果 step 没配 `webhook_id`,这个端点完全开放 —— 那是 step
作者的显式选择,框架不强制。

W4 的入站邮件路由(`workflow_v2.py:467-493`)走同一个 `_service_for_unauth`,
门控由 `step_id` + step 配置里的邮件过滤器提供。

### 7. `tenant_id` 下沉到存储与插件命名空间

认证解析出 `tid` 后,它不只是挂在 `request.state` 上 —— 它通过 ProjectManager
一路下沉到存储路径。`mini_cc/plugins/paths.py` 定义了三层 tier:

```
tier_dir(data_dir, PluginTier.SYSTEM)                     # 全局共享
tier_dir(data_dir, PluginTier.TENANT, tenant_id=tid)      # 按租户隔离
tier_dir(workspace, PluginTier.PROJECT)                   # 按项目隔离
```

`project_tier_dirs(data_dir, tenant_id, workspace)`(`paths.py:71-79`)返回
这三者的有序列表,discovery 走 system → tenant → project 顺序,同名条目后者
覆盖前者(`paths.py:19-20` 的注释:"project > tenant > system")。

所以一个租户的 `.mini_cc/skills/`、`.mini_cc/memory/`、`.mini_cc/mcp.json`
是和别的租户物理隔离的目录树。即使路由层有 bug 漏过租户校验,discovery 也只会
在错误租户的目录里找东西,不会跨租户污染。这是纵深防御:认证是第一道,
文件系统布局是第二道。三层 MCP 插件发现的完整细节在第 11 章,本章不展开。

存储层 (`mini_cc/storage/fs.py`) 同理按 `<data_dir>/tenants/<tid>/projects/<pid>/`
组织,ProjectManager 按 `(tenant_id, project_id)` 缓存组装好的 project
(`projects/manager.py:142`)。`tenant_id` 进了 meta(`manager.py:114`),
project 的 `workspace` 解析、tier 目录拼装都基于它。

---

## 操作与验证

### CLI:keygen / list / rotate / revoke

```bash
# 1. 给 tenant "acme" 建一个全权限 key
python -m mini_cc.server keygen acme
# → mck_3f9a...  tenant=acme  scopes=*  expires=never

# 2. 建一个只读、7 天过期的 CI key
python -m mini_cc.server keygen acme --scopes "read:*" --expires-in 7d --label "ci-readonly"
# → mck_...  tenant=acme  scopes=read:*  expires=2026-07-06T... [ci-readonly]

# 3. 列出 acme 的所有 key(含已过期)
python -m mini_cc.server keys list acme

# 4. 轮换:24 小时宽限期,旧 key 在窗口内仍可用
python -m mini_cc.server keys rotate mck_<old> --grace-hours 24
# → new: mck_<new>  (rotated from mck_<old>…)
#   old: mck_<old>  expires=<now+24h>

# 5. 立即吊销
python -m mini_cc.server keys revoke mck_<leaked>
# → revoked
```

CLI 代码在 `mini_cc/server/cli.py:152-208`;`_format_record`(`cli.py:143-149`)
负责那行人类可读输出。注意 `keygen` 子命令在 `cli.py:302-313` 注册。

### HTTP:用 curl 验证三层门

```bash
# A. 缺 Bearer → 401
curl -s -o /dev/null -w "%{http_code}\n" \
  http://127.0.0.1:8002/tenants/acme/projects/p1/sessions
# → 401

# B. 未知 key → 401
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer mck_deadbeef" \
  http://127.0.0.1:8002/tenants/acme/projects/p1/sessions
# → 401  (unknown or expired api key)

# C. 租户不匹配 → 403
#    用 acme 的 key 访问别的租户的 URL
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $ACME_KEY" \
  http://127.0.0.1:8002/tenants/other/projects/p1/sessions
# → 403  (api key does not match tenant)

# D. scope 不足 → 403 + insufficient_scope
#    用 read:* key 尝试 POST
curl -i -X POST -H "Authorization: Bearer $READONLY_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_input":"hi"}' \
  http://127.0.0.1:8002/tenants/acme/projects/p1/sessions/s1/send
# HTTP/1.1 403 Forbidden
# WWW-Authenticate: Bearer scope="sessions:write"
# {"error":{"code":"forbidden","message":"key lacks required scope: sessions:write",
#           "details":{"code":"insufficient_scope","required":"sessions:write",
#                       "held":["read:*"]}}}

# E. 过期 key → 401 (即使 scope 是 *)
python -m mini_cc.server keygen acme --expires-in 1s   # 1 秒后过期
sleep 2
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $(that key)" \
  http://127.0.0.1:8002/tenants/acme/projects/p1/sessions
# → 401

# F. admin 路由:用普通 * key 访问 → 403 (缺 admin scope)
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $ACME_KEY" \
  http://127.0.0.1:8002/tenants/acme/admin/keys
# → 403

# G. 共享密钥 webhook:无 Bearer,正确的 webhook_id → 200
curl -X POST \
  "http://127.0.0.1:8002/tenants/acme/projects/p1/workflow_v2/runs/r1/webhook/step1?webhook_id=$SECRET" \
  -H "Content-Type: application/json" -d '{"event":"push"}'
# → 200 (假设 run r1 的 step1 是 webhook_wait 且配了 webhook_id=$SECRET)

# H. 共享密钥 webhook:错的 webhook_id → 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  "http://127.0.0.1:8002/tenants/acme/projects/p1/workflow_v2/runs/r1/webhook/step1?webhook_id=wrong" \
  -d '{}'
# → 401  (invalid or missing webhook_id)
```

### 检查磁盘上的 keys.json

```bash
cat $(python -c "from pathlib import Path; print(Path.home() / '.mini_cc' / 'keys.json')")
# 或自定义 data_dir:cat $DATA_DIR/keys.json
# 看到每个 key 的完整 KeyRecord,包括 expires_at、label、rotated_from
```

### Playwright:web UI 的登录流

Web 前端 (`mini_cc/web/`) 把 API key 当作用户"登录"凭据:输入框收 key,前端
把它存进 `localStorage`,每个 fetch 带上 `Authorization: Bearer <key>`。租户
字段从前端表单填,必须和 key 绑定的 tenant 一致,否则后端 403。手动验证:

```bash
cd mini_cc/web && npm run dev
# 浏览器打开,输入一个 acme key 和 tenant="acme" → 进主页
# 故意输 tenant="other" → 403 toast "api key does not match tenant"
```

---

## 常见陷阱与最佳实践

1. **改了 `keys.json` 没生效**。`TenantKeyRegistry` 没有内存缓存 —— 每次
   `lookup` 都 `json.load` 整个文件 (`keys.py:127-128` 在锁内读)。所以编辑
   `keys.json` 后立即生效,不需要重启。但**别用文本编辑器直接写**:并发请求
   期间你的写入会和路由的读取竞争,可能写到一半被读到。永远走 CLI 或 admin 路由,
   它们走 `os.replace` 原子 rename。
2. **`*` scope 不等于 admin**。一个 `*` key 能调所有业务端点,但 admin 路由
   要求 `admin:read` / `admin:write`(`admin.py:39, 46`)。`scope_allows` 严格
   匹配 resource,`*` 通配匹配任何资源*包括* `admin` —— 所以 `*` 其实**能**
   管 admin。如果你想要"全业务权限但不能管 key",用 `sessions:* files:* projects:*`
   等显式列表,别用 `*`。这是 scope 设计的一个已知 footgun。
3. **rotate 后忘了更新 CI**。`grace_hours` 给你窗口,但默认是 0(硬吊销,
   `keys.py:200-203`)。生产 rotate 务必带 `--grace-hours 24`(或更长),
   否则 CI 会在你换完 key 的瞬间全线红。
4. **`expires_at` 解析失败的 fail-open**。`is_expired` 对格式坏的 `expires_at`
   返回 False (`keys.py:67-68`)。这是有意的(避免时钟/格式问题锁死所有 key),
   但意味着如果你手写 `keys.json` 把日期写错了,那个 key 实际上永不过期。
   用 CLI / admin API 生成,别手写。
5. **webhook 端点没有租户边界**。`_service_for_unauth` (`workflow_v2.py:440`)
   故意跳过租户校验。你的 `webhook_id` 必须足够随机(至少 128 bit),否则
   攻击者能猜出来。step 配置里的 `webhook_id` 是唯一的一道门 —— 别用
   `run_id`、`step_id`、`github` 这种可猜值。
6. **`TenantPrincipal` 是兼容别名**。`keys.py:75-84` 的 `TenantPrincipal` 是
   给老 import 留的。新代码一律用 `KeyRecord`,它带 scopes / expires_at,
   `TenantPrincipal` 只有 key + tenant_id 两个字段。
7. **多 worker 部署**。`threading.Lock` 只保护单进程 (`keys.py:92`)。多 worker
   (gunicorn -w 4)下,跨进程安全靠 `os.replace` 的原子 rename。但**并发写**
   会互覆盖:worker A 写完,worker B 基于旧快照写,覆盖 A。如果你跑多 worker
   且 key 操作频繁,考虑外置注册表 (Redis) 或把 key 管理收口到单进程 sidecar。
   多数部署 key 操作很少,这个限制不痛。

---

## 小结

mini_cc 的认证/授权是三个正交关卡的组合:

- **`TenantKeyRegistry`**(`mini_cc/auth/keys.py`):JSON 后端的 key 存储,
  惰性过期、原子写入、宽限期轮换、向后兼容 v0 格式。
- **`scope_allows`**(`mini_cc/auth/scope.py`):一种字符串语法,用 `resource:verb`
  + 三档通配表达从粗到细的权限。
- **`require_scope` / `_resolve`**(`mini_cc/server/deps.py`):FastAPI 依赖,
  串起 Bearer 解析 → 注册表查询 → 租户匹配 → scope 校验 → 限流,每一步用
  合适的 401/403/429 编码失败原因。

旁路是工作流 V2 的 `_service_for_unauth`(`workflow_v2.py:440`),用 step 配置
里的共享密钥替换租户边界,让外部 webhook 进得来。`tenant_id` 通过 ProjectManager
下沉到三层 tier 目录(system/tenant/project)和存储路径,即使路由层漏检,
文件系统布局仍是第二道防线。

工具级授权(交互式 permission prompt、deny-list hook)是另一个维度,见
第 06 章。三层 MCP 插件发现的细节见第 11 章。

---

[ < [08](08-sse-streaming.md) ] [ [10](10-workflow-v2.md) > ] · [English version](../en/09-auth.md)
