# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""Boundary tests for the chat-history guards on the tigergraph-mcp tools.

The guard *functions* are covered by ``test_chat_history_guard``. These tests
cover the *wiring*: that ``tg_run_query`` / ``tg_run_installed_query`` /
``tg_get_neighbors`` actually invoke the guard, refuse conversation
types/queries, and do so **before** reaching the database — so a future
refactor that drops or reorders a guard line is caught here.

Loaded standalone (like ``test_chat_history_tools``) so the heavy ``tools``
package ``__init__`` isn't imported. No live database: the real tigergraph-mcp
call layer is replaced by an async spy, and ``AVAILABLE`` is forced on.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "tools" / "tg_mcp_tools.py"
)
_spec = importlib.util.spec_from_file_location("tg_mcp_tools", _MODULE_PATH)
tg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg)

ChatHistoryAccessDenied = tg.ChatHistoryAccessDenied


class FakeQT:
    """Async stand-in for tigergraph_mcp.tools.query_tools.
    Records every call so a test can assert the database layer was (or was
    not) reached. Returns a trivial JSON body that ``_normalize`` accepts.
    """

    def __init__(self):
        self.calls = []

    async def run_query(self, **kwargs):
        self.calls.append(("run_query", kwargs))
        return "{}"

    async def run_installed_query(self, **kwargs):
        self.calls.append(("run_installed_query", kwargs))
        return "{}"

    async def get_neighbors(self, **kwargs):
        self.calls.append(("get_neighbors", kwargs))
        return "{}"


class FakeConn:
    graphname = "TestGraph"


class FakeCtx:
    def __init__(self):
        self.conn = FakeConn()
        self.tg_connection_config = {"host": "x", "graphname": "TestGraph"}
        self.emitted = []

    def emit(self, msg):
        self.emitted.append(msg)


@pytest.fixture
def ctx():
    return FakeCtx()


@pytest.fixture
def spy(monkeypatch):
    """Force the tools available and swap the mcp call layer for a spy."""
    fake = FakeQT()
    monkeypatch.setattr(tg, "AVAILABLE", True, raising=False)
    monkeypatch.setattr(tg, "_qt", fake, raising=False)
    return fake

class TestRefusal:
    def test_run_query_refuses_conversation_type(self, ctx, spy):
        with pytest.raises(ChatHistoryAccessDenied):
            tg.tg_run_query(ctx, "MATCH (c:ChatConversation) RETURN c")
        assert spy.calls == []  

    def test_run_installed_query_refuses_chat_query_name(self, ctx, spy):
        with pytest.raises(ChatHistoryAccessDenied):
            tg.tg_run_installed_query(ctx, "Chat_List_Conversations", {})
        assert spy.calls == []

    def test_run_installed_query_refuses_chat_type_in_params(self, ctx, spy):
        with pytest.raises(ChatHistoryAccessDenied):
            tg.tg_run_installed_query(ctx, "Some_Query", {"v": "ChatMessage"})
        assert spy.calls == []

    def test_get_neighbors_refuses_conversation_type(self, ctx, spy):
        with pytest.raises(ChatHistoryAccessDenied):
            tg.tg_get_neighbors(ctx, "ChatConversation", "c1")
        assert spy.calls == []



class TestEvasion:
    def test_lowercase_type_is_refused(self, ctx, spy):
        with pytest.raises(ChatHistoryAccessDenied):
            tg.tg_run_query(ctx, "MATCH (c:chatconversation) RETURN c")
        assert spy.calls == []

    def test_get_neighbors_refuses_chat_target_type(self, ctx, spy):
        with pytest.raises(ChatHistoryAccessDenied):
            tg.tg_get_neighbors(
                ctx, "Entity", "e1", target_vertex_type="ChatMessage"
            )
        assert spy.calls == []


class TestAllowThrough:
    def test_run_query_allows_corpus_type(self, ctx, spy):
        res = tg.tg_run_query(ctx, "MATCH (e:Entity) RETURN e")
        assert isinstance(res, dict)
        assert [c[0] for c in spy.calls] == ["run_query"]

    def test_run_installed_query_allows_non_chat(self, ctx, spy):
        res = tg.tg_run_installed_query(ctx, "GraphRAG_Retrieve", {"k": "v"})
        assert isinstance(res, dict)
        assert [c[0] for c in spy.calls] == ["run_installed_query"]

    def test_get_neighbors_allows_corpus_type(self, ctx, spy):
        res = tg.tg_get_neighbors(ctx, "Entity", "e1", edge_type="RELATES_TO")
        assert isinstance(res, dict)
        assert [c[0] for c in spy.calls] == ["get_neighbors"]


class TestUnavailable:
    def test_run_query_unavailable_returns_result(self, ctx, monkeypatch):
        monkeypatch.setattr(tg, "AVAILABLE", False, raising=False)
        res = tg.tg_run_query(ctx, "MATCH (e:Entity) RETURN e")
        assert res["ok"] is False
        assert "unavailable" in res["summary"]
