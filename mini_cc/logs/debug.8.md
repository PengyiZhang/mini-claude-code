# mini_cc 优化任务

注意不要动 端口8000 的python进程，它是LLM网关服务！！！

测试用模型，详细配置使用 .env 环境

```bash
# 租户
glm 的 anthropic端点: http://localhost:8000
API Key: sk-REDACTED 
模型使用 glm-4.7
```

python环境

## Agent Teammates 功能优化

当前主Agent session中的内容显示极为混乱，可以考虑优化区分一下teammates，比如当启动或者有teammates时，自动增加一个teammates看板，实时展示每个teammate中的消息与活动，而在主session中，则展示主Agent收到的与teammate发来的，以及用户发来的，且当是teammates发来的messages，则**需要用不同的头像/名称进行标识区分主Agent，teammate与User等**，同时考虑使用与agent输出消息相同的方式渲染agent收到消息的渲染。teammates看板可以考虑右侧栏弹出或者悬浮等方式(具体由你来考虑决定)，根据其数量进行排列展示，可折叠与展开，页面都能在消息更新时自动流式刷新显示

具体测试使用 真实LLM 开展，确保覆盖如下流程验证：

使用 playwright mcp，在前端 spawn 两个teammates，由主Agents发动，让两个teammates开始讨论关于 DeepResearch的技术进展 ，而主Agents则监视进展，发现问题时，可以**广播**（如果不支持，则需要设计支持一下，如果当前的消息系统不满足需要，则考虑升级更加鲁棒的消息架构）或者**单播**，且两个teammates需要有选择时，则发送给消息给Lead去批准或者做决定，当 Lead 发送批准后或者拒绝feedback后，teammates根据指示继续开展推进，最终所有结果都要能够汇总至主Agent Lead，然后由主Agent生成任务完成报告、输出等任务状态与收尾工作


## slash /mcp 管理状态调试

如下错误描述：重启服务后，/mcp 可以查看到tavily-remote-mcp 是已连接状态，然后 /mcp card中点击disconnect之后，整个mcp就没了，如果再点击上次/mcp card中的reconnect，具体日志如下：

```
/mcp
🔌
tavily-remote-mcp
— 5 tools live
connected
tools
reconnect
disconnect
1 connected
＋ tools
/mcp tools tavily-remote-mcp
🔌
tavily_search
— Search the web for current information on any topic. Use for news, facts, or data beyon…
tavily-remote-mcp
🔌
tavily_extract
— Extract content from URLs. Returns raw page content in markdown or text format.
tavily-remote-mcp
🔌
tavily_crawl
— Crawl a website starting from a URL. Extracts content from pages with configurable dept…
tavily-remote-mcp
🔌
tavily_map
— Map a website's structure. Returns a list of URLs found starting from the base URL.
tavily-remote-mcp
🔌
tavily_research
— Perform comprehensive research on a given topic or question. Use this tool when you nee…
tavily-remote-mcp
5 tools
/mcp
🔌
tavily-remote-mcp
— 5 tools live
connected
tools
reconnect
disconnect
1 connected
＋ tools
/mcp reconnect tavily-remote-mcp
Unknown server 'tavily-remote-mcp'. Available: (none)

```

建议修改 /mcp 功能列出所有正确配置的mcp，显示连接与未连接状态，如果是未连接状态，则支持点击建立连接，如果点击断开连接，该服务器也还显示，只是处在断开状态，支持重新连接等操作




