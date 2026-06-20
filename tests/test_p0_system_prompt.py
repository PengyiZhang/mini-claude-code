"""P0 system_prompt + compaction tests."""
from __future__ import annotations

from pathlib import Path

from mini_cc.core.compaction import (compact_history, estimate_size,
                                     prepare_context, snip_compact,
                                     tool_result_budget)
from mini_cc.core.system_prompt import assemble_system_prompt


def test_system_prompt_includes_project_root(tmp_path):
    p = assemble_system_prompt(
        project_root=tmp_path,
        tools=[],
        memories="",
        mcp_servers=[],
        skills_catalog="",
    )
    assert str(tmp_path) in p
    assert "Working directory" in p


def test_system_prompt_includes_memory_when_present(tmp_path):
    p = assemble_system_prompt(
        project_root=tmp_path, tools=[], memories="user prefers terse",
        mcp_servers=[], skills_catalog="")
    assert "user prefers terse" in p


def test_system_prompt_omits_memory_when_empty(tmp_path):
    p = assemble_system_prompt(
        project_root=tmp_path, tools=[], memories="",
        mcp_servers=[], skills_catalog="")
    assert "Relevant memories" not in p


def test_estimate_size():
    msgs = [{"role": "user", "content": "abcd"}]
    # ~4 chars per token -> 1 token
    assert estimate_size(msgs) >= 1


def test_tool_result_budget_truncates_old():
    msgs = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu1", "name": "bash", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1",
             "content": "x" * 5000}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu2", "name": "bash", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu2",
             "content": "y" * 100}]},
    ]
    out = tool_result_budget(msgs, keep=1)
    # tu1 result is truncated, tu2 result is preserved (it's the recent one)
    blocks = out[1]["content"]
    assert "[...truncated by compaction]" in blocks[0]["content"]
    assert out[3]["content"][0]["content"] == "y" * 100


def test_compact_history_keeps_recent():
    msgs = [{"role": "user", "content": str(i)} for i in range(20)]
    out = compact_history(msgs, keep_recent=4)
    assert len(out) == 5  # summary + 4 kept
    assert "compacted" in out[0]["content"].lower()


def test_prepare_context_under_limit():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"}]
    prepare_context(msgs)
    # small context should pass through unchanged
    assert msgs[0]["content"] == "hi"
