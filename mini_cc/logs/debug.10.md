
目前主session会话显示还存在如下问题:


实时运行时的UI 主session目前展示就是如下，自动刷新到alice的消息后就停了：

```
让它们逐个进行自我介绍吧
U
L

▶
🔧 send_message
alice
✓ done

▶
🔧 schedule_wakeup
5
✓ done
我会让它们各自进行自我介绍。既然它们是按顺序“逐个”介绍，我会按序发送指令——先从 alice 开始。等待

A
@alice
Hi! I'm alice, a teammate on this team. My role is to pick up assigned tasks, collaborate with my peers, and ship work back to you — reporting progress by milestone and a final result when my mission is complete. Ready to roll whenever you've got work for me.

```

但是重新手动刷新页面后，就显示完整如下：

```
让它们逐个进行自我介绍吧
U
L

▶
🔧 send_message
alice
✓ done

▶
🔧 schedule_wakeup
5
✓ done
我会让它们各自进行自我介绍。既然它们是按顺序“逐个”介绍，我会按序发送指令——先从 alice 开始。等待唤醒触发，这样我就能查看 Alice 的回复并触发 Bob。

[Teammate messages]
- from: alice, type: message, cc: False
  content: Hi! I'm alice, a teammate on this team. My role is to pick up assigned tasks, collaborate with my peers, and ship work back to you — reporting progress by milestone and a final result when my mission is complete. Ready to roll whenever you've got work for me.
- [scheduled wakeup] Check alice's reply, then ask bob to introduce itself.
U
L

▶
🔧 send_message
Please introduce yourself. Send a short self-introduction (w…
✓ done

▶
🔧 schedule_wakeup
5
✓ done
Alice 的介绍已经收到。现在正在触发 Bob。

<teammate_messages>[{"from": "bob", "to": "lead", "content": "Hi, I'm bob — a teammate on this team. I'm a generalist coding agent ready to take on whatever work you delegate: writing code, investigating issues, running builds/tests, coordinating with alice and carl, and reporting results back to you. Standing by for tasks.", "type": "message", "ts": 1783255692.3874514, "metadata": {}}]</teammate_messages>
U
L

▶
🔧 send_message
Please introduce yourself. Send a short self-introduction (w…
✓ done
Bob's intro is in. Now triggering carl.正在等待 Carl 的回复，以完成自我介绍。

- [scheduled wakeup] Check bob's reply, then ask carl to introduce itself.
U
L
That wakeup is stale (bob already replied and carl has been triggered). Waiting for carl's intro.

[Teammate messages]
- from: carl, type: message, cc: False
  content: Hi, I'm carl — a teammate on this team. My role is to pick up assigned tasks, execute them with the available tools, coordinate with my peers (alice and bob), and report results back to you. Ready for my first assignment whenever you are.
U
L
All three teammates have introduced themselves. Here's the round-up:

Teammate	Self-introduction
alice	Picks up assigned tasks, collaborates with peers, ships work back reporting progress by milestone.
bob	Generalist coding agent — writes code, investigates issues, runs builds/tests, coordinates with alice and carl.
carl	Picks up assigned tasks, executes them with the available tools, coordinates with peers, reports results.
The team is assembled and all members are standing by. Want me to assign them some work, or run a kickoff task together?
```

而实时刷新时，右边栏的TIMELINE则正常能够刷新所有内容


现在希望的是Lead Session在唤醒时也能在主session对话UI中唤醒，持续刷新消息


