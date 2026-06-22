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

- 1. [ ] chat 没有流式输出
- 2. [ ] session 没有持久化，刷新后丢失所有
- 3. [ ] 文件夹上传没有保持文件夹目录结构，也没有看到对应的树状结构
- 4. [ ] 左侧面板上传与下载两个按钮实在丑陋，能否与树状结构合并，比如在所选中level下增加一个+号，点击即可上传文件或者文件夹

请先基于源码进行分析，修改明显错误、缺陷与新增功能，之后再基于 playwright mcp 从UI端开始进行验证