
# 调试错误信息汇总

进行了部分测试，发现了一些Bugs亟待修复：

使用 uv venv 环境

前端启动:
```bash
mini_cc\web> npm run dev -- --port 11111
```

后端启动
```bash
uv python -m mini_cc.server
```

测试用模型:

```bash
# 租户
mck_REDACTED  tenant=my_tenant  scopes=*  expires=never

# 模型
glm 的 anthropic端点: http://localhost:8000
API Key: sk-REDACTED 
模型使用 glm-4.7
```

已知的bugs如下：

- [ ] 1. skills系统目前扫描机制不支持类似以下的多级目录下的SKILL.md


```bash
mini_cc_data\tenants\my_tenant\projects\test01\workspace\.mini_cc\skills\architecture\preserving-productive-tensions\SKILL.md


我来帮你为 mini_cc 安装 superpowers skills。首先让我了解一下当前的 skills 目录结构。✅ Superpowers skills 安装成功！

已成功为 mini_cc 安装了 Superpowers skills 库，包含 31 个技能，分为以下类别：

📁 安装的结构
architecture - 架构相关技能 (1个)
collaboration - 协作技能 (11个)
debugging - 调试技能 (4个)
meta - 元技能 (5个)
problem-solving - 问题解决技能 (6个)
research - 研究技能 (1个)
testing - 测试技能 (3个)
using-skills - 使用技能指南 (1个)
🎯 主要技能包括
test-driven-development - 测试驱动开发
systematic-debugging - 系统化调试
brainstorming - 头脑风暴
dispatching-parallel-agents - 调度并行代理
subagent-driven-development - 子代理驱动开发
root-cause-tracing - 根因追踪
when-stuck - 遇到困难时的处理
所有技能文件都已正确安装到 .mini_cc/skills/ 目录中，每个技能都有完整的 SKILL.md 文件，可以在需要时调用使用。

/skills
Skills in test01:

agents — How to spawn and coordinate mini_cc agents/teammates — the spawn_teammate / send_message / submit_plan / review_plan tool family, and when to use subagent (task) vs teammate vs workflow. Read this before suggesting multi-agent patterns.
mcp — How to wire MCP (Model Context Protocol) servers into mini_cc via .mcp.json or mcp.toml under .mini_cc/. Read this before suggesting Claude-Code-style claude mcp add commands.
skills — How to write, install, and load mini_cc skills (the .mini_cc/skills/SKILL.md format). Read this BEFORE suggesting npm install or symlinks — mini_cc is not Claude Code.
workflow — How to build mini_cc workflows — declarative multi-step pipelines via workflow_create / workflow_add_step / workflow_run_step / workflow_run_all / workflow_status / workflow_set_state. Read this when the user wants a repeatable, gate-able pipeline of agent steps.
Getting Started with Skills — Skills wiki intro - mandatory workflows, search tool, brainstorming triggers
Tip: ask the agent to load_skill <name> to use one.
```

- [ ] 2. 目前目录树仅显示部分结构，没显示一些系统目录，请设计通过环境变量控制是否全显示完整的目录结构

- [ ] 3. 关于文件沙箱的权限问题，主要对于一些系统性的skills，在session workspace之外，是否例外能读?  请确认下这种情况！！！

- [ ] 4. 关于工具调用内容点击展开或者折叠之后，页面直接调到了对话内容的最后，这不方便进行内容查看，请修复！！！

