
# 调试错误信息汇总

针对 mini_cc 对应功能，对应文档说明见:

```bash
mini_cc\README.md
docs\plans\
下面对应的phase B-C-D-E-F 都已经是实现，G和H尚未开始

```

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

- [ ] 1. Agent不懂如何为自己安装 skills: 以下为对应的测试，可能需要设计几个系统的skill包，以让模型能够知晓如何适配 mini_cc 系统

```bash

帮我安装 superpowers技能

... 一堆工具调用：

🔧 bash
bash node_modules/@complexthings/superpowers-agent/.agents/s…
✓ done

▶
🔧 read_file
.agents/AGENTS.md
✓ done
已成功安装和配置 superpowers 技能！

安装总结
✅ 安装了 @complexthings/superpowers-agent (v9.2.1)
✅ 运行了 bootstrap 初始化
✅ 创建了 .agents/AGENTS.md 配置文件
✅ 同步了所有技能到 ~/.agents/skills/
可用的技能类别
架构: preserving-productive-tensions
协作: brainstorming, dispatching-parallel-agents, executing-plans, finishing-a-development-branch, leveraging-cli-tools, receiving-code-review, requesting-code-review, subagent-driven-development, using-git-worktrees, writing-plans
调试: defense-in-depth, root-cause-tracing, systematic-debugging, verification-before-completion
元技能: create-agents-md, create-skill-json, creating-prompts, using-superpowers, writing-prompts
问题解决: collision-zone-thinking, inversion-exercise, meta-pattern-recognition, scale-game, simplification-cascades, when-stuck
研究: tracing-knowledge-lineages
测试: condition-based-waiting, test-driven-development, testing-anti-patterns, verification-before-completion
使用方法
在任何任务开始前，检查是否有相关技能可用：

bash node_modules/@complexthings/superpowers-agent/.agents/superpowers-agent find-skills
如果找到相关技能，必须在开始任务前使用它！



当前工作目录结构：

workspace/
├── .agents/
│   ├── AGENTS.md          # AI 代理配置文件
│   └── skills/            # 项目技能（符号链接）
│       ├── brainstorming -> ../node_modules/@complexthings/superpowers-agent/skills/collaboration/brainstorming
│       ├── test-driven-development -> ../node_modules/@complexthings/superpowers-agent/skills/testing/test-driven-development
│       ├── systematic-debugging -> ../node_modules/@complexthings/superpowers-agent/skills/debugging/systematic-debugging
│       ├── when-stuck -> ../node_modules/@complexthings/superpowers-agent/skills/problem-solving/when-stuck
│       └── [31个技能符号链接]
├── .bun/                  # Bun 缓存目录
├── .mailboxes/            # 邮箱目录
├── .mini_cc/              # Mini CC 配置
├── README.md              # 项目说明文档
├── node_modules/          # 依赖包
│   └── @complexthings/superpowers-agent/
├── npm-cache/             # npm 缓存
├── package-lock.json      # 锁定依赖版本
└── package.json           # 项目配置
```

上述安装过程虽然已经下载完毕，但是不符合 mini_cc 的skill系统，所以，在用 slash 命令 /skills 查看时，mini_cc 并没有发现skills，且把**工作空间严重的污染了（这种插件应当放入到.mini_cc中，不污染 workspace ）**，归根结底是不清楚 mini_cc 的skills系统，以及其它plugins的机制

我能想到的解决方案如下： 

```markdown
`skills.skill`: 去指导让模型如何配置、编写或者安装 mini_cc 系统支持的skills

`mcp.skill`: 去指导模型模型如何配置或者接入 mini_cc 系统支持的mcp工具

`agent.skill`: 去指导模型模型如何配置、编写或者安装 mini_cc 系统支持的agent

`workflow.skill`: 去指导模型模型如何配置、编写或者安装 mini_cc 系统支持的workflow

以及其它你应该想到的skills，其实就是对整个mini_cc相关接口插件的介绍，只是通过skills这种 分层封装、按需加载 这种机制来暴露而已！！！
```

- [ ] 2. slash命令 /cost 错误

```
AttributeError: 'Project' object has no attribute 'metrics'
```





