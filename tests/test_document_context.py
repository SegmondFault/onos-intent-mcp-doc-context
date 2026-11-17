#!/usr/bin/env python3
"""Tests for the bounded document-context catalogue and MCP tools."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from mcp import Client
from onos_mcp.document_context import (
    DocumentContextError,
    DocumentContextStore,
)
from onos_mcp.server import build_server
from tests.test_mcp_server import McpFakeClient


def write_context(root):
    documents = root / "documents"
    documents.mkdir()
    (documents / "brief.md").write_text(
        "# Demo\nmarker: COBALT-MERIDIAN-27\nfinal line\n",
        encoding="utf-8",
    )
    (documents / "rules.json").write_text(
        json.dumps({"restricted": ["h2-h4"]}),
        encoding="utf-8",
    )
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "documents": [
                    {
                        "id": "brief",
                        "title": "Brief",
                        "file": "documents/brief.md",
                        "scenario_ids": ["S01"],
                        "category": "supplied_context",
                    },
                    {
                        "id": "rules",
                        "title": "Rules",
                        "file": "documents/rules.json",
                        "scenario_ids": ["S02"],
                        "category": "supplied_context",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


def make_context_server(manifest):
    return build_server(
        onos_url="http://127.0.0.1:8181",
        app_id="org.test",
        control_url="",
        client=McpFakeClient(),
        context_manifest=str(manifest),
    )


class DocumentContextStoreTests(unittest.TestCase):
    def test_list_filter_and_bounded_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = DocumentContextStore(write_context(Path(temporary)))
            listed = store.list_documents("S01")
            self.assertEqual([row["id"] for row in listed["documents"]], ["brief"])
            read = store.read_document("brief", start_line=2, max_lines=1)
            self.assertEqual(read["content"], "marker: COBALT-MERIDIAN-27")
            self.assertTrue(read["truncated"])
            self.assertTrue(read["content_is_untrusted"])

    def test_manifest_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "documents": [
                            {
                                "id": "escape",
                                "title": "Escape",
                                "file": "../escape.md",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(DocumentContextError):
                DocumentContextStore(root / "manifest.json")

    def test_manifest_rejects_unsupported_extension(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "payload.bin").write_bytes(b"text")
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "documents": [
                            {
                                "id": "binary",
                                "title": "Binary",
                                "file": "payload.bin",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(DocumentContextError):
                DocumentContextStore(root / "manifest.json")


class DocumentContextMcpTests(unittest.TestCase):
    def test_mcp_lists_and_reads_approved_document(self):
        async def exercise(manifest):
            async with Client(make_context_server(manifest)) as client:
                listed = await client.call_tool(
                    "list_context_documents", {"scenario_id": "S01"}
                )
                read = await client.call_tool(
                    "read_context_document",
                    {"document_id": "brief", "start_line": 2, "max_lines": 1},
                )
                return listed, read

        with tempfile.TemporaryDirectory() as temporary:
            listed, read = asyncio.run(exercise(write_context(Path(temporary))))
        listed_payload = json.loads(listed.content[0].text)
        read_payload = json.loads(read.content[0].text)
        self.assertFalse(listed.is_error)
        self.assertEqual(listed_payload["documents"][0]["id"], "brief")
        self.assertFalse(read.is_error)
        self.assertEqual(read_payload["content"], "marker: COBALT-MERIDIAN-27")

    def test_mcp_rejects_unknown_document_id(self):
        async def exercise(manifest):
            async with Client(make_context_server(manifest)) as client:
                return await client.call_tool(
                    "read_context_document", {"document_id": "not-approved"}
                )

        with tempfile.TemporaryDirectory() as temporary:
            result = asyncio.run(exercise(write_context(Path(temporary))))
        self.assertTrue(result.is_error)
        self.assertIn("unknown context document id", result.content[0].text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
