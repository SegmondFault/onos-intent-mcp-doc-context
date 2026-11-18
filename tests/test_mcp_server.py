#!/usr/bin/env python3
"""Unit tests for the ONOS MCP server.

Run in the official MCP SDK environment:
    uv run python -m unittest -v tests.test_mcp_server

The tests use the fake ONOS client from tests/test_intent_tools.py, so they do not
need a running controller or Mininet topology.  If the MCP SDK is not
installed, the module is skipped so the rest of the offline suite still runs.
"""

import asyncio
import json
import unittest

from tests.test_intent_tools import (
    GENERIC_DEVICES,
    GENERIC_HOSTS,
    FakeClient,
)

try:
    from mcp import Client
    from onos_mcp.core import OnosMCPCore
    from onos_mcp.server import build_server
except ImportError:
    Client = None
    build_server = None
    OnosMCPCore = None


class McpFakeClient(FakeClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._flows = []
        for index in range(1, 5):
            self._flows.append(
                {
                    "deviceId": "of:000000000000000%d" % index,
                    "id": "flow-%d" % index,
                    "state": "ADDED",
                    "priority": 100,
                    "selector": {"criteria": []},
                    "treatment": {"instructions": []},
                    "bytes": 0,
                    "packets": 0,
                    "duration": 1,
                }
            )
        self._next_flow = 100

    def devices(self):
        return [
            {
                "id": "of:000000000000000%d" % index,
                "available": True,
                "type": "SWITCH",
            }
            for index in range(1, 5)
        ]

    def links(self):
        pairs = (("1", "2"), ("2", "3"), ("3", "4"), ("4", "1"))
        rows = []
        for left, right in pairs:
            for src, dst in ((left, right), (right, left)):
                rows.append(
                    {
                        "src": {
                            "device": "of:000000000000000" + src,
                            "port": "2",
                        },
                        "dst": {
                            "device": "of:000000000000000" + dst,
                            "port": "2",
                        },
                        "state": "ACTIVE",
                    }
                )
        return rows

    def flows(self):
        return self._flows

    def create_flow(self, device_id, payload, app_id):
        self._next_flow += 1
        row = dict(payload)
        row.update(
            {
                "deviceId": device_id,
                "id": "flow-%d" % self._next_flow,
                "state": "ADDED",
                "appId": app_id,
            }
        )
        self._flows.append(row)
        return 201, {}, row, json.dumps(row)

    def delete_flow(self, device_id, flow_id):
        self._flows = [
            flow
            for flow in self._flows
            if not (
                flow.get("deviceId") == device_id
                and flow.get("id") == flow_id
            )
        ]
        return 204, {}, "", "/flows/%s/%s" % (device_id, flow_id)


class GenericMcpFakeClient(McpFakeClient):
    def __init__(self, host_aliases=None):
        super().__init__(hosts=[dict(host) for host in GENERIC_HOSTS])
        self.host_aliases = host_aliases or {}
        for index, host in enumerate(self._hosts):
            host["locations"] = [
                {
                    "elementId": (
                        "of:0000000000000001"
                        if index < 2
                        else "of:0000000000000005"
                    ),
                    "port": str(index + 1),
                }
            ]
        self._flows = [
            {
                "deviceId": "of:0000000000000001",
                "id": "flow-1",
                "state": "ADDED",
                "priority": 100,
                "selector": {"criteria": []},
                "treatment": {"instructions": []},
                "bytes": 0,
                "packets": 0,
                "duration": 1,
            }
        ]

    def devices(self):
        return [dict(device) for device in GENERIC_DEVICES]

    def links(self):
        return [
            {
                "src": {"device": "of:0000000000000001", "port": "2"},
                "dst": {"device": "of:0000000000000005", "port": "2"},
                "state": "ACTIVE",
            },
            {
                "src": {"device": "of:0000000000000005", "port": "2"},
                "dst": {"device": "of:0000000000000001", "port": "2"},
                "state": "ACTIVE",
            },
        ]

    def flows(self):
        return self._flows


def make_server():
    return build_server(
        onos_url="http://127.0.0.1:8181",
        app_id="org.test",
        control_url="",
        client=McpFakeClient(),
    )


def make_generic_server():
    return build_server(
        onos_url="http://127.0.0.1:8181",
        app_id="org.test",
        control_url="",
        client=GenericMcpFakeClient(),
        host_aliases={"web": "10.10.0.50"},
        switch_aliases={"edge": "of:0000000000000005"},
        website_aliases={
            "wikipedia.org": "10.10.0.5",
            "netflix.com": "10.10.0.50",
        },
    )


async def _with_client(action):
    async with Client(make_server()) as client:
        return await action(client)


@unittest.skipUnless(Client, "mcp SDK is not installed")
class McpProtocolTests(unittest.TestCase):
    def test_initialize_advertises_capabilities(self):
        async def exercise():
            async with Client(make_server(), mode="legacy") as client:
                return client.session.initialize_result

        result = asyncio.run(exercise())
        self.assertIsNotNone(result.capabilities.tools)
        self.assertIsNotNone(result.capabilities.resources)
        self.assertIsNotNone(result.capabilities.prompts)
        self.assertEqual(result.server_info.name, "onos-intent-mcp-doc-context")

    def test_tools_list(self):
        async def action(client):
            return await client.list_tools()

        result = asyncio.run(_with_client(action))
        names = [tool.name for tool in result.tools]
        self.assertIn("get_network_context", names)
        self.assertIn("create_intent", names)
        self.assertIn("remove_intent", names)
        self.assertIn("connect_nodes", names)
        self.assertIn("disconnect_nodes", names)
        self.assertIn("check_connectivity", names)
        self.assertIn("block_traffic", names)
        self.assertIn("unblock_traffic", names)
        self.assertIn("list_websites", names)
        self.assertIn("set_website_access", names)
        self.assertIn("check_website_access", names)
        self.assertIn("set_link", names)
        self.assertIn("list_context_documents", names)
        self.assertIn("read_context_document", names)

    def test_network_context(self):
        async def action(client):
            return await client.call_tool("get_network_context", {})

        result = asyncio.run(_with_client(action))
        self.assertFalse(result.is_error)
        payload = json.loads(result.content[0].text)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["hosts"]), 4)
        self.assertEqual(len(payload["switches"]), 4)
        self.assertEqual(len(payload["links"]), 4)
        self.assertFalse(payload["control"]["enabled"])

    def test_resource_read(self):
        async def action(client):
            return await client.read_resource("onos://network/context")

        result = asyncio.run(_with_client(action))
        self.assertEqual(result.contents[0].uri, "onos://network/context")
        self.assertIn("summary", json.loads(result.contents[0].text))

    def test_prompt_get(self):
        async def action(client):
            return await client.get_prompt(
                "connect_hosts",
                {"source": "h1", "destination": "h2"},
            )

        result = asyncio.run(_with_client(action))
        text = result.messages[0].content.text
        self.assertIn("connect_nodes", text)
        self.assertIn("h1", text)
        self.assertIn("h2", text)


@unittest.skipUnless(Client, "mcp SDK is not installed")
class McpIntentTests(unittest.TestCase):
    def test_create_list_and_remove_intent(self):
        async def action(client):
            created = await client.call_tool(
                "create_intent",
                {"source": "h1", "destination": "h2"},
            )
            listed = await client.call_tool("list_intents", {})
            removed = await client.call_tool(
                "remove_intent",
                {"source": "h2", "destination": "h1"},
            )
            return created, listed, removed

        created, listed, removed = asyncio.run(_with_client(action))
        self.assertFalse(created.is_error)
        self.assertEqual(
            json.loads(created.content[0].text)["intent"]["state"],
            "INSTALLED",
        )
        self.assertEqual(len(json.loads(listed.content[0].text)["intents"]), 1)
        self.assertEqual(len(json.loads(removed.content[0].text)["removed"]), 1)

    def test_duplicate_and_overlap_are_reported(self):
        async def action(client):
            await client.call_tool(
                "create_intent", {"source": "h1", "destination": "h2"}
            )
            duplicate = await client.call_tool(
                "create_intent", {"source": "h2", "destination": "h1"}
            )
            overlap = await client.call_tool(
                "create_intent", {"source": "h1", "destination": "h3"}
            )
            forced = await client.call_tool(
                "create_intent",
                {"source": "h1", "destination": "h3", "force": True},
            )
            return duplicate, overlap, forced

        duplicate, overlap, forced = asyncio.run(_with_client(action))
        self.assertTrue(duplicate.is_error)
        self.assertIn("duplicate", duplicate.content[0].text)
        self.assertTrue(overlap.is_error)
        self.assertIn("overlap", overlap.content[0].text)
        self.assertFalse(forced.is_error)
        self.assertTrue(json.loads(forced.content[0].text)["forced"])

    def test_control_tool_explains_when_disabled(self):
        async def action(client):
            return await client.call_tool(
                "check_connectivity",
                {"source": "h1", "destination": "h2"},
            )

        result = asyncio.run(_with_client(action))
        self.assertTrue(result.is_error)
        self.assertIn("control API", result.content[0].text)

    def test_unknown_tool_is_reported(self):
        async def action(client):
            return await client.call_tool("does_not_exist", {})

        result = asyncio.run(_with_client(action))
        self.assertTrue(result.is_error)
        self.assertIn("does_not_exist", result.content[0].text)


@unittest.skipUnless(Client, "mcp SDK is not installed")
class GenericToolTests(unittest.TestCase):
    def test_host_block_roundtrip(self):
        core = OnosMCPCore(
            onos_url="http://127.0.0.1:8181",
            app_id="org.test",
            control_url="",
            client=GenericMcpFakeClient(),
        )
        blocked = core.block_traffic("h1", "h5", 120)
        self.assertTrue(blocked["ok"])
        self.assertEqual(len(blocked["rules"]), 2)
        self.assertEqual(len(core.list_traffic_blocks()["rules"]), 2)
        removed = core.unblock_traffic("h5", "h1")
        self.assertEqual(len(removed["removed"]), 2)
        self.assertEqual(core.list_traffic_blocks()["rules"], [])

    def test_website_policy_roundtrip(self):
        core = OnosMCPCore(
            onos_url="http://127.0.0.1:8181",
            app_id="org.test",
            control_url="",
            client=GenericMcpFakeClient(),
            website_aliases={"netflix.com": "10.10.0.50"},
        )
        denied = core.set_website_access(
            "h1", "netflix.com", "deny", 120
        )
        self.assertTrue(denied["ok"])
        rules = core.list_website_policies()["deny_rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(
            rules[0]["criteria_map"]["IPV4_DST"], "10.10.0.50/32"
        )
        allowed = core.set_website_access(
            "h1", "netflix.com", "allow", 120
        )
        self.assertEqual(len(allowed["removed"]), 1)
        self.assertEqual(core.list_website_policies()["deny_rules"], [])

    def test_hospital_website_catalogue_names(self):
        core = OnosMCPCore(
            onos_url="http://127.0.0.1:8181",
            app_id="org.test",
            control_url="",
            client=GenericMcpFakeClient(),
            website_aliases={
                "wikipedia.org": "10.10.0.5",
                "netflix.com": "10.10.0.50",
            },
        )
        rows = core.list_websites()["websites"]
        self.assertEqual(
            [row["name"] for row in rows],
            ["netflix.com", "wikipedia.org"],
        )

    def test_connect_and_disconnect_hosts_by_name(self):
        async def action(client):
            connected = await client.call_tool(
                "connect_nodes", {"source": "h1", "destination": "h5"}
            )
            disconnected = await client.call_tool(
                "disconnect_nodes", {"source": "h5", "destination": "h1"}
            )
            return connected, disconnected

        connected, disconnected = asyncio.run(
            self._with_generic_client(action)
        )
        self.assertFalse(connected.is_error)
        self.assertEqual(json.loads(connected.content[0].text)["mode"], "intent")
        self.assertFalse(disconnected.is_error)
        self.assertEqual(
            json.loads(disconnected.content[0].text)["mode"], "intent"
        )

    def test_alias_resolves_host(self):
        async def action(client):
            return await client.call_tool(
                "connect_nodes", {"source": "web", "destination": "h1"}
            )

        result = asyncio.run(self._with_generic_client(action))
        payload = json.loads(result.content[0].text)
        self.assertFalse(result.is_error)
        self.assertEqual(payload["source"], "web")
        self.assertEqual(payload["destination"], "h1")

    def test_switch_and_host_switch_links(self):
        core = OnosMCPCore(
            onos_url="http://127.0.0.1:8181",
            app_id="org.test",
            control_url="http://control",
            client=GenericMcpFakeClient(),
        )
        calls = []

        def fake_command(payload):
            calls.append(payload)
            return {"ok": True, "state": payload["state"]}

        core.control_command = fake_command
        switch_up = core.connect_nodes("s1", "s5")
        switch_down = core.disconnect_nodes("s5", "s1")
        host_link = core.connect_nodes("h1", "s1")
        self.assertEqual(switch_up["mode"], "link")
        self.assertEqual(switch_down["mode"], "link")
        self.assertEqual(host_link["mode"], "link")
        self.assertEqual(
            [call["state"] for call in calls], ["up", "down", "up"]
        )
        self.assertEqual(calls[0]["left"], "s1")
        self.assertEqual(calls[0]["right"], "s5")

    @staticmethod
    async def _with_generic_client(action):
        async with Client(make_generic_server()) as client:
            return await action(client)


if __name__ == "__main__":
    unittest.main(verbosity=2)
