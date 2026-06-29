[ < [11](11-mcp-plugins.md) ] [ [13](13-teams-scheduler.md) > ] · [中文版本](../zh/12-skills-commands-lsp.md)

# 12 — Skills, Commands, LSP

> These are mini_cc's "three helpers" for the LLM: **Skills** (markdown
> packs the model loads on demand via `load_skill`), **slash commands**
> (small actions triggered when the user types `/foo` in chat, handled
> either client-side or server-side), and **LSP integration** (exposing
> language servers like pyright / clangd / tsserver as a tool). All three
> ride on the 3-tier plugin layout from [chapter 11](11-mcp-plugins.md).

---

## Problem & motivation

Giving the model fs / bash tools isn't enough. In production you also need
to answer three questions:

1. **How does the model know which playbook to use right now?** Stuffing
   every skill's full text into the system prompt is too expensive; not
   mentioning them means the model doesn't know they exist. You need a
   lightweight catalog plus an on-demand `load_skill` mechanism, with
   skills discovered from system / tenant / project tiers and project
   overriding tenant.
2. **What about quick user actions?** A full model turn is slow and
   verbose for things like "clear this session's history", "list MCP
   servers", "switch to another session", "export this chat to md". You
   need an extensible slash-command registry where server-scoped commands
   return the same shape as a normal model turn (an SSE event stream), so
   the frontend doesn't need a special render path for commands.
3. **How do you make the model actually understand code, not just read
   strings?** grep finds text, but "who calls this function" or "what's
   the type of this variable" are semantic questions only LSP can answer.
   You need to expose LSP's 9 common operations as a single `lsp` tool,
   auto-pick the language server by file extension, and not drag in heavy
   dependencies like `pygls`.

Source: `mini_cc/skills/loader.py`, `mini_cc/commands/registry.py`,
`mini_cc/lsp/__init__.py`. All three share the `project_tier_dirs` +
`discover_skills` machinery covered in [chapter 11](11-mcp-plugins.md).

---

## Design & principles

### 1. SkillLoader: three-tier tier_dirs + legacy fallback

`SkillLoader.__init__` (`skills/loader.py:20`) accepts three construction
modes:

- `tier_dirs=[...]` (preferred): an explicit list of `.mini_cc/` dirs in
  priority order (system first, project last);
- `data_dir + tenant_id` (convenience): internally calls
  `project_tier_dirs` to build the standard 3-tier list;
- neither: legacy mode that only scans `<project_root>/skills/`.

The `scan()` (`skills/loader.py:52`) merge logic:

```python
def scan(self):
    self._registry.clear()
    tier_dirs = self._resolve_tier_dirs()
    if tier_dirs:
        self._registry.update(discover_skills(tier_dirs))
    # Legacy fallback: <project_root>/skills/
    if self._legacy_skills_dir.exists():
        self._registry.update(
            discover_skills([self._legacy_skills_dir.parent]))
```

`discover_skills` (`plugins/discover.py:94`) walks tiers — **later tiers
override earlier on name clash**. `_scan_skill_dir` (`plugins/discover.py:53`)
uses `rglob("SKILL.md")` so nested layouts
(`skills/<category>/<name>/SKILL.md`) work; this is how third-party skill
packs (e.g. superpowers) ship. The skill name comes from frontmatter
`name` if present, else the parent directory name.

`catalog()` (`skills/loader.py:70`) renders one line per skill for the
system prompt; when the model sees an interesting one it pulls the full
text via the `load_skill` tool (registered by AgentLoop).

### 2. SlashCommand: client / server dual scope

```
User types /foo in the input box
      │
      ▼
CommandRegistry.resolve("foo") → SlashCommand
      │
      ├─ scope="client"  → frontend handles it (e.g. switch tab); backend
      │                    never sees the execution
      │
      └─ scope="server"  → POST /commands/{sid}/invoke
                              │
                              ▼
                          handler(ctx) yields SSE events
                          ({type:"text"|"done"|"error"|...})
                              │
                              ▼
                          Frontend renders with the exact same pipeline
                          as a model turn
```

`SlashCommand` dataclass (`commands/registry.py:41`):

```python
@dataclass
class SlashCommand:
    name: str
    description: str
    scope: Literal["client", "server"] = "server"
    aliases: tuple[str, ...] = ()
    handler: Optional[Callable[[CommandContext], Iterator[dict]]] = None
    visible: bool = True              # False for hidden aliases
```

`CommandContext` (`commands/registry.py:25`) bundles everything a handler
needs: `project_id / session_id / tenant_id / args / project /
session_manager / storage`. Every server handler is a generator yielding
dicts of the same shape as `AgentLoop.run` — that's why commands and
ordinary turns share the frontend render path.

`CommandRegistry.register` (`commands/registry.py:63`) does a small trick
with aliases: each alias is registered as a hidden pointer
(`visible=False`) back to the original SlashCommand, so `resolve("/?")`
transparently finds `help`.

### 3. The built-in command catalog (22 commands)

`default_registry()` (`commands/registry.py:1227`) populates 22 built-ins
on first call. Grouped by theme:

| Theme | Commands | Source |
|-------|----------|--------|
| Session mgmt | `/sessions`, `/resume`, `/fork`, `/clear`, `/compact` | `:135`, `:1155`, `:1110`, `:106`, `:453` |
| Config introspection | `/model`, `/config`, `/cost`, `/permissions`, `/output-style` | `:155`, `:796`, `:476`, `:519`, `:836` |
| Tools / MCP / Skills | `/tools`, `/mcp`, `/skills`, `/tasks` | `:205`, `:370`, `:172`, `:434` |
| Background / scheduling | `/bg`, `/loop`, `/agents` | `:1051`, `:716`, `:560` |
| Workflow (legacy) | `/workflow` subcommands clear/save/load/list/delete | `:873` |
| Search / export / logs | `/search`, `/export`, `/logs` | `:242`, `:279`, `:681` |
| Help | `/help` (alias `?`) | `:93` |

`/workflow` operates on the **legacy** Workflow (its docstring at
`commands/registry.py:873` says so explicitly), not the V2 from
[chapter 10](10-workflow-v2.md). The two coexist; V2 goes through the HTTP
API.

`/agents` (subcommands `stop <name>` / `inbox <name>`,
`commands/registry.py:560`) and `/bg` (`stop <bg_id>`, `:1051`) show the
"command with subcommands" pattern — the handler splits `ctx.args` itself
to dispatch.

### 4. LSP integration: 9 operations, one tool

`OPERATIONS` (`lsp/__init__.py:51`) maps the 9 semantic operations to
their JSON-RPC methods:

| Operation | LSP method |
|-----------|------------|
| `goToDefinition` | `textDocument/definition` |
| `goToImplementation` | `textDocument/implementation` |
| `findReferences` | `textDocument/references` |
| `hover` | `textDocument/hover` |
| `documentSymbol` | `textDocument/documentSymbol` |
| `workspaceSymbol` | `workspace/symbol` |
| `prepareCallHierarchy` | `textDocument/prepareCallHierarchy` |
| `incomingCalls` | `callHierarchy/incomingCalls` |
| `outgoingCalls` | `callHierarchy/outgoingCalls` |

`LSPManager` (`lsp/__init__.py:84`) is a **per-project** pool of language
servers, lazily started: the first time a file of a given language is
queried, the matching server spawns. `DEFAULT_LANGUAGE_SERVERS`
(`lsp/__init__.py:34`) lists candidate binaries per language;
`_find_binary` uses `shutil.which` to pick the first one on PATH.

```python
DEFAULT_LANGUAGE_SERVERS = {
    "python": ["pyright-langserver", "pylsp", "jedi-language-server"],
    "typescript": ["typescript-language-server", "vtsls"],
    "rust": ["rust-analyzer"], "go": ["gopls"], "java": ["jdtls"],
    ...
}
```

Each server is wrapped in a `_ServerHandle` (`lsp/__init__.py:64`) holding
a lock — **requests are serialized**, because most language servers handle
them serially anyway. `_launch_args` (`lsp/__init__.py:194`) handles
per-binary quirks: most accept `--stdio`, but `rust-analyzer`/`gopls`/
`jdtls` take no args, and `solargraph` uses `stdio` (no dash).

The tool entry `_lsp(ctx, args)` (`lsp/__init__.py:341`) does four things:

1. Validate the operation is in the 9;
2. Resolve the path through the sandbox
   (`ctx.sandbox.resolve_path` + `validate_path`), so project-relative
   paths work;
3. Infer language from file extension, requiring an explicit `language`
   argument if inference fails;
4. Pull `lsp_manager` off the project, send the request, format the
   JSON-RPC result as markdown for the LLM.

`_format_result` (`lsp/__init__.py:423`) customizes output per operation:
hover extracts `contents.value`, documentSymbol lists symbol names + kinds
(`_SYMBOL_KINDS` maps numeric kinds to "Function"/"Class"/...), and
definition / references list `uri:line:char`.

**Key design choice: no pygls dependency**. `_read_one_message`
(`lsp/__init__.py:278`) hand-rolls Content-Length frame parsing;
`_read_response` (`lsp/__init__.py:258`) loops reading until the response
with the matching id arrives, ignoring server notifications like
`window/showMessage` along the way. Every request is a single idempotent
round-trip; no persistent document state is maintained (servers fall back
to disk reads).

### 5. /help auto-discovery + aliases

`_cmd_help` (`commands/registry.py:93`) calls
`default_registry().all_visible()` at runtime, so **newly registered
commands appear in help immediately**. Aliases are registered with
`visible=False` pointing at the primary command, so `/?` doesn't take its
own help line but `resolve("/?")` still finds `help`.

---

## Operation & configuration

### Skills file layout

```
<tier>/.mini_cc/skills/
    my-skill/
        SKILL.md           ← frontmatter (name, description) + body
    category/nested/       ← arbitrary nesting depth supported
        deep-skill/SKILL.md
```

Frontmatter is optional; without it, name defaults to the parent
directory name and description to the first body line
(`plugins/discover.py:87`).

### Slash command registration (extension point)

```python
from mini_cc.commands import SlashCommand, default_registry

def _my_cmd(ctx):
    yield {"type": "text", "text": f"hi {ctx.args}"}
    yield {"type": "done"}

default_registry().register(SlashCommand(
    name="hi", description="Say hi", handler=_my_cmd,
    aliases=("hello",),
))
```

Plugin `register` calls must happen at app startup, because
`default_registry()` returns a process-wide singleton
(`commands/registry.py:1224`).

### LSP server configuration

Defaults probe PATH. To swap or add a binary:

```python
from mini_cc.lsp import LSPManager
mgr = LSPManager(
    project_root=Path("/repo"),
    language_servers={"python": ["my-pyright"], "kotlin": ["kotlin-lsp"]},
)
```

There's no env-var switch; the LSPManager instance lives on
`project.lsp_manager`, and `_get_manager` (`lsp/__init__.py:498`) pulls it
off `ToolContext.project_ref`.

### File extension → language map

`_EXT_TO_LANGUAGE` (`lsp/__init__.py:308`) covers py/js/ts/jsx/tsx/c/cpp/
rs/go/java/rb. New extensions need either an explicit `language` argument
or an extended `language_servers` map.

---

## Verification steps

Backend on `:8002`. Warm a session and type in chat:

```
/help                        # list every visible command + aliases
/skills                      # list skills in this project
/tools                       # list builtin + MCP tools
/mcp                         # list connected / available / failed
/sessions                    # list all sessions in this project
/resume                      # no args → list 10 most recent sessions
/resume sess_xxx             # switch to a specific session
/export md                   # export, saved to <ws>/.mini_cc/exports/<sid>.md
/search login                # full-text search "login" across sessions
/agents                      # list teammates with age + inbox counts
/agents stop researcher      # send shutdown to teammate "researcher"
/bg                          # list background tasks
/loop                        # list cron + wakeup schedules
/config                      # show effective config (API keys auto-redacted)
```

LSP test (requires `pyright-langserver` on PATH):

```
> Use the lsp tool to find all references to the WorkflowService class
  in mini_cc/workflow_v2.py
# The model will call:
#   lsp(operation="workspaceSymbol", query="WorkflowService")
#   lsp(operation="findReferences",
#       filePath="mini_cc/workflow_v2.py", line=216, character=7)
```

Driving LSPManager directly from Python:

```python
from pathlib import Path
from mini_cc.lsp import LSPManager, OPERATIONS
mgr = LSPManager(Path("/path/to/repo"))
syms = mgr.request("python", "textDocument/documentSymbol",
                   {"textDocument": {"uri": Path("src/app.py").resolve().as_uri()}})
print([s["name"] for s in syms])
mgr.shutdown()
```

---

## Common pitfalls / debugging

1. **Added a skill but `/skills` doesn't show it?** `/skills` calls
   `project.skills_loader.scan()` at runtime (`commands/registry.py:184`)
   so it rescans live — but if the file isn't at
   `.mini_cc/skills/<name>/SKILL.md` (or the legacy `<root>/skills/`),
   it won't be found. Remember the parent directory name is the default
   skill id.
2. **Command handler edits don't take effect?** `default_registry()` is a
   process-wide singleton (`commands/registry.py:1224`); `register` runs
   only on the first call to `default_registry()`. You must restart the
   process after editing a handler.
3. **LSP `no LSP server binary found on PATH`?** `_find_binary` only
   checks candidates in `language_servers[language]`
   (`lsp/__init__.py:170`). After `pip install pyright`, confirm
   `pyright-langserver` is on PATH (not `pyright` — that's the npm CLI;
   the LSP entry point is `pyright-langserver`).
4. **LSP request `timeout`?** Default is 15s (`lsp/__init__.py:233` for
   init, `:382` for requests). First init on a big repo (especially
   TypeScript) can be slow; pass `LSPManager.request(..., timeout=30.0)`.
   `_read_response` loops, ignoring server notifications, waiting only
   for the response with the matching id (`lsp/__init__.py:258`).
5. **Call hierarchy complains about params?** `incomingCalls` /
   `outgoingCalls` need a `prepareCallHierarchy` first to obtain an item,
   then pass that item (`lsp/__init__.py:404`). The item must include
   `name / kind / uri / range / selectionRange`; defaults are filled from
   `line/character`.
6. **`/workflow` and V2 HTTP are two different things?** Yes. `/workflow`
   operates on the **legacy** `Workflow` (`commands/registry.py:873` says
   so), via storage's `save_workflow/load_workflow`. V2 is the HTTP API
   from [chapter 10](10-workflow-v2.md). The two data stores are not
   interchangeable.

---

## Further reading

- Source: `mini_cc/skills/loader.py`, `mini_cc/commands/registry.py`
  (1352 lines), `mini_cc/lsp/__init__.py` (573 lines)
- The shared 3-tier discovery foundation:
  [11 — MCP Clients & 3-tier Plugins](11-mcp-plugins.md)
  (same `plugins/discover.py`)
- Workflow V2 (contrast with the legacy `/workflow`):
  [10 — Workflow V2](10-workflow-v2.md)
- Skill / Memory shared discovery: `plugins/discover.py:53`
  (`_scan_skill_dir`), `:112` (`_scan_memory_dir`)
- LSP OPERATIONS table: `lsp/__init__.py:51`
