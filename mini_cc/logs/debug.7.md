# 优化任务

注意不要动 端口8000 的python进程，它是LLM网关服务！！！

测试用模型，详细配置使用 .env 环境

```bash
# 租户

glm 的 anthropic端点: http://localhost:8000
API Key: sk-REDACTED 
模型使用 glm-4.7
```



- [ ] 1. Agents teammates 功能优化

手工 spawn 一个 teammate agent 后，当用户 @该agent时，期望的行文应当是发送消息给该Agent，然后主session应当刷新与该Agent交互的activities，并且该agent也能看到其它agents，包括主agent，需要@其它agent的化就@其它agent，以驱使它做事情，此时主agent的session中应当有当前活跃Agent的状态与调用逻辑的展现，以便用户能够在主Session即可看到Agent的动态

手工 spawn 的 teammate agent应当是常驻的，即执行完毕后，不退出，除非用户手工停止、删除（Card请支持停止、删除、编辑）


第二次 @alice 时（此时alice已经stop了），输出了一堆历史 tool acitivies，有点莫名其妙，刷新后就没了

```
@alice

▶
🔧 bash
cd /e/GitRepos/learn-claude-code/mini_cc_data/tenants/my_ten…
✓ done

▶
🔧 task_output
bg_66d3d6c3
✓ done

......

```

alice的手工spawn定义如下：
```
👥
alice
— joker · age 16m
stopped
▶
stop
```

下面是第一次@alice时新启动的 teammate-alice 的session内容，但是主session一直没有 teammate-alice 相关进展的activities与结果

```
<identity>You are 'alice', a joker. Use tools to complete the requested work. Send your final summary to 'lead' via send_message before stopping. After calling submit_plan, end your turn and wait for approval.</identity>

请讲个笑话，当被@时

▶
🔧 send_message
lead
✓ done
我来给你讲个笑话！😄

笑话时间：

有一天，0对8说："唉，我的腰好粗啊！"

8听了，想了想，说："那你看我，我不仅是腰粗，我还戴了皮带呢！"

再来一个：

为什么程序员总是分不清万圣节和圣诞节？

因为 Oct 31 == Dec 25！

（八进制的31等于十进制的25，哈哈！）

还有一个经典的双关语：

什么东西能写但不能读？答案是：橡皮（能写"错"但不能读"错"）。

希望这些笑话能让你开心！如果你还需要更多笑话，随时@我！😊笑话任务已完成！我分享了几个有趣的笑话，并通知了lead。现在你可以随时@我来听更多笑话！😄
```

- [ ] 2. slash命令 /mcp 应当允许查看 tools、禁用或者connect、reconnect等操作，与claude code对齐

- [ ] 3. workflow_v2的功能不知道如何使用，请详细确认以下几个问题，如果还不支持，请进行实现，如果支持，请编写详细的实践教程

    - 对于状态传递不清楚，比如 branch/loop 节点，如何定义prompt与条件，让它可以根据其上一个节点的执行结果进行分支引用或者退出循环的？
    - checkpoint审核，里面有approver，是否可以选择下拉或编辑已经手工 spawn 的teammates，或者人工，其具体是如何使用的


 