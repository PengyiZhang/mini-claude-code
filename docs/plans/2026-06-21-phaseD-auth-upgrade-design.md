# Phase D: auth upgrade (scopes, expiry, rotation)

## Context

Today `TenantKeyRegistry` is a flat `key → tenant_id` JSON map. Every
key has full access to every operation on its tenant. Phase D adds
the three pieces a multi-tenant backend needs before going near
production:

1. **Scopes** — least-privilege keys (CI key with `read:*`, audit
   script with `sessions:read`, etc).
2. **Expiry** — `--expires-in 7d` for short-lived keys.
3. **Rotation** — `keys rotate <key> [--grace-hours N]` so a
   compromised key can be replaced without hard-cutting clients.

## Approach (locked via brainstorm)

- **Scope grammar**: `*`, `read:*`, `sessions:*`, `sessions:write`,
  etc. Strict resource:verb matching. `read` ≡ GET, `write` ≡
  POST/PUT/PATCH/DELETE.
- **Storage shape**: replace string values with
  `{tenant_id, scopes, expires_at, created_at, label}`. Auto-migrate
  on first read (old strings → `scopes=["*"]`).
- **Rotation**: optional `--grace-hours N` (default 0 = hard revoke).
  With grace, old key keeps working until `expires_at = now + grace`.
- **Expiry**: lazy — `lookup()` rejects expired keys, but they stay
  in keys.json for explicit admin listing.
- **CLI**: extend `keygen` with `--scopes/--expires-in/--label`; add
  `keys list <tenant>` and `keys rotate <key> [--grace-hours N]`.
- **Enforcement**: per-route `require_scope("sessions:write")`
  dependency. Touches every route file.
- **Failure mode**: 403 with `details.code = "insufficient_scope"`
  + `WWW-Authenticate: Bearer scope="..."` hint.

## File-by-file

### `mini_cc/auth/keys.py` (rewrite)

```python
@dataclass
class KeyRecord:
    key: str
    tenant_id: str
    scopes: list[str]
    created_at: str        # ISO8601 UTC
    expires_at: str | None # ISO8601 UTC, or None
    label: str = ""
    rotated_from: str | None = None

class TenantKeyRegistry:
    def generate(self, tenant_id, *, scopes=None, expires_in=None,
                 label="") -> KeyRecord: ...
    def lookup(self, key) -> KeyRecord | None: ...   # None on unknown OR expired
    def list_for(self, tenant_id) -> list[KeyRecord]: ...
    def revoke(self, key) -> bool: ...
    def rotate(self, key, *, grace_hours=0, scopes=None,
               expires_in=None, label="") -> tuple[KeyRecord, KeyRecord | None]: ...
    def update(self, key, *, scopes=None, expires_in=None,
               label=None) -> KeyRecord | None: ...
```

`_read()` auto-promotes bare-string values to KeyRecord with
`scopes=["*"]`, `expires_at=None`, `label="migrated"`.

### `mini_cc/auth/scope.py` (new, ~60 lines)

```python
def parse_duration(s: str | int) -> int: ...
def scope_allows(held: list[str], required: str, method: str) -> bool: ...
```

`parse_duration("7d")` → 604800; `"12h"` → 43200; `"3600s"` → 3600;
plain int → seconds.

`scope_allows`:
1. verb = `read` if method == GET else `write`
2. resource, _ = required.split(":", 1)
3. for each h in held:
   - `h == "*"` → True
   - h_res, h_verb = h.split(":", 1)
   - h_res == "*" (i.e. `read:*`/`write:*`) and (h_verb == "*" or h_verb == verb) → True
   - h_res == resource and (h_verb == "*" or h_verb == verb) → True
4. False

### `mini_cc/server/deps.py` (modify)

Add `require_scope(required)` factory. Builds a dep that:
- parses bearer, looks up KeyRecord.
- 401 on missing / unknown / expired.
- 403 on tenant mismatch (`Forbidden`).
- 403 on scope miss with `insufficient_scope` details and
  `WWW-Authenticate: Bearer scope="<required>"` header.

Keep `require_tenant` for any callers that haven't migrated yet, but
internally it's `require_scope("*")`.

`check_rate_limit` chains on `require_scope(...)` per route (caller
specifies required scope as a sibling dep).

### `mini_cc/server/routes/*.py` (modify all route files)

Swap `Depends(require_tenant)` for the appropriate scope:

| Route file     | Endpoint          | Required scope        |
| -------------- | ----------------- | --------------------- |
| projects.py    | GET (list/get)    | `projects:read`       |
| projects.py    | POST              | `projects:write`      |
| projects.py    | DELETE            | `projects:write`      |
| sessions.py    | GET (list)        | `sessions:read`       |
| sessions.py    | POST start/resume | `sessions:write`      |
| sessions.py    | DELETE            | `sessions:write`      |
| sessions.py    | POST send         | `sessions:write`      |
| permissions.py | GET               | `sessions:read`       |
| permissions.py | POST decide       | `sessions:write`      |
| resources.py   | GET (tree/content/download) | `files:read` |
| resources.py   | POST/DELETE       | `files:write`         |

### `mini_cc/server/cli.py` (modify)

- Extend `keygen <tenant>` with `--scopes` (append), `--expires-in`
  (string), `--label` (string).
- Add `keys` parser with `list <tenant>` and `rotate <key>` subcommands.
- `rotate` has `--grace-hours` (int, default 0), `--scopes`, `--label`.

### `mini_cc/server/errors.py` (modify)

`Forbidden` already supports `details`. Add optional `extra_headers`
field so we can attach `WWW-Authenticate`. Error handler in `app.py`
merges those headers into the response.

### `tests/test_phaseD_registry.py` (new)

KeyRecord shape; migration of old format on read; generate/revoke/
rotate/update paths; expiry rejection; list_for filtering.

### `tests/test_phaseD_scope.py` (new)

`scope_allows` truth table covering `*`, `read:*`, `sessions:*`,
`sessions:write`, verb mapping, and OR semantics across held scopes.

### `tests/test_phaseD_http.py` (new)

TestClient: insufficient_scope → 403 + WWW-Authenticate; `*` works;
expired key → 401; migrated old key still works as `*`; rate-limit
coexists with scope check (scope first, then rate).

### `tests/test_phaseD_cli.py` (new)

Subprocess: keygen with --scopes, keys list, keys rotate --grace-hours.

### `mini_cc/README.md` + `.zh.md` (modify)

New "Authentication" section: scope grammar, migration, rotation
flow, CLI examples.

## Verification

```bash
python -m pytest tests/test_phaseD_*.py -v
python -m pytest tests/ -q   # expect ~310 passing
```

Manual:

```bash
python -m mini_cc.server keygen t1 --scopes "read:*" --label ci
# Hit a GET → 200. Hit a POST → 403 insufficient_scope.

python -m mini_cc.server keys rotate mck_<old> --grace-hours 24
# Both old and new work for 24h.

python -m mini_cc.server keys list t1
# Shows all keys with metadata.
```

## Out of scope for Phase D

- External KMS / Vault integration. Keys stay in `keys.json`.
- JWT signing — keys are opaque `mck_<hex>` tokens, validated by
  registry lookup.
- Per-IP allowlists on keys.
- Web UI for key management (Phase F).
