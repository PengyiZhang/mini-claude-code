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
C:\ZhangPengYi\BITDeeper\mini-claude-code\mini_cc\web> npm run dev -- --port 11111
```

后端启动
```bash
(mini-claude-code) PS C:\ZhangPengYi\BITDeeper\mini-claude-code> python -m mini_cc.server
```

测试用模型:

```bash
# 租户
mck_REDACTED  tenant=my_tenant  scopes=*  expires=never

# 模型
deepseek 的 anthropic端点: https://api.deepseek.com/anthropic
API Key: sk-REDACTED 
模型使用 deepseek-v4-flash
```

已知的bugs如下：

- 1. [ ] files页卡点击上传文件/上传文件夹没有响应
- 2. [ ] chat session中ToDo任务看板置顶不合适，一刷新就看不到了，可否考虑固定到用户输入框的上方，悬浮或者怎么着的都可以
- 3. [ ] chat对话内容markdown格式没有被渲染，请考虑直接渲染，包括对<html>格式的扩展、以及mermaid渲染
- 4. [ ] bash 工具指令等实现需要考虑到后端系统差异，比如观察到 `bash mkdir -p "fastapi-react-app/backend/app/api/v1"` 居然也创建了名为 "-p" 的目录，请全面审查目前的命令平台兼容性
- 5. [ ] chat session 刷新后，对应的工具调用展开折叠查看失效（感觉是没有持久化工具结果），当请修复问题


请先基于源码进行分析，修改明显错误、缺陷与新增功能，之后再基于 playwright mcp 从UI端开始进行验证