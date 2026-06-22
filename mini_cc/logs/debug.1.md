# 调试错误信息汇总

针对 mini_cc 对应功能，对应文档说明见:

```bash
mini_cc\README.md
docs\plans\
下面对应的phase B-C-D-E-F 都已经是实现，G和H未开始

```

进行了部分测试，发现了一些Bugs亟待修复，请基于 playwright mcp 从UI端开始进行 Debug :

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

- 1. [ ] UI 中新建project后，在project下的session进行对话错误如下：

[{"role": "user", "content": "你好"}, {"role": "assistant", "content": [{"type": "text", "text": "[Error] TypeError: \"Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set. Or for one of the `X-Api-Key` or `Authorization` headers to be explicitly omitted\""}]}]

- 2. [ ] 删除project，后台的 mini_cc_data\projects\.storage 中仍有对应的工程文件，且似乎没有做到租户隔离？

- 3. [ ] 当 session 对话出现错误后，再次进入该project前端会有如下错误:

```bash
Unexpected Application Error!
Objects are not valid as a React child (found: object with keys {session_id, created_at, last_active_at, message_count, in_memory}). If you meant to render a collection of children, use an array instead.
Error: Objects are not valid as a React child (found: object with keys {session_id, created_at, last_active_at, message_count, in_memory}). If you meant to render a collection of children, use an array instead.
    at throwOnInvalidObjectTypeImpl (http://127.0.0.1:11111/node_modules/.vite/deps/react-dom_client.js?v=3f32eb27:4596:15)
    at throwOnInvalidObjectType (http://127.0.0.1:11111/node_modules/.vite/deps/react-dom_client.js?v=3f32eb27:4604:13)
    at reconcileChildFibersImpl (http://127.0.0.1:11111/node_modules/.vite/deps/react-dom_client.js?v=3f32eb27:5215:13)
    at http://127.0.0.1:11111/node_modules/.vite/deps/react-dom_client.js?v=3f32eb27:5235:35
    at reconcileChildren (http://127.0.0.1:11111/node_modules/.vite/deps/react-dom_client.js?v=3f32eb27:7180:53)
    at beginWork (http://127.0.0.1:11111/node_modules/.vite/deps/react-dom_client.js?v=3f32eb27:8699:104)
    at runWithFiberInDEV (http://127.0.0.1:11111/node_modules/.vite/deps/react-dom_client.js?v=3f32eb27:995:72)
    at performUnitOfWork (http://127.0.0.1:11111/node_modules/.vite/deps/react-dom_client.js?v=3f32eb27:12559:98)
    at workLoopSync (http://127.0.0.1:11111/node_modules/.vite/deps/react-dom_client.js?v=3f32eb27:12422:43)
    at renderRootSync (http://127.0.0.1:11111/node_modules/.vite/deps/react-dom_client.js?v=3f32eb27:12406:13)
💿 Hey developer 👋

You can provide a way better UX than this when your app throws errors by providing your own ErrorBoundary or errorElement prop on your route
```

- 4. [ ] project中没有文档树渲染，且没有右键上传文档及其文件夹上传的功能

```bash

select a file to preview, or right-click any folder to upload files or folders.

files
sessions
⬇ Download ZIP
workspace

```

请修复过程中发现的前后端错误，并及时进行 commit 记录

