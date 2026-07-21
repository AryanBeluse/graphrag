# Review Feedback: Fixes and Considerations

This update addresses the four issues raised on `feature/tigergraph-chat-history`.

The overall goal was to close the identified gaps without breaking existing behavior, increasing response latency, introducing unbounded memory usage, or blocking legitimate setup operations.

## 1. Critical: `tg_mcp_*` tools bypassed chat-history guards

### Issue

The chat-history guard is implemented in `TigerGraphConnectionProxy`, but the tigergraph-mcp tools use their own raw TigerGraph connection. As a result, these tools could access `Chat*` vertex types and `Chat_*` installed queries without passing through the proxy.

### Fix

Guards were added directly to the wrappers in `graphrag/app/tools/tg_mcp_tools.py`:

- `tg_run_query` checks the query text.
- `tg_get_neighbors` checks vertex, edge, and target type parameters.
- `tg_run_installed_query` blocks `Chat_*` queries and checks all parameters.

Although `tg_run_installed_query` is not currently registered, it is protected for defense in depth.

### Rationale

Blocked calls raise `ChatHistoryAccessDenied`. `tool_registry.run` converts this into an auditable `ok: false` result, matching existing proxy behavior and preventing the agent from interpreting the response as an empty result.

Optional `None` parameters are safely ignored and do not create false positives.

## 2. Critical: `conn.gsql()` was not guarded



### Issue

The proxy protects typed REST methods and `runInstalledQuery`, but not `gsql()`. Generated Cypher queries were executed through `ctx.conn.gsql(...)` and `self.db_connection.gsql(...)`, which allowed queries referencing chat-history types to bypass the guard.

### Fix

Checks were added at the two agent-controlled Cypher execution points:

- `graphrag/app/tools/graphrag_tools.py` in `_cypher_retrieve`
- `graphrag/app/agent/agent_graph.py` in `generate_cypher`

Before execution, generated Cypher is checked with `mentions_chat_history`. Blocked queries are logged and treated as failed generations, allowing the existing retry logic to continue without executing them.

### Rationale

A global guard on `proxy.gsql` would break graph initialization because setup code legitimately installs the chat-history schema and queries through a proxied connection.

Guarding only agent-generated Cypher protects runtime access while leaving setup, migration, and repository operations unchanged.

Blocked generations are handled as normal generation failures rather than raising new exceptions. This preserves existing control flow and avoids introducing uncaught errors.

### Known limitation

The guard also checks generic edge names such as `RETRIEVED`, `HAS_MESSAGE`, and `NEXT_STEP`. A domain graph using the same names could produce a false positive.

This behavior is fail-closed, unlikely, and consistent with the existing proxy guard. Narrowing the check would reduce false positives but weaken defense in depth.

## 3. High: Deployment cleanup was incomplete



### Issue

The root Compose file no longer deployed the Go `chat-history` service, but the service remained in Kubernetes manifests, tutorial files, CI workflows, configuration files, documentation, and the repository itself.

### Fix

All remaining deployment and configuration references were removed from:

- Root and tutorial Kubernetes manifests
- Tutorial Compose files
- Server configuration files
- On-prem CI workflows
- README configuration examples
- The UI setup form

The retired `chat-history/` Go service directory was also deleted.

### Rationale

The `chat_history_api` setting was no longer used by the Python runtime, so removing it does not change application behavior.

CI steps referencing the deleted service directory also had to be removed to prevent build failures.

Historical architecture notes remain because they document the retirement decision rather than deploy the service.

### Existing data

The retired Go service stored history in SQLite, while the new implementation stores it in TigerGraph.

Existing SQLite conversations are not migrated automatically because the old schema does not identify the graph associated with each conversation, and related edges must exist in the graph where the chat occurred.

This does not affect fresh or development deployments, but live deployments with existing SQLite history should be aware of the storage change.

## 4. Medium: Trace writes could be dropped under load



### Issue

`TraceWriter` used a bounded queue of 512 items and a single worker. When the queue filled, traces were dropped with limited visibility.

### Fix

The trace-writing path was improved while remaining non-blocking:

- Default queue size increased to 2048 and made configurable.
- A worker pool, defaulting to four workers, replaced the single worker.
- Each worker uses its own short-lived TigerGraph connection.
- A Prometheus counter was added:
`chat_trace_write_total{outcome=written|dropped|failed}`
- Internal counters are protected by locks.



### Final Consideration

Trace submission runs on the response path and must return immediately.

Blocking when the queue is full would increase user-facing latency. An unbounded queue could cause excessive memory usage or an out-of-memory failure.

The selected approach improves drain capacity and makes any remaining failures observable while preserving bounded memory and non-blocking responses.

## Verification



### Unit and static validation

- All edited Python files compile successfully.
- The full chat-history test suite passes: 170 of 170 tests.
- TraceWriter drain, failure, and drop paths were tested.
- All edited JSON and YAML files were validated.
- One outdated test assertion was updated from `name` to the intended `title` response field.
- A pre-existing trailing comma in `server_config.json.gemini` remains out of scope because the application already tolerates it.



### End-to-end validation

The full Docker stack was rebuilt and tested against `financeDB` using two database users.

The following scenarios passed:

- User and assistant messages persisted with correct sequence and parent relationships.
- Traces were written asynchronously.
- Users could list and access only their own conversations.
- Cross-user conversation access did not reveal existence.
- Feedback could be saved only for owned conversations.
- Trace access remained restricted to authorized roles.
- Proxy and tigergraph-mcp access to `Chat*` data was blocked.
- Chat-history types were hidden from schema and type discovery.
- Repository ownership checks prevented cross-user writes.
- Message search worked correctly.
- Conversation deletion removed related traces.
- Cross-user deletion attempts did not modify data.

Failure handling was also tested with LLM quota errors. The pipeline continued safely, persisted the conversation and trace, and returned a normal fallback response instead of an HTTP 500 error.