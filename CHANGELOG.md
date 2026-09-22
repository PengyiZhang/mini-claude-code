# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/) and the project
follows [Semantic Versioning](https://semver.org/). `mini_cc.__version__`
reads from `pyproject.toml` — bump the version there and tag
`v<version>` on main.

## [Unreleased]

## [0.1.0] — 2026-09-22

First public open-source release.

### Added

- Multi-tenant platform: per-project isolation, API-key scopes/expiry/
  rotation, rate limiting, Prometheus metrics, OpenTelemetry tracing
- Agent core: streaming AgentLoop, layered compaction, recovery chain,
  hooks, interactive permissions, subagents, skills, 3-tier memory, MCP
  pool, LSP tool, cron + wakeups, background tasks
- Multi-agent teammates: message bus, plan-approval gate, task
  auto-claim with worktrees, @mention routing, LeadWatcher
- Workflows: dynamic V1 runner + versioned V2 with human/webhook/email
  approval gates and console UI
- Bidirectional IM channels (Feishu built in), share links, outbound
  webhooks, project templates, 3-tier plugins
- Vite/React web console with card-based slash commands
- 1300+ test pytest suite and 15-chapter bilingual internals deep-dive
  (`docs/mini_cc/`)

### Acknowledgments

Derives from [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)
(MIT); the upstream teaching tree is preserved under
`reference/learn-claude-code/`.
