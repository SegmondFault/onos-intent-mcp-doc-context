#!/usr/bin/env python3
"""End-to-end Qwen -> MCP -> ONOS/Mininet action smoke test."""

from __future__ import annotations

import argparse
import asyncio
import json
import urllib.error
import urllib.request

from mcp import Client


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-url",
        default="http://127.0.0.1:8080/v1/chat/completions",
    )
    parser.add_argument("--model", default="qwen3.8-flash-next")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8002/mcp")
    parser.add_argument("--timeout", type=int, default=1200)
    return parser.parse_args()


def model_request(url, payload, timeout):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        raise RuntimeError(
            "local model returned HTTP %d: %s" % (error.code, body[:1000])
        ) from error


def openai_tool(tool):
    dumped = tool.model_dump(by_alias=True, exclude_none=True)
    schema = dumped.get("inputSchema") or dumped.get("input_schema") or {}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": schema,
        },
    }


def result_text(result):
    return "\n".join(
        block.text
        for block in result.content
        if getattr(block, "type", None) == "text"
    )


def parsed_result(result):
    text = result_text(result)
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text}


async def checked_call(client, name, arguments, allow_error=False):
    result = await client.call_tool(name, arguments)
    if result.is_error and not allow_error:
        raise RuntimeError("MCP tool %s failed: %s" % (name, result_text(result)))
    return parsed_result(result)


async def wait_for_tool_ok(client, name, arguments, attempts=8, delay=2):
    last = None
    for _attempt in range(attempts):
        last = await checked_call(client, name, arguments)
        if last.get("ok"):
            return last
        await asyncio.sleep(delay)
    return last


async def agent_run(
    args, client, tool_map, allowed, prompt, final_token, validator
):
    tools = [openai_tool(tool_map[name]) for name in allowed]
    messages = [
        {
            "role": "system",
            "content": (
                "You are conducting a controlled SDN lab test. Follow the "
                "requested tool sequence exactly, inspect every returned "
                "result, and do not claim a network outcome without a tool "
                "result. All endpoints are simulated Mininet hosts."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    trace = []
    for _iteration in range(18):
        response = model_request(
            args.model_url,
            {
                "model": args.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "parallel_tool_calls": False,
                "temperature": 0,
                "max_tokens": 1024,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            args.timeout,
        )
        message = response["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            answer = (message.get("content") or "").strip()
            validation_error = None
            if final_token in answer:
                try:
                    validator(trace)
                except RuntimeError as error:
                    validation_error = str(error)
            if final_token not in answer or validation_error:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You have not completed the requested sequence. "
                            + (
                                "Validation says: %s. " % validation_error
                                if validation_error
                                else ""
                            )
                            + "Continue with the missing tool calls, then include "
                            + final_token
                            + " in the final response."
                        ),
                    }
                )
                continue
            return {"answer": answer, "trace": trace}

        messages.append(
            {
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": tool_calls,
            }
        )
        for call in tool_calls:
            name = call["function"]["name"]
            if name not in allowed:
                raise RuntimeError("model requested unapproved tool: %s" % name)
            arguments = json.loads(call["function"].get("arguments") or "{}")
            result = await client.call_tool(name, arguments)
            payload = parsed_result(result)
            trace.append(
                {
                    "name": name,
                    "arguments": arguments,
                    "is_error": bool(result.is_error),
                    "result": payload,
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result_text(result),
                }
            )
    raise RuntimeError("model exceeded the tool-loop iteration limit")


def find_after(trace, name, start=0, predicate=None):
    for index in range(start, len(trace)):
        row = trace[index]
        if row["name"] != name:
            continue
        if predicate is None or predicate(row):
            return index, row
    raise RuntimeError("missing required %s call after trace index %d" % (name, start))


def validate_host(trace):
    if any(row["is_error"] for row in trace):
        raise RuntimeError("one or more host-action MCP calls returned an error")
    blocked_index, _blocked = find_after(trace, "block_traffic")
    failed_index, failed = find_after(
        trace,
        "check_connectivity",
        blocked_index + 1,
        lambda row: row["result"].get("ok") is False,
    )
    unblocked_index, unblocked = find_after(
        trace, "unblock_traffic", failed_index + 1
    )
    restored_index, restored = find_after(
        trace,
        "check_connectivity",
        unblocked_index + 1,
        lambda row: row["result"].get("ok") is True,
    )
    if len(unblocked["result"].get("removed") or []) != 2:
        raise RuntimeError("unblock did not remove both directional rules")
    return {
        "blocked_loss_percent": failed["result"].get("loss_percent"),
        "restored_loss_percent": restored["result"].get("loss_percent"),
        "verified_at_trace_index": restored_index,
    }


def website_action(row, website, action):
    arguments = row["arguments"]
    return (
        arguments.get("website") == website
        and arguments.get("action") == action
    )


def website_check(row, website, ok):
    return (
        row["arguments"].get("website") == website
        and row["result"].get("ok") is ok
    )


def validate_websites(trace):
    if any(row["is_error"] for row in trace):
        raise RuntimeError("one or more website MCP calls returned an error")
    allow_index, _ = find_after(
        trace,
        "set_website_access",
        predicate=lambda row: website_action(row, "wikipedia.org", "allow"),
    )
    deny_index, _ = find_after(
        trace,
        "set_website_access",
        allow_index + 1,
        lambda row: website_action(row, "netflix.com", "deny"),
    )
    allowed_index, allowed = find_after(
        trace,
        "check_website_access",
        deny_index + 1,
        lambda row: website_check(row, "wikipedia.org", True),
    )
    denied_index, denied = find_after(
        trace,
        "check_website_access",
        allowed_index + 1,
        lambda row: website_check(row, "netflix.com", False),
    )
    restore_index, _ = find_after(
        trace,
        "set_website_access",
        denied_index + 1,
        lambda row: website_action(row, "netflix.com", "allow"),
    )
    _final_index, restored = find_after(
        trace,
        "check_website_access",
        restore_index + 1,
        lambda row: website_check(row, "netflix.com", True),
    )
    return {
        "allowed_http_status": allowed["result"].get("status"),
        "denied_http_status": denied["result"].get("status"),
        "restored_http_status": restored["result"].get("status"),
    }


async def remove_if_present(client, source, destination):
    await checked_call(
        client,
        "remove_intent",
        {"source": source, "destination": destination},
        allow_error=True,
    )


async def run(args):
    report = {"ok": False, "model": args.model, "phases": {}}
    async with Client(args.mcp_url, read_timeout_seconds=args.timeout) as client:
        listed = await client.list_tools()
        tool_map = {tool.name: tool for tool in listed.tools}
        required = {
            "connect_nodes",
            "remove_intent",
            "check_connectivity",
            "block_traffic",
            "unblock_traffic",
            "list_websites",
            "set_website_access",
            "check_website_access",
            "list_traffic_blocks",
            "list_website_policies",
        }
        missing = required - set(tool_map)
        if missing:
            raise RuntimeError("MCP is missing tools: %s" % sorted(missing))

        try:
            await checked_call(
                client,
                "connect_nodes",
                {"source": "h1", "destination": "h2", "force": True},
            )
            baseline = await wait_for_tool_ok(
                client,
                "check_connectivity",
                {"source": "h1", "destination": "h2", "count": 2},
            )
            if not baseline.get("ok"):
                raise RuntimeError("h1-h2 baseline ping failed: %r" % baseline)

            host = await agent_run(
                args,
                client,
                tool_map,
                {"block_traffic", "unblock_traffic", "check_connectivity"},
                (
                    "Test h1 and h2. Call block_traffic for h1 and h2 for 300 "
                    "seconds. Then call check_connectivity from h1 to h2 with "
                    "count 2 and confirm it is blocked. Then call "
                    "unblock_traffic for h1 and h2. Finally call "
                    "check_connectivity again with count 2 and confirm it is "
                    "restored. Do every step even if a previous result is "
                    "unexpected. End with HOST_TEST_COMPLETE."
                ),
                "HOST_TEST_COMPLETE",
                validate_host,
            )
            report["phases"]["host_block"] = {
                "baseline_loss_percent": baseline.get("loss_percent"),
                "validation": validate_host(host["trace"]),
                "agent": host,
            }
            await remove_if_present(client, "h1", "h2")

            await checked_call(
                client,
                "connect_nodes",
                {"source": "h1", "destination": "h4", "force": True},
            )
            await checked_call(
                client,
                "connect_nodes",
                {"source": "h1", "destination": "h5", "force": True},
            )
            for website in ("wikipedia.org", "netflix.com"):
                baseline_http = await wait_for_tool_ok(
                    client,
                    "check_website_access",
                    {"source": "h1", "website": website},
                )
                if not baseline_http.get("ok"):
                    raise RuntimeError(
                        "%s baseline HTTP failed: %r"
                        % (website, baseline_http)
                    )

            websites = await agent_run(
                args,
                client,
                tool_map,
                {"list_websites", "set_website_access", "check_website_access"},
                (
                    "Test simulated website policy from h1. First call "
                    "list_websites. Call set_website_access to allow "
                    "wikipedia.org from h1. Then deny netflix.com from "
                    "h1 for 300 seconds. Check wikipedia.org and confirm its "
                    "HTTP request succeeds. Check netflix.com and confirm "
                    "its HTTP request fails. Then allow netflix.com again "
                    "and check it once more to confirm HTTP succeeds. Do every "
                    "step in that order. End with WEBSITE_TEST_COMPLETE."
                ),
                "WEBSITE_TEST_COMPLETE",
                validate_websites,
            )
            report["phases"]["website_policy"] = {
                "validation": validate_websites(websites["trace"]),
                "agent": websites,
            }
            report["ok"] = True
            return report
        finally:
            await checked_call(
                client,
                "unblock_traffic",
                {"source": "h1", "destination": "h2"},
                allow_error=True,
            )
            for website in ("wikipedia.org", "netflix.com"):
                await checked_call(
                    client,
                    "set_website_access",
                    {
                        "source": "h1",
                        "website": website,
                        "action": "allow",
                    },
                    allow_error=True,
                )
            for destination in ("h2", "h4", "h5"):
                await remove_if_present(client, "h1", destination)


def main():
    result = asyncio.run(run(parse_args()))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
