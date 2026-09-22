# Open-Source Restructure Design

Date: 2026-09-22
Branch: `restructure/opensource`

## Goal

Prepare this repository for open-sourcing: make the user's mini_cc work the
main body of the repo, relocate the upstream teaching content into a
reference directory, and rewrite the entry docs with attribution to the
upstream project.

Upstream: [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)
(MIT, Copyright 2024 shareAI Lab). This repo started from it; ~115 of 358
commits are upstream's; ~243 are the user's (PyZhang/ZhangPY).

## Decisions (confirmed with user)

1. Upstream content moves into `reference/learn-claude-code/` via `git mv`
   (full git history preserved).
2. Full git history is kept — no fresh init.
3. Entry README: English primary (`README.md`) + Chinese (`README.zh.md`).
4. Runtime tenant data (`mini_cc_data*`) is untracked and gitignored
   (stays on disk locally). Note: old blobs remain in history; scrubbing
   history would conflict with decision 2 and is deferred.

## Target structure

```
mini-claude-code/
├── README.md                 # new, English, mini_cc as the main body
├── README.zh.md              # new, Chinese
├── LICENSE                   # MIT, keep shareAI Lab line + add ZhangPY
├── pyproject.toml / requirements.txt / uv.lock / .env*.example
├── mini_cc/                  # user's framework (incl. web/ frontend)
├── tests/                    # user's tests (2 files get path fixes)
├── docs/                     # user's docs only (mini_cc/, plans/, 4 design docs)
└── reference/
    └── learn-claude-code/    # upstream snapshot, read-only reference
        ├── README.md / README-zh.md / README-ja.md   # original, + snapshot banner
        ├── s01_agent_loop/ … s20_comprehensive/
        ├── agents/  skills/  web/  docs/en|ja|zh/
        └── .github/workflows  # archived CI (won't run from subdir)
```

## Move list (`git mv`)

- `s01_agent_loop` … `s20_comprehensive` (20 dirs)
- `agents/`, `skills/`, `web/` (original Next.js learning platform)
- `README.md`, `README-zh.md`, `README-ja.md`
- `docs/en`, `docs/ja`, `docs/zh`
- `.github/` (original CI for teaching dirs; breaks after the move, so it is
  archived with the rest)

## Cleanup list

Untrack + gitignore:

- `mini_cc_data/`, `mini_cc_data_xiangsheng/`
- `mini_cc/web/mini_cc_data*` (196 tracked files)
- `.playwright-mcp/`, `.pytest_cache/`, `mini_cc.egg-info/`

Delete (tracked junk): root screenshots (`*.png`), `pytest_out.txt`.

## Dependency fixes

`tests/test_compaction_tool_pairs.py` and
`tests/test_todo_write_string_input.py` load `s05…s20/code.py` via
`REPO_ROOT / "s05_todo_write"` etc. Update to
`REPO_ROOT / "reference" / "learn-claude-code" / "s05_todo_write"`.

`mini_cc/` itself has no imports from the moved directories (verified by
grep), so nothing else changes.

## Entry README outline

1. Name + one-line positioning (reuse mini_cc/README.md: multi-tenant,
   sandboxed, backend-integrable mini Claude Code)
2. Highlights (from commit history): multi-tenant isolation, FastAPI +
   API-key auth + HTTP/SSE, Vite/React web console, team orchestration,
   IM channels + Feishu webhook, container sandbox, cron, MCP, 3-tier
   plugins, workflow, 150+ test files
3. Quick start (env → server → web)
4. Architecture (layered diagram, English)
5. Project layout (mini_cc/, tests/, docs/, reference/)
6. Acknowledgments (derives from shareAI-lab/learn-claude-code; upstream
   snapshot kept in reference/learn-claude-code/)
7. License (MIT)

`reference/learn-claude-code/README.md` gets a top banner: "snapshot of the
upstream repo, kept for reference; see root README for this project".
