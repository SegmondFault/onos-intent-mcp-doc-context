#!/usr/bin/env python3
"""Core ONOS/Mininet logic and the stdlib MCP fallback server.

The server exposes tools, resources, and prompts over the Model Context
Protocol (MCP).  An LLM client can inspect any ONOS-discovered topology,
create/remove HostToHostIntents through ONOS, and optionally ping hosts or
change a link through the local Mininet control API.

No third-party Python packages are required.  This is the Python 3.8 fallback;
the primary server is src/onos_mcp/server.py, which uses the official MCP SDK
(MCPServer, formerly FastMCP in SDK v1).

Typical use from the project root:
    PYTHONPATH=src python3 -m onos_mcp.stdlib_server --selftest
    PYTHONPATH=src python3 -m onos_mcp.stdlib_server --control-url http://127.0.0.1:9090
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from .intent_core import (
    DEFAULT_APP_ID,
    DEFAULT_ONOS_PASSWORD,
    DEFAULT_ONOS_URL,
    DEFAULT_ONOS_USER,
    ConflictError,
    IntentManager,
    NodeResolver,
    OnosClient,
    OnosError,
    load_env,
    parse_aliases,
)

SERVER_NAME = "onos-intent-mcp-doc-context"
SERVER_VERSION = "1.1.0"
SUPPORTED_PROTOCOLS = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
    "2026-07-28",
)
ACTIVE_STATES = ("INSTALLED", "INSTALLING", "REINSTALLING")

INSTRUCTIONS = """\
You are an intent-network assistant for an ONOS-managed network.

The server discovers hosts and switches from ONOS.  Host names may be
``h<last IPv4 octet>``, an explicit alias, an IP address, a MAC address, or a
full ONOS host ID.  Switch names may be ``s<datapath number>``, an explicit
alias, or a full ONOS device ID.  Always call get_network_context first and use
the names and links it returns.

Rules:
1. To connect two hosts, use connect_nodes or create_intent.  To connect two
   switches, or a host and a switch, use connect_nodes; it brings the
   corresponding link up through the control API.
2. To disconnect two hosts, use disconnect_nodes or remove_intent.  To take a
   switch link down, or disconnect a host from a switch, use disconnect_nodes;
   it brings the corresponding link down.
3. A duplicate intent endpoint pair is always rejected.  A shared-endpoint
   overlap is rejected unless force=true; only use force when the user
   explicitly asks for an overlapping intent.
4. After a change, verify with list_intents, list_flows, get_network_context,
   or check_connectivity.  Only report success when the intent state is
   INSTALLED or the link state is ``up``/``down`` as requested.
5. If a tool returns an error, report the exact error and the current network
   context instead of guessing.  If a name is unknown, ask for an alias or use
   get_network_context to see the discovered names.
"""

RUNBOOK = """\
# ONOS MCP runbook

## Inspect
- get_network_context
- list_hosts
- list_intents
- list_flows

## Connect hosts
1. get_network_context
2. connect_nodes(source="h1", destination="h5")
3. list_intents
4. check_connectivity(source="h1", destination="h5")

## Disconnect hosts
1. list_intents
2. disconnect_nodes(source="h1", destination="h5")
3. list_intents

## Change a link
1. get_network_context
2. disconnect_nodes(source="s2", destination="s5")   # link down
3. connect_nodes(source="s2", destination="s5")      # link up

## Failure and recovery
1. connect_nodes(source="h1", destination="h5")
2. check_connectivity(source="h1", destination="h5")
3. disconnect_nodes(source="s1", destination="s2")
4. check_connectivity(source="h1", destination="h5")
5. list_flows
6. connect_nodes(source="s1", destination="s2")
7. check_connectivity(source="h1", destination="h5")

## Notes
- Host and switch names are discovered from ONOS.  Add explicit aliases with
  --host-alias and --switch-alias when the automatic names do not match the
  names used by your topology or control API.
- The control API is optional.  If it is disabled, intent tools still work;
  ping and link tools return an explanatory error.
"""


class ToolError(Exception):
    """Raised for invalid tool arguments or unavailable services."""


def json_text(value):
    return json.dumps(value, indent=2, sort_keys=True)


def _arg(args, name):
    value = args.get(name)
    if value is None or str(value).strip() == "":
        raise ToolError("missing required argument %r" % name)
    return str(value).strip()


def _bool_arg(args, name, default=False):
    value = args.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _number_arg(args, name, default, minimum, maximum):
    value = args.get(name, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ToolError("%s must be a number" % name)
    if not minimum <= value <= maximum:
        raise ToolError(
            "%s must be between %s and %s" % (name, minimum, maximum)
        )
    return value


def _int_arg(args, name, default, minimum, maximum):
    value = _number_arg(args, name, default, minimum, maximum)
    return int(value)


class OnosMCPCore:
    """MCP server implementation with a small set of network tools."""

    def __init__(
        self,
        onos_url,
        app_id,
        control_url,
        client=None,
        host_aliases=None,
        switch_aliases=None,
        onos_user=DEFAULT_ONOS_USER,
        onos_password=DEFAULT_ONOS_PASSWORD,
    ):
        self.onos_url = onos_url.rstrip("/")
        self.app_id = app_id
        self.control_url = (control_url or "").rstrip("/")
        self.client = client or OnosClient(
            base_url=self.onos_url,
            user=onos_user,
            password=onos_password,
        )
        self.host_aliases = dict(host_aliases or {})
        self.switch_aliases = dict(switch_aliases or {})
        self.manager = IntentManager(
            client=self.client,
            app_id=self.app_id,
            host_ips=self.host_aliases,
            switch_aliases=self.switch_aliases,
        )
        self.tools = self._tool_definitions()
        self.resources = self._resource_definitions()
        self.prompts = self._prompt_definitions()

    # ------------------------------------------------------------------
    # MCP definitions
    # ------------------------------------------------------------------
    def _tool_definitions(self):
        return [
            {
                "name": "get_network_context",
                "description": (
                    "Read the current ONOS/Mininet context. Call this first. "
                    "Returns discovered hosts, switches, links, aliases, "
                    "active intents, flow counts, and control API health."
                ),
                "inputSchema": self._schema({}),
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "list_hosts",
                "description": (
                    "List all hosts discovered by ONOS with their generated "
                    "names, aliases, IPs, MACs, and attachment points."
                ),
                "inputSchema": self._schema({}),
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "list_intents",
                "description": (
                    "List HostToHostIntent records. By default, only active "
                    "intents are shown; set include_withdrawn=true to include "
                    "historical WITHDRAWN records."
                ),
                "inputSchema": self._schema(
                    {
                        "include_withdrawn": {
                            "type": "boolean",
                            "description": "Include WITHDRAWN records.",
                            "default": False,
                        }
                    }
                ),
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "list_flows",
                "description": (
                    "List priority-100 intent flows currently installed in "
                    "ONOS, grouped by switch."
                ),
                "inputSchema": self._schema({}),
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "create_intent",
                "description": (
                    "Create a HostToHostIntent between any two hosts "
                    "discovered by ONOS. A host may be named hN, an explicit "
                    "alias, an IP, a MAC, or an ONOS host ID. Rejects "
                    "duplicates and shared-endpoint overlaps unless "
                    "force=true. Waits for INSTALLED."
                ),
                "inputSchema": self._schema(
                    {
                        "source": {
                            "type": "string",
                            "description": (
                                "Source host name, alias, IP, MAC, or ONOS "
                                "host ID."
                            ),
                        },
                        "destination": {
                            "type": "string",
                            "description": (
                                "Destination host name, alias, IP, MAC, or "
                                "ONOS host ID."
                            ),
                        },
                        "force": {
                            "type": "boolean",
                            "description": "Allow a shared-endpoint overlap.",
                            "default": False,
                        },
                        "timeout": {
                            "type": "number",
                            "description": "Seconds to wait for INSTALLED.",
                            "default": 25,
                        },
                    },
                    ["source", "destination"],
                ),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": False,
                },
            },
            {
                "name": "remove_intent",
                "description": (
                    "Remove the active HostToHostIntent matching any two "
                    "discovered host names or aliases. Other intents are not "
                    "touched."
                ),
                "inputSchema": self._schema(
                    {
                        "source": {
                            "type": "string",
                            "description": "One endpoint host name.",
                        },
                        "destination": {
                            "type": "string",
                            "description": "The other endpoint host name.",
                        },
                    },
                    ["source", "destination"],
                ),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": True,
                },
            },
            {
                "name": "connect_nodes",
                "description": (
                    "Connect any two discovered nodes. Two hosts create a "
                    "HostToHostIntent. Two switches, or a host and a switch, "
                    "bring their direct link up through the control API. "
                    "Accepts hN/sN names, aliases, IPs, MACs, and ONOS IDs."
                ),
                "inputSchema": self._schema(
                    {
                        "source": {
                            "type": "string",
                            "description": "First node name or alias.",
                        },
                        "destination": {
                            "type": "string",
                            "description": "Second node name or alias.",
                        },
                        "force": {
                            "type": "boolean",
                            "description": "Allow a host intent overlap.",
                            "default": False,
                        },
                        "timeout": {
                            "type": "number",
                            "description": (
                                "Seconds to wait for a host intent to reach "
                                "INSTALLED."
                            ),
                            "default": 25,
                        },
                    },
                    ["source", "destination"],
                ),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": False,
                },
            },
            {
                "name": "disconnect_nodes",
                "description": (
                    "Disconnect any two discovered nodes. Two hosts remove "
                    "their HostToHostIntent. Two switches, or a host and a "
                    "switch, bring their direct link down through the "
                    "control API. Accepts hN/sN names, aliases, IPs, MACs, "
                    "and ONOS IDs."
                ),
                "inputSchema": self._schema(
                    {
                        "source": {
                            "type": "string",
                            "description": "First node name or alias.",
                        },
                        "destination": {
                            "type": "string",
                            "description": "Second node name or alias.",
                        },
                    },
                    ["source", "destination"],
                ),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": True,
                },
            },
            {
                "name": "check_connectivity",
                "description": (
                    "Ping one host from another inside Mininet using the "
                    "optional control API. Names must match the control API "
                    "hosts; destinations may also be IP addresses."
                ),
                "inputSchema": self._schema(
                    {
                        "source": {
                            "type": "string",
                            "description": "Source host name or alias.",
                        },
                        "destination": {
                            "type": "string",
                            "description": "Destination host name or IP.",
                        },
                        "count": {
                            "type": "integer",
                            "description": "Number of ping packets, 1-10.",
                            "default": 3,
                            "minimum": 1,
                            "maximum": 10,
                        },
                    },
                    ["source", "destination"],
                ),
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "set_link",
                "description": (
                    "Bring any direct link up or down through the optional "
                    "Mininet control API. Examples: s1-s2, h1-s1."
                ),
                "inputSchema": self._schema(
                    {
                        "link": {
                            "type": "string",
                            "description": (
                                "Link endpoints separated by '-', for "
                                "example s1-s2 or h1-s1."
                            ),
                        },
                        "state": {
                            "type": "string",
                            "enum": ["up", "down"],
                            "description": "Desired link state.",
                        },
                    },
                    ["link", "state"],
                ),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": True,
                },
            },
        ]

    def _resource_definitions(self):
        return [
            {
                "uri": "onos://network/context",
                "name": "Network context",
                "description": "Current hosts, switches, links, and intents.",
                "mimeType": "application/json",
            },
            {
                "uri": "onos://hosts",
                "name": "Hosts",
                "description": "All hosts discovered by ONOS.",
                "mimeType": "application/json",
            },
            {
                "uri": "onos://intents",
                "name": "Intents",
                "description": "Active and historical HostToHostIntents.",
                "mimeType": "application/json",
            },
            {
                "uri": "onos://flows",
                "name": "Intent flows",
                "description": "Priority-100 intent flows installed in ONOS.",
                "mimeType": "application/json",
            },
            {
                "uri": "onos://runbook",
                "name": "Runbook",
                "description": "Suggested tool sequences for common tasks.",
                "mimeType": "text/markdown",
            },
        ]

    def _prompt_definitions(self):
        return [
            {
                "name": "connect_hosts",
                "description": "Connect two hosts and verify the intent.",
                "arguments": [
                    {
                        "name": "source",
                        "description": "Source host name.",
                        "required": True,
                    },
                    {
                        "name": "destination",
                        "description": "Destination host name.",
                        "required": True,
                    },
                ],
            },
            {
                "name": "failure_demo",
                "description": (
                    "Create an intent, fail a link, observe recovery, and "
                    "restore the link."
                ),
                "arguments": [
                    {
                        "name": "source",
                        "description": "Source host name.",
                        "required": True,
                    },
                    {
                        "name": "destination",
                        "description": "Destination host name.",
                        "required": True,
                    },
                    {
                        "name": "link",
                        "description": "Ring link to fail, for example s1-s2.",
                        "required": True,
                    },
                ],
            },
            {
                "name": "explain_network",
                "description": "Summarize the current network and active intents.",
                "arguments": [],
            },
            {
                "name": "cleanup_intents",
                "description": "Remove active intents after confirmation.",
                "arguments": [],
            },
        ]

    @staticmethod
    def _schema(properties, required=None):
        return {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        }

    # ------------------------------------------------------------------
    # ONOS and control helpers
    # ------------------------------------------------------------------
    def _devices(self):
        devices_method = getattr(self.client, "devices", None)
        if callable(devices_method):
            return devices_method()
        return []

    def _resolver(self, hosts=None, devices=None):
        return NodeResolver(
            self.client.hosts() if hosts is None else hosts,
            self._devices() if devices is None else devices,
            host_aliases=self.host_aliases,
            switch_aliases=self.switch_aliases,
        )

    def _host_rows(self):
        hosts = self.client.hosts()
        resolver = self._resolver(hosts=hosts)
        rows = []
        for host in hosts:
            location = (host.get("locations") or [{}])[0]
            rows.append(
                {
                    "name": resolver.host_name(host),
                    "id": host.get("id"),
                    "mac": host.get("mac"),
                    "ips": host.get("ipAddresses") or [],
                    "switch": resolver.device_name(
                        location.get("elementId")
                    ),
                    "port": location.get("port"),
                }
            )
        return rows

    def _link_rows(self):
        resolver = self._resolver()
        pairs = {}
        for link in self.client.links():
            src = link.get("src") or {}
            dst = link.get("dst") or {}
            src_device = src.get("device")
            dst_device = dst.get("device")
            if not src_device or not dst_device:
                continue
            key = tuple(sorted((src_device, dst_device)))
            if key not in pairs:
                pairs[key] = {
                    "left": resolver.device_name(key[0]),
                    "right": resolver.device_name(key[1]),
                    "state": link.get("state"),
                    "directed_records": 0,
                }
            pairs[key]["directed_records"] += 1
        return sorted(pairs.values(), key=lambda item: item["left"])

    def _flow_rows(self):
        resolver = self._resolver()
        rows = []
        for flow in self.client.flows():
            try:
                priority = int(flow.get("priority"))
            except (TypeError, ValueError):
                continue
            if priority != 100:
                continue
            rows.append(
                {
                    "device": resolver.device_name(flow.get("deviceId")),
                    "id": flow.get("id"),
                    "state": flow.get("state"),
                    "priority": priority,
                    "selector": flow.get("selector"),
                    "treatment": flow.get("treatment"),
                    "bytes": flow.get("bytes"),
                    "packets": flow.get("packets"),
                    "duration": flow.get("duration"),
                }
            )
        return rows

    def _intent_rows(self):
        rows = []
        for row in self.manager.list_intents():
            row = dict(row)
            row["active"] = str(row.get("state", "")).upper() in ACTIVE_STATES
            rows.append(row)
        return rows

    def _flow_summary(self, flows):
        counts = {}
        for flow in flows:
            device = flow["device"]
            counts[device] = counts.get(device, 0) + 1
        return counts

    def control_health(self):
        if not self.control_url:
            return {
                "enabled": False,
                "reason": "set ONOS_CONTROL_URL or --control-url",
            }
        try:
            with urllib.request.urlopen(
                self.control_url + "/health", timeout=3
            ) as response:
                body = response.read().decode("utf-8", "replace")
            return {
                "enabled": True,
                "ok": True,
                "url": self.control_url,
                "status": json.loads(body),
            }
        except Exception as error:  # noqa: BLE001
            return {
                "enabled": True,
                "ok": False,
                "url": self.control_url,
                "error": str(error),
            }

    def control_command(self, payload):
        if not self.control_url:
            raise ToolError(
                "Mininet control API is disabled; start your topology with "
                "its control API enabled and set ONOS_CONTROL_URL"
            )
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.control_url + "/command",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            raise ToolError(
                "control API returned HTTP %s: %s" % (error.code, body[:400])
            )
        except urllib.error.URLError as error:
            raise ToolError(
                "cannot reach Mininet control API at %s: %s"
                % (self.control_url, error)
            )
        try:
            parsed = json.loads(body)
        except ValueError:
            raise ToolError("control API returned non-JSON: %s" % body[:400])
        if not parsed.get("ok") and parsed.get("error"):
            raise ToolError(parsed["error"])
        return parsed

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------
    def get_network_context(self):
        context = {
            "ok": True,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "onos_url": self.onos_url,
            "app_id": self.app_id,
            "control_url": self.control_url or None,
            "control": self.control_health(),
        }
        try:
            devices = self._devices()
            resolver = self._resolver(devices=devices)
            hosts = self._host_rows()
            links = self._link_rows()
            intents = self._intent_rows()
            flows = self._flow_rows()
        except OnosError as error:
            context["ok"] = False
            context["error"] = str(error)
            return context

        active = [row for row in intents if row["active"]]
        context.update(
            {
                "hosts": hosts,
                "switches": [
                    {
                        "name": resolver.switch_name(device),
                        "id": device.get("id"),
                        "available": device.get("available"),
                        "type": device.get("type"),
                    }
                    for device in devices
                ],
                "links": links,
                "aliases": {
                    "hosts": dict(self.host_aliases),
                    "switches": dict(self.switch_aliases),
                },
                "active_intents": active,
                "intent_flows": flows,
                "intent_flow_counts": self._flow_summary(flows),
                "summary": (
                    "%d hosts, %d switches, %d links, "
                    "%d active intents, %d priority-100 flows"
                    % (
                        len(hosts),
                        len(devices),
                        len(links),
                        len(active),
                        len(flows),
                    )
                ),
            }
        )
        return context

    def list_hosts(self):
        return {"ok": True, "hosts": self._host_rows()}

    def list_intents(self, include_withdrawn=False):
        rows = self._intent_rows()
        if not include_withdrawn:
            rows = [row for row in rows if row["active"]]
        return {"ok": True, "intents": rows}

    def list_flows(self):
        rows = self._flow_rows()
        return {
            "ok": True,
            "flows": rows,
            "counts": self._flow_summary(rows),
        }

    def create_intent(self, source, destination, force=False, timeout=25):
        timeout = float(timeout)
        if not 1 <= timeout <= 60:
            raise ToolError("timeout must be between 1 and 60 seconds")
        result = self.manager.submit(
            "connect %s to %s" % (source, destination),
            force=force,
        )
        wanted = {result["one"], result["two"]}
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            for row in self.manager.list_intents():
                if {row.get("one"), row.get("two")} == wanted:
                    last = row
                    if str(row.get("state", "")).upper() == "INSTALLED":
                        return {
                            "ok": True,
                            "source": source,
                            "destination": destination,
                            "http_status": result.get("status"),
                            "forced": bool(result.get("forced")),
                            "intent": row,
                            "conflicts": result.get("conflicts", []),
                        }
            time.sleep(1)
        return {
            "ok": False,
            "source": source,
            "destination": destination,
            "error": "intent did not reach INSTALLED before timeout",
            "last_seen": last,
            "http_status": result.get("status"),
        }

    def remove_intent(self, source, destination):
        result = self.manager.disconnect(
            "disconnect %s from %s" % (source, destination)
        )
        return {
            "ok": True,
            "source": source,
            "destination": destination,
            "removed": result.get("deleted", []),
        }

    def check_connectivity(self, source, destination, count=3):
        count = int(count)
        return self.control_command(
            {
                "command": "ping",
                "source": source,
                "destination": destination,
                "count": count,
            }
        )

    def _split_link(self, link):
        match = re.match(r"^(.+?)\s*-\s*(.+)$", str(link or "").strip())
        if not match:
            raise ToolError("link must look like s1-s2 or h1-s1")
        left, right = match.group(1).strip(), match.group(2).strip()
        if not left or not right or left == right:
            raise ToolError("link endpoints must be two different nodes")
        return left, right

    def _set_link_pair(self, left, right, state):
        state = str(state).lower()
        if state not in ("up", "down"):
            raise ToolError("state must be 'up' or 'down'")
        return self.control_command(
            {
                "command": "link",
                "left": left,
                "right": right,
                "state": state,
            }
        )

    def set_link(self, link, state):
        left, right = self._split_link(link)
        return self._set_link_pair(left, right, state)

    def connect_nodes(self, source, destination, force=False, timeout=25):
        resolver = self._resolver()
        src_kind, src_node, src_name = resolver.resolve_node(source)
        dst_kind, dst_node, dst_name = resolver.resolve_node(destination)
        if src_node is dst_node:
            raise ToolError("source and destination are the same node")

        if src_kind == "host" and dst_kind == "host":
            result = self.create_intent(
                src_name,
                dst_name,
                force=force,
                timeout=timeout,
            )
            result["mode"] = "intent"
            return result

        result = self._set_link_pair(src_name, dst_name, "up")
        result["mode"] = "link"
        return result

    def disconnect_nodes(self, source, destination):
        resolver = self._resolver()
        src_kind, src_node, src_name = resolver.resolve_node(source)
        dst_kind, dst_node, dst_name = resolver.resolve_node(destination)
        if src_node is dst_node:
            raise ToolError("source and destination are the same node")

        if src_kind == "host" and dst_kind == "host":
            result = self.remove_intent(src_name, dst_name)
            result["mode"] = "intent"
            return result

        result = self._set_link_pair(src_name, dst_name, "down")
        result["mode"] = "link"
        return result

    # ------------------------------------------------------------------
    # Resource and prompt implementations
    # ------------------------------------------------------------------
    def read_resource(self, uri):
        if uri == "onos://network/context":
            return json_text(self.get_network_context()), "application/json"
        if uri == "onos://hosts":
            return json_text(self.list_hosts()), "application/json"
        if uri == "onos://intents":
            return json_text(self.list_intents(include_withdrawn=True)), (
                "application/json"
            )
        if uri == "onos://flows":
            return json_text(self.list_flows()), "application/json"
        if uri == "onos://runbook":
            return RUNBOOK, "text/markdown"
        raise ToolError("unknown resource %r" % uri)

    def get_prompt(self, name, arguments):
        if name == "connect_hosts":
            source = _arg(arguments, "source")
            destination = _arg(arguments, "destination")
            text = (
                "Connect %s to %s in the ONOS network.\n"
                "1. Call get_network_context and verify both host names.\n"
                "2. Call connect_nodes(source=%r, destination=%r).\n"
                "3. Verify the intent state is INSTALLED.\n"
                "4. If available, call check_connectivity.\n"
                "5. Report the intent id, state, and any conflict.\n"
                "Do not use force unless I explicitly ask for an overlap.\n"
                % (source, destination, source, destination)
            )
            return text
        if name == "failure_demo":
            source = _arg(arguments, "source")
            destination = _arg(arguments, "destination")
            link = _arg(arguments, "link")
            text = (
                "Demonstrate failure recovery between %s and %s.\n"
                "1. Call get_network_context.\n"
                "2. Connect the %s-%s intent and verify it is INSTALLED.\n"
                "3. Run check_connectivity to establish a baseline.\n"
                "4. Call disconnect_nodes(source=%r, destination=%r) or "
                "set_link(link=%r, state='down').\n"
                "5. Poll check_connectivity and list_flows until traffic "
                "recovers over an alternate path.\n"
                "6. Call connect_nodes(source=%r, destination=%r) or "
                "set_link(link=%r, state='up').\n"
                "7. Report the recovery time and explain that ONOS does not "
                "automatically reoptimize an installed intent.\n"
                % (
                    source,
                    destination,
                    source,
                    destination,
                    source,
                    destination,
                    link,
                    source,
                    destination,
                    link,
                )
            )
            return text
        if name == "explain_network":
            return (
                "Call get_network_context and explain the hosts, switches, "
                "links, active intents, and current priority-100 flow "
                "distribution. Do not change anything."
            )
        if name == "cleanup_intents":
            return (
                "Call list_intents. For each active HostToHostIntent, ask me "
                "to confirm before calling remove_intent. Do not remove "
                "anything without explicit confirmation."
            )
        raise ToolError("unknown prompt %r" % name)

    # ------------------------------------------------------------------
    # JSON-RPC / MCP dispatch
    # ------------------------------------------------------------------
    def _result(self, request_id, result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id, code, message):
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def _tool_result(self, value):
        if isinstance(value, str):
            text = value
        else:
            text = json_text(value)
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        }

    def _tool_error(self, error):
        return {
            "content": [{"type": "text", "text": "ERROR: %s" % error}],
            "isError": True,
        }

    def call_tool(self, name, arguments):
        handlers = {
            "get_network_context": lambda args: self.get_network_context(),
            "list_hosts": lambda args: self.list_hosts(),
            "list_intents": lambda args: self.list_intents(
                _bool_arg(args, "include_withdrawn", False)
            ),
            "list_flows": lambda args: self.list_flows(),
            "create_intent": lambda args: self.create_intent(
                _arg(args, "source"),
                _arg(args, "destination"),
                _bool_arg(args, "force", False),
                _number_arg(args, "timeout", 25, 1, 60),
            ),
            "remove_intent": lambda args: self.remove_intent(
                _arg(args, "source"), _arg(args, "destination")
            ),
            "connect_nodes": lambda args: self.connect_nodes(
                _arg(args, "source"),
                _arg(args, "destination"),
                _bool_arg(args, "force", False),
                _number_arg(args, "timeout", 25, 1, 60),
            ),
            "disconnect_nodes": lambda args: self.disconnect_nodes(
                _arg(args, "source"), _arg(args, "destination")
            ),
            "check_connectivity": lambda args: self.check_connectivity(
                _arg(args, "source"),
                _arg(args, "destination"),
                _int_arg(args, "count", 3, 1, 10),
            ),
            "set_link": lambda args: self.set_link(
                _arg(args, "link"), _arg(args, "state")
            ),
        }
        handler = handlers.get(name)
        if handler is None:
            raise ToolError("unknown tool %r" % name)
        try:
            return self._tool_result(handler(arguments or {}))
        except (ToolError, OnosError, ConflictError) as error:
            return self._tool_error(error)
        except Exception as error:  # noqa: BLE001
            return self._tool_error("unexpected error: %s" % error)

    def handle(self, message):
        if not isinstance(message, dict):
            return self._error(None, -32600, "request must be an object")
        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message

        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion")
            protocol = (
                requested
                if requested in SUPPORTED_PROTOCOLS
                else "2024-11-05"
            )
            result = {
                "protocolVersion": protocol,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                    "logging": {},
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
                "instructions": INSTRUCTIONS,
            }
            return self._result(request_id, result)

        if method in (
            "notifications/initialized",
            "notifications/cancelled",
            "notifications/roots/list_changed",
        ):
            return None
        if is_notification:
            return None

        if method == "ping":
            return self._result(request_id, {})
        if method == "logging/setLevel":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": self.tools})
        if method == "resources/list":
            return self._result(request_id, {"resources": self.resources})
        if method == "resources/templates/list":
            return self._result(request_id, {"resourceTemplates": []})
        if method == "prompts/list":
            return self._result(request_id, {"prompts": self.prompts})
        if method == "completion/complete":
            return self._result(
                request_id,
                {
                    "completion": {
                        "values": [],
                        "total": 0,
                        "hasMore": False,
                    }
                },
            )

        params = message.get("params") or {}
        if method == "tools/call":
            name = params.get("name")
            if not name:
                return self._error(request_id, -32602, "missing tool name")
            try:
                result = self.call_tool(name, params.get("arguments") or {})
            except ToolError as error:
                return self._error(request_id, -32602, str(error))
            return self._result(request_id, result)
        if method == "resources/read":
            uri = params.get("uri")
            if not uri:
                return self._error(request_id, -32602, "missing resource uri")
            try:
                text, mime_type = self.read_resource(uri)
            except ToolError as error:
                return self._error(request_id, -32602, str(error))
            return self._result(
                request_id,
                {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": mime_type,
                            "text": text,
                        }
                    ]
                },
            )
        if method == "prompts/get":
            name = params.get("name")
            if not name:
                return self._error(request_id, -32602, "missing prompt name")
            try:
                text = self.get_prompt(name, params.get("arguments") or {})
            except ToolError as error:
                return self._error(request_id, -32602, str(error))
            return self._result(
                request_id,
                {
                    "description": name,
                    "messages": [
                        {
                            "role": "user",
                            "content": {"type": "text", "text": text},
                        }
                    ],
                },
            )

        return self._error(request_id, -32601, "method not found: %s" % method)

    # ------------------------------------------------------------------
    # stdio transport
    # ------------------------------------------------------------------
    def _send(self, message):
        sys.stdout.write(
            json.dumps(message, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
        sys.stdout.flush()

    def run(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError as error:
                self._send(self._error(None, -32700, "parse error: %s" % error))
                continue
            try:
                response = self.handle(message)
            except Exception as error:  # noqa: BLE001
                self._send(
                    self._error(
                        message.get("id") if isinstance(message, dict) else None,
                        -32603,
                        "internal error: %s" % error,
                    )
                )
                continue
            if response is not None:
                self._send(response)

    def selftest(self):
        print("server: %s %s" % (SERVER_NAME, SERVER_VERSION))
        print("ONOS: %s" % self.onos_url)
        print("control: %s" % (self.control_url or "disabled"))
        print("tools:")
        for tool in self.tools:
            print("  - %s" % tool["name"])
        print("resources:")
        for resource in self.resources:
            print("  - %s" % resource["uri"])
        print("prompts:")
        for prompt in self.prompts:
            print("  - %s" % prompt["name"])


def main(argv=None):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file")
    known, _unknown = pre.parse_known_args(argv)
    try:
        loaded_env = load_env(known.env_file)
    except OnosError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 2
    if loaded_env:
        print("loaded env file: %s" % loaded_env, file=sys.stderr)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        default=os.environ.get("ONOS_ENV_FILE"),
        help="load ONOS_* variables from this file",
    )
    parser.add_argument(
        "--onos-url",
        default=os.environ.get("ONOS_URL", DEFAULT_ONOS_URL),
        help="ONOS northbound URL",
    )
    parser.add_argument(
        "--app-id",
        default=os.environ.get("ONOS_APP_ID", DEFAULT_APP_ID),
        help="intent application ID",
    )
    parser.add_argument(
        "--onos-user",
        default=os.environ.get("ONOS_USER", DEFAULT_ONOS_USER),
        help="ONOS REST username",
    )
    parser.add_argument(
        "--onos-password",
        default=os.environ.get("ONOS_PASSWORD", DEFAULT_ONOS_PASSWORD),
        help="ONOS REST password",
    )
    parser.add_argument(
        "--control-url",
        default=os.environ.get("ONOS_CONTROL_URL", ""),
        help="Mininet control API URL, for example http://127.0.0.1:9090",
    )
    parser.add_argument(
        "--host-alias",
        action="append",
        dest="host_aliases",
        help="host alias name=IP/MAC/ONOS-ID; repeat for more hosts",
    )
    parser.add_argument(
        "--switch-alias",
        action="append",
        dest="switch_aliases",
        help="switch alias name=ONOS-device-ID; repeat for more switches",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="print the MCP surface and exit",
    )
    args = parser.parse_args(argv)

    host_aliases = parse_aliases(os.environ.get("ONOS_HOST_ALIASES", ""))
    switch_aliases = parse_aliases(os.environ.get("ONOS_SWITCH_ALIASES", ""))
    for value in args.host_aliases or []:
        host_aliases.update(parse_aliases(value))
    for value in args.switch_aliases or []:
        switch_aliases.update(parse_aliases(value))

    core = OnosMCPCore(
        onos_url=args.onos_url,
        app_id=args.app_id,
        control_url=args.control_url,
        host_aliases=host_aliases,
        switch_aliases=switch_aliases,
        onos_user=args.onos_user,
        onos_password=args.onos_password,
    )
    if args.selftest:
        core.selftest()
        return 0
    print(
        "MCP stdio fallback ready; waiting for JSON-RPC on stdin.",
        file=sys.stderr,
    )
    core.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
