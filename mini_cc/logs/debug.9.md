# mini_cc 优化任务

注意不要动 端口8000 的python进程，它是LLM网关服务！！！

测试用模型，详细配置使用 .env 环境

```bash
# 租户
glm 的 anthropic端点: http://localhost:8000
API Key: sk-REDACTED 
模型使用 glm-4.7
```

python环境: uv -> `.venv\Scripts\python.exe`

## Agent Teammates 功能优化

现在对话输出显示有点混乱，下面是我测试中发现的问题:

1. 当创建有了 teammates 时，在主session(非teammates session)，用户输入消息回车后，马上会刷新一堆 teammates inbox的历史消息，示例如下，然后主session也不再刷新内容（即流式回答你叫什么的问题）
     
```
你叫什么                                                        
U
L

▶
🔧 check_inbox
✓ done
Hello Alice! I'm ready to help coordinate research work with you. I see you're online and ready to assist with research tasks.

Currently, I don't have any specific research tasks assigned, but I can definitely help you get started with some work. A few options:

Investigate a topic - I can assign you to research something specific
Gather and analyze information - You can help collect data on a particular subject
Explore documentation or resources - Research existing materials or find new ones
What kind of research would you like to focus on? Or would you like me to suggest some tasks based on what we're working on in this project?

A
@alice

▶
🔧 send_message
alice
✓ done

▶
🔧 check_inbox
✓ done

▶
🔧 send_message
alice
✓ done
I received your mention! I'm Alice, the researcher, and I'm ready to assist with any research tasks you need. What would you like me to investigate or research? I can help with:

Literature reviews and background research
Data gathering and analysis
Technical investigations
Market research
Best practices research
Any other research needs
Please let me know what specific research task you'd like me to work on.

A
@alice

▶
🔧 send_message
alice
✓ done

▶
🔧 check_inbox
✓ done

▶
🔧 todo_write
[1 item]
✓ done
Answer: 1.9 比 1.89 大。

Comparison:

1.9 = 1.90
1.89 = 1.89
1.90 > 1.89
So 1.9 is larger than 1.89.
```

2. 但是当刷新网页之后，内容又恢复正常，且已经显示对应答案，示例如下：                               

```                                                                                          
你叫什么
U
L
我是 mini_cc，一个AI编程助手！

我可以帮助你：

编写和调试代码
创建和管理项目文件
运行命令和脚本
协调队友（如Alice和Bob）完成不同任务
进行网络搜索和研究
有什么我可以帮助你的吗？
```

请务必保持对话内容的时序正确性！！！

3. 在同一个 project 下，新建一个session，简单输入 hi，输出结果却莫名其妙如下，感觉不同session被混入了teammates的 inbox messages，主要是完全不相关啊

```
hi
U
L
Hi

A
@alice
北京天气研究报告 - 2026年7月3日
研究结果摘要
根据最新气象数据，北京2026年7月3日的天气情况如下：

主要天气指标
日期：2026年7月3日（周五，农历五月十九）
天气状况：雷阵雨转晴
温度范围：24°C - 36°C
湿度：74%
风力：南风3级（约3米/秒）
日出日落：日出04:51，日落19:47
详细分析
温度特征
最低温度：24°C（出现在清晨）
最高温度：36°C（出现在下午）
温度趋势：午后气温较高，可能有雷暴天气
风力情况
风向：主要为南风
风力等级：3级（微风）
风速：约3米/秒
湿度与降水
相对湿度：74%（较为潮湿）
降水概率：高（雷阵雨）
能见度：受雷雨影响可能降低
24小时温度变化
早晨（6-8点）：23-24°C，湿度较高
上午（9-11点）：26-29°C，逐渐升温
中午（12-14点）：32-34°C，达到高温
下午（15-18点）：34-37°C，可能出现雷暴
晚上（19-21点）：33-34°C，开始降温
深夜（22-24点）：27-24°C，温度下降
生活建议
防暑降温：气温较高，注意防暑
防雨措施：有雷阵雨，携带雨具
空气质量：当前指数28（优），适合户外活动
穿衣建议：轻薄透气的夏季服装
数据来源
中央气象台
中国天气网
实时气象监测数据
研究完成时间：2026-07-03
```

4. 同样地，刷新页面后，输出内容感觉正常了

```
hi
U
L
Hi! I'm ready to help you with your coding project. What would you like to work on today?
```


## 关于 AgentLoop 能力的问题

我测试让它查一下 今天福田汽车股价如何

它居然连基础的日期都没弄对，今天的实际日期是2026-07-03，但它报告的却是 2026-07-01 的股价如下，完全离谱的很，体验很差，感觉要提升其 harness 能力

```
今天福田汽车股价如何
U
L

▶
🔧 mcp__tavily-remote-mcp__tavily_search
福田汽车股价 今日 2025年7月
✓ done

▶
🔧 mcp__tavily-remote-mcp__tavily_search
福田汽车 600166 实时股价 2025
✓ done
我来帮您查询今天福田汽车的股价情况。根据我查到的最新信息，福田汽车(600166)的股价情况如下：

最新股价信息
当前股价: 2.98元 (+0.04元，+1.36%)

交易时间: 2025年7月2日 15:00:00（北京时间）

今日交易情况
日内波动范围: 2.90元 - 2.99元
52周波动范围: 2.63元 - 3.86元
前一交易日收盘: 2.94元
7月1日交易数据
收盘价: 2.94元
涨跌幅: +1.03%
成交量: 111.55万手
成交额: 3.29亿元
换手率: 1.41%
公司基本面
根据2025年年报数据：

净利润: 13.64亿元（同比增长1593.3%）
每股净资产: 2.02元
每股收益: 0.10元
总体来看，福田汽车今日股价表现良好，小幅上涨1.36%，公司2025年业绩表现突出，净利润大幅增长。
```

---

## 收尾：Team tab 渲染层加固（2026-07-04 ~ 07-05）

> Phase I 主线合并后用 3 人相声团队（lead spawn alice 逗哏 / bob 捧哏 / carl 泥缝，表演《扒马褂》）做端到端验证时暴露的一系列渲染问题，集中记录在此便于后续维护。详细技术分析见 `docs/plans/2026-07-03-phaseI-team-orchestration-design.md` 第 6 节。

### 涉及 commit

| Commit | 主题 |
|---|---|
| `247db17` | 流式文本按 token 一行 → `mergeConsecutiveTexts` 渲染层合并 |
| `94581bf` | 合并方向 / speakerLabel / 后端 inbox 人话化 |
| `75ab087` | `teammate_message` 占位符 + LLM 信封幻觉过滤 |

### 端到端验证

- 数据目录：`mini_cc_data_xiangsheng/`（fresh tenant `xs` + project `xs_demo`，与生产数据隔离）
- 浏览器验证：lead spawn 三人 → 三人按捧哏/逗哏/泥缝分工表演 → 截图对比修复前后 Team tab
- 修复后 80/80 行干净对话（0 信封泄漏 / 0 占位符）
- 单测：`mini_cc/web/src/lib/teamEvent.test.ts` 22 个用例全过

### 截图

- `xs-team-tab-1.png` — 修复前 token 一行 + 信封泄漏现场
- `xs-team-tab-after-fix.png` — `teammate_message` case + chunk-level 检测修复后（仍有跨泡尾巴）
- `xs-team-tab-final.png` — flush-level 信封检测最终态

### 后续遗留

- LLM 偶发 `<function_calls>` 信封幻觉已纳入过滤，但根本原因是 LLM 行为而非渲染层 —— 后续可考虑给 LLM 加一条系统级 hint（"工具调用必须走 tool_use 事件，不要在文本里贴 XML"）。
- `mini_cc_data_xiangsheng/` 是一次性的 e2e 数据目录，未加 `.gitignore`（按本次「全部入库」意愿保留）。生产部署时不应依赖。

