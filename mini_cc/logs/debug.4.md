
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

- 1. [ ] 执行复杂的环境配置任务时，启动了subagent，直接创建了subagent-session(ui上多了一个 subagent:dd583c47 session，但没有消息)，而不是 在当前session 展示activities，请确认 subagent 模式是否正确工作

```bash
sess_00ec7076
subagent:dd583c47
```

INFO:     127.0.0.1:12607 - "GET /tenants/my_tenant/projects/ai-proposal/sessions/sess_72fb197c/permissions HTTP/1.1" 200 OK
INFO:     127.0.0.1:12607 - "GET /tenants/my_tenant/projects/ai-proposal/sessions/sess_72fb197c/permissions HTTP/1.1" 200 OK
INFO:     127.0.0.1:12607 - "GET /tenants/my_tenant/projects/ai-proposal/sessions/sess_72fb197c/permissions HTTP/1.1" 200 OK
INFO:     127.0.0.1:12607 - "GET /tenants/my_tenant/projects/ai-proposal/sessions/sess_72fb197c/permissions HTTP/1.1" 200 OK

{"ts": "2026-06-23T09:06:25.424Z", "level": "INFO", "logger": "mini_cc.trace", "msg": "span.end", "span": "anthropic.request", "trace_id": null, "dur_ms": 0.020600040443241596, "tenant": "my_tenant", "model": "glm-4.7", "session_id": "subagent:305b563a"}
Exception in thread Thread-172 (_readerthread):
Traceback (most recent call last):
  File "D:\anaconda3\Lib\threading.py", line 1038, in _bootstrap_inner
    self.run()
  File "D:\anaconda3\Lib\threading.py", line 975, in run
    self._target(*self._args, **self._kwargs)
  File "D:\anaconda3\Lib\subprocess.py", line 1597, in _readerthread
    buffer.append(fh.read())
                  ^^^^^^^^^
UnicodeDecodeError: 'gbk' codec can't decode byte 0xa4 in position 904: illegal multibyte sequence
INFO:     127.0.0.1:12607 - "GET /tenants/my_tenant/projects/ai-proposal/sessions/sess_72fb197c/permissions HTTP/1.1" 200 OK

- 2. [ ] 补充内置工具与Bash命令，比如 curl等，并做好操作系统的适配性

- 3. [ ] 当刷新session后，可能会在用户输入内容展示的地方显示 <reminder>Update your todos.</reminder>，具体如下，请考虑屏蔽掉不进行显示

```bash

请使用 ai 提案技能，初始化目录 

▶
🔧 load_skill
ai-proposal
✓ done
我将使用 AI 提案技能来初始化目录。


▶
🔧 load_skill
ai-proposal-workflow
✓ done
让我查看一下可用的 AI 提案工作流技能：


▶
🔧 bash
pwd
✓ done
<reminder>Update your todos.</reminder>

▶
🔧 todo_write
[3 items]
✓ done

▶
🔧 bash
ls -la
✓ done

▶
🔧 bash
mkdir -p inters proposals/ch/docx proposals/ch/images refere…
✓ done

▶
🔧 write_file
manifest.json
✓ done
<reminder>Update your todos.</reminder>

```

- [ ] 4. 请丰富对应的 slash 命令，比如 /skills，/mcp 等常规管理功能


- [ ] 5. 请优化一下 markdown render 时对应的字体大小，目前看上去偏大，code 与 table 等与常规文字没有颜色上的区分






