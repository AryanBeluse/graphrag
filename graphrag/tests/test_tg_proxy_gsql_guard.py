# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""Regression tests for the ``gsql`` chat-history guard on the proxy.

``gsql`` executes arbitrary GSQL/openCypher, so it is the one method that can
reach conversation data without a typed accessor. These tests lock in that:

* a default (agent) proxy refuses a ``gsql`` naming a conversation type,
* it still allows ``gsql`` over corpus types,
* ``as_admin()`` lifts the guard for setup/migration,
* the admin twin does not run token/metric cleanup meant for the owner.

The guard itself is real; only import-time infra is mocked so the test needs
no database or metrics backend.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

for _mod in [
    "common.config",
    "common.logs",
    "common.logs.logwriter",
    "common.logs.log",
    "common.metrics.prometheus_metrics",
]:
    sys.modules.setdefault(_mod, MagicMock())

from common.chat_history.guard import ChatHistoryAccessDenied  
from common.metrics.tg_proxy import TigerGraphConnectionProxy  


class FakeConn:
    """Minimal stand-in for a pyTigerGraph connection.

    Records the gsql calls that reach it so a test can assert the guard let a
    call through (or blocked it before it arrived).
    """

    def __init__(self):
        self._req = lambda *a, **k: None
        self.apiToken = ""
        self.graphname = "TestGraph"
        self.gsql_calls = []

    def gsql(self, query, *args, **kwargs):
        self.gsql_calls.append(query)
        return "ok"


_CHAT_CYPHER = (
    "USE GRAPH TestGraph\n"
    "INTERPRET OPENCYPHER QUERY () {\n"
    "MATCH (c:ChatConversation) RETURN c\n"
    "}"
)

_CORPUS_CYPHER = (
    "USE GRAPH TestGraph\n"
    "INTERPRET OPENCYPHER QUERY () {\n"
    "MATCH (e:Entity) RETURN e\n"
    "}"
)


@pytest.fixture
def agent_proxy():
    return TigerGraphConnectionProxy(FakeConn())


class TestAgentGsqlGuard:
    def test_refuses_gsql_naming_conversation_type(self, agent_proxy):
        with pytest.raises(ChatHistoryAccessDenied):
            agent_proxy.gsql(_CHAT_CYPHER)
        assert agent_proxy._tg_connection.gsql_calls == []

    def test_allows_gsql_over_corpus_types(self, agent_proxy):
        assert agent_proxy.gsql(_CORPUS_CYPHER) == "ok"
        assert agent_proxy._tg_connection.gsql_calls == [_CORPUS_CYPHER]

    def test_refusal_is_case_insensitive(self, agent_proxy):
        with pytest.raises(ChatHistoryAccessDenied):
            agent_proxy.gsql(_CHAT_CYPHER.replace("ChatConversation", "chatconversation"))


class TestAsAdminBypass:
    def test_admin_twin_allows_conversation_gsql(self, agent_proxy):
        admin = agent_proxy.as_admin()
        assert admin.gsql(_CHAT_CYPHER) == "ok"
        assert agent_proxy._tg_connection.gsql_calls == [_CHAT_CYPHER]

    def test_default_is_guarded_not_admin(self, agent_proxy):
        assert agent_proxy.admin is False

    def test_as_admin_sets_admin_flag(self, agent_proxy):
        assert agent_proxy.as_admin().admin is True

    def test_admin_twin_shares_underlying_connection(self, agent_proxy):
        assert agent_proxy.as_admin()._tg_connection is agent_proxy._tg_connection

    def test_admin_twin_is_not_the_owner(self, agent_proxy):
        assert agent_proxy._owns_connection is True
        assert agent_proxy.as_admin()._owns_connection is False

    def test_as_admin_on_admin_returns_self(self, agent_proxy):
        admin = agent_proxy.as_admin()
        assert admin.as_admin() is admin

    def test_twin_keeps_owner_alive(self, agent_proxy):
        assert agent_proxy.as_admin()._owner is agent_proxy

    def test_twin_from_throwaway_owner_still_works(self):
        import gc
        twin = TigerGraphConnectionProxy(FakeConn()).as_admin()
        gc.collect()
        assert twin.gsql(_CHAT_CYPHER) == "ok"
        assert twin._owner is not None
