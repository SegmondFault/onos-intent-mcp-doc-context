# ONOS MCP Server Reference

Supplement to [`README.md`](README.md): resource and prompt details, internal layout, API contract, native llama.cpp configuration, and the Python 3.8 fallback.

## Resources and prompts

Resources use the `onos://` scheme:

| URI | Contents |
|---|---|
| `onos://network/context` | Current network snapshot as JSON. |
| `onos://hosts` | ONOS host inventory as JSON. |
| `onos://intents` | Active and historical intents as JSON. |
| `onos://flows` | Priority-100 intent flows as JSON. |
| `onos://runbook` | Markdown runbook for common workflows. |

Prompts:

| Prompt | Arguments | Purpose |
|---|---|---|
| `connect_hosts` | `source`, `destination` | Connect two hosts and verify the intent. |
| `failure_demo` | `source`, `destination`, `link` | Create an intent, fail a link, observe recovery, restore. |
| `explain_network` | none | Explain the current snapshot without changing it. |
| `cleanup_intents` | none | List intents and ask for confirmation before removing. |

## Architecture

```text
MCP client (stdio)  or  browser client (Streamable HTTP :8000/mcp)
   |
   | MCP over stdin/stdout or HTTP
   v
main.py -> src/onos_mcp/server.py
   |
   +-- src/onos_mcp/document_context.py
   |       +--> approved UTF-8 files under the manifest directory
   |
   +-- src/onos_mcp/intent_core.py: NodeResolver / IntentManager / OnosClient
   |       +--> ONOS REST API :8181
   |
   +-- src/onos_mcp/core.py
           +--> optional Mininet control API :9090
```

The MCP server does not run ONOS or the topology. Host-to-host `connect_nodes`/`disconnect_nodes` use ONOS intents. `check_connectivity`, `set_link`, and switch/host-switch link operations use the control API.

## llama.cpp native stdio

llama.cpp's native MCP process configuration is stdio-only:

```json
{
  "mcpServers": {
    "onos": {
      "command": "uv",
      "args": ["run", "--directory", "<project-root>", "main.py"],
      "env": {
        "ONOS_URL": "http://<onos-host>:8181",
        "ONOS_USER": "onos",
        "ONOS_PASSWORD": "rocks",
        "ONOS_APP_ID": "org.onosproject.cli",
        "ONOS_CONTROL_URL": "http://<topology-host>:9090"
      },
      "timeout_ms": 120000
    }
  }
}
```

Tools are exposed as `<server>_<tool>`, for example `onos_get_network_context`.

## ONOS REST

The server reads:

- `/onos/v1/hosts`
- `/onos/v1/devices`
- `/onos/v1/links`
- `/onos/v1/intents`
- `/onos/v1/flows`
- `/onos/v1/applications`

It creates and deletes `HostToHostIntent`s through `/onos/v1/intents`.

## Control API contract

| Method | Path | Meaning |
|---|---|---|
| `GET` | `/health` | Health/readiness response |
| `GET` | `/status` | Hosts, links, and switch names |
| `POST` | `/command` | Run `status`, `ping`, or `link` |

Ping request:

```json
{"command": "ping", "source": "h1", "destination": "h2", "count": 3}
```

Link request:

```json
{"command": "link", "left": "s1", "right": "s2", "state": "down"}
```

The API accepts any direct link in the running topology, including host-switch links. It has no authentication.

## Python 3.8 fallback

The fallback is stdio-only and uses no third-party dependencies:

```bash
PYTHONPATH=src python3 -m onos_mcp.stdlib_server --selftest
PYTHONPATH=src python3 -m onos_mcp.stdlib_server \
  --control-url http://<topology-host>:9090
```

## Files

| File | Purpose |
|---|---|
| `main.py` | Entrypoint; selects stdio or Streamable HTTP |
| `src/onos_mcp/server.py` | Primary MCP server using the official SDK |
| `src/onos_mcp/document_context.py` | Approved manifest and bounded text reads |
| `src/onos_mcp/core.py` | Tool implementations and stdlib fallback core |
| `src/onos_mcp/stdlib_server.py` | Python 3.8 stdio fallback entrypoint |
| `src/onos_mcp/intent_core.py` | ONOS client, resolver, intents, conflicts |
| `tests/` | Offline tests and optional live ONOS test |
| `topology/` | Mininet launcher, control API, and example JSON |
| `context/` | Demo manifest and approved Markdown, JSON, and CSV files |
| `scripts/smoke_local_llm.py` | Local Qwen-to-MCP document retrieval smoke test |
| `.env.example` | Example environment configuration |
