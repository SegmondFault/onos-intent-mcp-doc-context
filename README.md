# ONOS Intent MCP — Document Context Fork

A Skynet research fork of Nenskyy's ONOS Intent MCP. It adds a small,
read-only document catalogue for experiments on how request specificity and
supplied context affect an LLM agent's translation of natural-language
connectivity requests into SDN intents.

The ONOS and Mininet behaviour remains intact. Document context is deliberately
simple: approved UTF-8 Markdown, text, JSON, and CSV files are returned as
bounded text. There is no arbitrary path access, document writing, remote
fetching, embedding model, or vector database.

## Team location

- Workstation checkout: `/home/grayadmin/Projects/onos-intent-mcp-doc-context`
- GitHub: `https://github.com/SegmondFault/onos-intent-mcp-doc-context`
- Upstream: `https://github.com/Nenskyy/onos-intent-mcp`

On `gray-area`:

```bash
cd /home/grayadmin/Projects/onos-intent-mcp-doc-context
uv sync
uv run main.py --selftest
```

![Example call using the MCP](/images/example_mcp_use.png)

It supports:

- **stdio** for desktop MCP clients;
- **Streamable HTTP** for browser clients such as the llama.cpp Web UI;
- host-to-host intents, connectivity checks, and direct link up/down;
- scoped ONOS drop flows for host isolation and simulated website policy.

The repository contains both the MCP server and the topology launcher.

## Quick start

```bash
uv sync
cp .env.example .env
# edit .env: ONOS_URL, credentials, and optional ONOS_CONTROL_URL
uv run main.py --selftest
```

For a browser client, set `ONOS_HTTP=1` in `.env`, for a stdio client, set `ONOS_HTTP=0` and run:

```bash
uv run main.py
```

The MCP endpoint is `http://127.0.0.1:8000/mcp`; health is at
`http://127.0.0.1:8000/health`.

```bash
curl http://127.0.0.1:8000/health
```

Without `ONOS_HTTP=1` or `--http`, the server waits for JSON-RPC on stdin and does not open a port.

## Requirements

- Linux or macOS with [`uv`](https://docs.astral.sh/uv/).
- Python 3.12+ managed by `uv`.
- ONOS northbound REST, usually `http://<onos-host>:8181`.
- Optional control API:
  - The topology control API, usually `http://<topology-host>:9090`.
  - Mininet on the machine running `topology/start_topo.py`.

The MCP server does not start ONOS or the topology. If you only need discovery and host intents, the control API is optional.

## Tools

| Tool | Purpose |
|---|---|
| `get_network_context` | Hosts, switches, links, aliases, intents, flows, and control health. Call first. |
| `list_hosts` | Hosts discovered by ONOS. |
| `list_intents` | Active host-to-host intents, optionally including withdrawn ones. |
| `list_flows` | Priority-100 intent flows grouped by switch. |
| `list_context_documents` | List approved document IDs and metadata, optionally by scenario ID. |
| `read_context_document` | Read a bounded line range from one approved document ID. |
| `create_intent` | Create a host-to-host intent and wait for `INSTALLED`. |
| `remove_intent` | Remove the intent matching two hosts. |
| `connect_nodes` | Connect hosts with an intent, or bring a switch/host-switch link up. |
| `disconnect_nodes` | Remove a host intent, or bring a switch/host-switch link down. |
| `check_connectivity` | Ping one host from another through the control API. |
| `block_traffic` | Install two exact-MAC ONOS drop flows to block a host pair in both directions. |
| `unblock_traffic` | Remove only the matching MCP-managed host-pair drop flows. |
| `list_traffic_blocks` | List active MCP-managed host drop flows. |
| `list_websites` | List configured fixed-IP simulated websites. |
| `set_website_access` | Deny a source host with an ONOS IPv4 drop flow, or allow it by removing that matching deny. |
| `list_website_policies` | List active MCP-managed website deny flows. |
| `check_website_access` | Run a bounded HTTP GET from a Mininet host to a configured simulated website. |
| `set_link` | Bring any direct link up or down through the control API. |

Resources use the `onos://` scheme. Additional reference material is in [`MCP_SERVER.md`](MCP_SERVER.md).

## Document context

The default catalogue is [`context/manifest.json`](context/manifest.json).
Each manifest entry maps a stable document ID to a file beneath `context/` and
records its scenario IDs, category, sensitivity, and authority. The LLM can
list metadata and request text by ID; it cannot submit a filesystem path.

The checked-in `DEMO-01` corpus includes Markdown, JSON, and CSV examples. Its
verification marker is intentionally unique so a smoke test can demonstrate
that the model actually retrieved the document rather than guessed.

Safety boundaries:

- allowed extensions are `.md`, `.txt`, `.json`, and `.csv`;
- manifest paths must remain under the manifest directory and may not use
  symlinks or parent traversal;
- the manifest and each document have a configurable byte limit;
- reads are capped at 500 lines and return a content hash and truncation state;
- returned bodies are labelled as untrusted scenario data, not instructions;
- document ID and line range are logged for experimental provenance.

Use a different approved catalogue with:

```bash
uv run main.py --context-manifest /absolute/path/to/manifest.json
```

### Local Qwen smoke test

With the existing llama.cpp model service listening on loopback, start this MCP
over HTTP in one terminal:

```bash
cd /home/grayadmin/Projects/onos-intent-mcp-doc-context
uv run main.py --http --http-host 127.0.0.1 --http-port 8000
```

Then run the bounded smoke test in another terminal:

```bash
cd /home/grayadmin/Projects/onos-intent-mcp-doc-context
uv run python scripts/smoke_local_llm.py
```

The script exposes only the two document tools, requires the model to list and
read the `DEMO-01` documents, and fails unless it returns the unique marker.
It calls the local llama.cpp OpenAI-compatible endpoint directly; no API key or
cloud service is used.

For the live action experiment, start the MCP with `ONOS_CONTROL_URL` and
`ONOS_WEBSITE_ALIASES`, start the local model, and run:

```bash
uv run python scripts/smoke_network_actions.py \
  --mcp-url http://127.0.0.1:8002/mcp
```

The harness requires the model itself to call the MCP. It verifies baseline,
blocked, and restored packet/HTTP outcomes, records the complete tool trace,
and removes all temporary rules and intents in a `finally` cleanup.

## Host and switch names

Names are resolved against live ONOS inventory:

- hosts become `h<last IPv4 octet>` when unique, for example `h5` for `10.20.0.5`;
- switches become `s<hex datapath number>` when unique, for example `s5` for `of:0000000000000005`;
- IPs, MACs, full ONOS host IDs, and full device IDs also work.

For different names, use aliases:

```bash
uv run main.py \
  --host-alias h5=10.20.0.5 \
  --switch-alias edge=of:0000000000000005
```

Or set `ONOS_HOST_ALIASES` and `ONOS_SWITCH_ALIASES` in `.env`.

## ONOS traffic blocks

`block_traffic` does not disconnect a Mininet link. It installs two temporary,
priority-50123 ONOS rules with exact source/destination MAC selectors and an
empty treatment (`NOACTION`), one for each direction. `unblock_traffic`
removes only rules with that managed priority and exact host pair. Durations
are bounded to 30–3600 seconds and default to 300 seconds.

For the website experiment, define an approved fixed-IP catalogue:

```bash
ONOS_WEBSITE_ALIASES=allowed.example=10.20.0.4,blocked.example=10.20.0.5
```

`set_website_access(..., action="deny")` installs a priority-50124 ingress
drop rule matching the source MAC, IPv4 EtherType, and destination `/32`.
`action="allow"` removes that exact deny rule; it does not create an intent or
otherwise grant connectivity. This is deliberately a fixed-IP HTTP lab
simulation. It is not general Internet access control and does not implement
DNS policy, proxying, HTTPS SNI inspection, URL filtering, or domain-to-IP
tracking.

## ONOS and the control API

The server uses two independent endpoints, both interpreted from the machine where the MCP server runs:

| Setting | Endpoint | Needed for |
|---|---|---|
| `ONOS_URL` | ONOS REST, usually port 8181 | Discovery, intents, flows |
| `ONOS_CONTROL_URL` | Mininet control API, usually port 9090 | Pings, link changes, switch/host-switch connect |

The MCP server does not SSH into the VM. For a ping or link change it sends JSON to the control API, which runs the operation inside Mininet.

If ONOS runs in a VM and the MCP server runs on the host, forward ports 8181 and 9090 and bind the control API to `0.0.0.0`. If the MCP server runs inside the VM, use `127.0.0.1` for both.

Without `ONOS_CONTROL_URL`, these fail with an explanatory error:
`check_connectivity`, `check_website_access`, `set_link`, and switch/host-switch
`connect_nodes`/`disconnect_nodes`. Host-to-host intents and ONOS drop-flow
changes still work.

## Mininet topology launcher

`topology/` is separate from the MCP package. It starts a topology from JSON, connects its switches to ONOS, and optionally starts the control API.

```bash
cd topology
sudo python3 start_topo.py \
  --topology example.json \
  --controller-ip 127.0.0.1 \
  --controller-port 6653 \
  --control-host 0.0.0.0 \
  --control-port 9090
```

Use `--clean` to run `mn -c` first, `--hold N` to run without the interactive CLI, and `--control-port 0` to disable the control API. Stop with Ctrl-C so Mininet can clean its interfaces.

The example launcher keeps `autoStaticArp=False` so ONOS can observe initial
discovery traffic. After ONOS has discovered the hosts, run `py net.staticArp()`
at the Mininet prompt before deterministic intent/connectivity experiments.
Otherwise an installed unicast intent can appear healthy while the first ARP
exchange remains unresolved.

See [`topology/README.md`](topology/README.md) for the JSON schema and [`topology/example.json`](topology/example.json) for a complete example.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ONOS_URL` | `http://127.0.0.1:8181` | ONOS northbound URL |
| `ONOS_USER` | `onos` | ONOS REST username |
| `ONOS_PASSWORD` | `rocks` | ONOS REST password |
| `ONOS_APP_ID` | `org.onosproject.cli` | Intent application ID |
| `ONOS_CONTROL_URL` | empty | Mininet control API URL; empty disables it |
| `ONOS_CONTEXT_MANIFEST` | bundled demo manifest | Approved document catalogue |
| `ONOS_CONTEXT_MAX_BYTES` | `262144` | Maximum bytes per manifest or document |
| `ONOS_HOST_ALIASES` | empty | Host aliases, `name=IP/MAC/ID,...` |
| `ONOS_SWITCH_ALIASES` | empty | Switch aliases, `name=device-ID,...` |
| `ONOS_WEBSITE_ALIASES` | empty | Simulated website catalogue, `name=IPv4,...` |
| `ONOS_HTTP` | off | Set to `1` to start Streamable HTTP |
| `ONOS_HTTP_HOST` | `127.0.0.1` | HTTP bind address |
| `ONOS_HTTP_PORT` | `8000` | HTTP port |
| `ONOS_HTTP_PATH` | `/mcp` | Streamable HTTP path |
| `ONOS_CORS_ORIGINS` | `*` | Comma-separated allowed CORS origins |
| `ONOS_ENV_FILE` | empty | Explicit env file; `.env` is automatic |

`.env` is loaded from the current directory or project root. Existing shell variables take precedence. Use `--env-file PATH` or `ONOS_ENV_FILE` for a different file. Equivalent flags include `--onos-url`, `--onos-user`, `--onos-password`, `--app-id`, `--control-url`, `--http`, `--http-host`, `--http-port`, `--http-path`, and `--cors-origin`.

## MCP client configuration

Stdio:

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
      }
    }
  }
}
```

HTTP browser client: start the server with `ONOS_HTTP=1` or `--http`, then add the Streamable HTTP URL:

```text
http://<mcp-server-host>:8000/mcp
```

Use `/mcp`, not `/sse`. If the browser runs on another machine, bind `--http-host 0.0.0.0` and allow the port through the firewall.

## Testing

```bash
uv run python -m unittest -v \
  tests.test_topology_config \
  tests.test_topology_control \
  tests.test_http_transport \
  tests.test_mcp_server \
  tests.test_document_context \
  tests.test_intent_tools
```

Expected: 46 tests, with the live ONOS test skipped unless `ONOS_LIVE=1`.

```bash
ONOS_LIVE=1 uv run python -m unittest -v tests.test_intent_tools
```

## Troubleshooting

- **`uv run main.py` prints nothing**: it is in stdio mode, waiting on stdin. Set `ONOS_HTTP=1` or use `--http`.
- **Browser `NetworkError` or `Disallowed CORS origin`**: set `ONOS_CORS_ORIGINS=*` or the exact Web UI origin, then restart the server.
- **No hosts**: ONOS has not discovered them yet. Generate traffic and call `get_network_context` again.
- **Intent is installed but ping still fails**: after discovery, populate deterministic ARP entries with `py net.staticArp()` in the Mininet CLI.
- **Control disabled**: set `ONOS_CONTROL_URL` and restart the MCP server.
- **Control API connection refused**: confirm the topology is running and the port is reachable from the MCP server.

## Security

- The HTTP MCP endpoint has no authentication. Bind `127.0.0.1` unless a remote client needs it; otherwise use a firewall or a TLS reverse proxy.
- The control API also has no authentication. Do not expose it to an untrusted network.
- Keep `.env` private; it is ignored by git.
- Change the default ONOS credentials if your controller uses different ones.
