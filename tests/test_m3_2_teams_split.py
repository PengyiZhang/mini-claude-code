"""M3-2: teams 包拆分 — 新模块落位 + 包级 re-export 面不变。"""
from __future__ import annotations

import inspect

import mini_cc
from mini_cc import teams


def test_new_module_homes():
    from mini_cc.teams.bus import MessageBus, _format_inbox_as_dialogue
    from mini_cc.teams.protocol import ProtocolState, ProtocolTracker
    from mini_cc.teams.spawner import TeammateInfo, TeammateSpawner
    from mini_cc.teams.convention import convention_prompt
    assert callable(convention_prompt)
    assert MessageBus and ProtocolTracker and TeammateSpawner  # noqa


def test_reexport_identity():
    from mini_cc.teams.bus import MessageBus as Bus
    from mini_cc.teams.protocol import ProtocolTracker as Tracker
    from mini_cc.teams.spawner import TeammateSpawner as Spawner
    assert teams.MessageBus is Bus
    assert teams.ProtocolTracker is Tracker
    assert teams.TeammateSpawner is Spawner
    # 顶层包导出仍然可用（mini_cc/__init__.py:50 的导入路径）
    from mini_cc import (MessageBus, ProtocolState, ProtocolTracker,  # noqa
                         TeammateInfo, TeammateSpawner)
    assert mini_cc.TeammateSpawner is Spawner


def test_init_is_thin():
    src = inspect.getsource(teams)
    assert len(src.splitlines()) < 120, (
        "teams/__init__.py 应只留 re-export，不应再承载实现")
