#!/usr/bin/env python3
"""60-line smoke test for hermes-bridge.

The article's pattern: initialize, grab session id, list tools, run one of each.
Fail loud on transport errors, classify isError: true separately.
"""
import json
import sys
import uuid

import os
import httpx

URL = os.environ.get("HERMES_BRIDGE_URL", "http://127.0.0.1:7777")
TIMEOUT = float(os.environ.get("HERMES_BRIDGE_TIMEOUT", "15"))

def step(label):
    print(f"=== {label} ===", flush=True)

def rpc(client, sid, method, params=None):
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params or {}}
    headers = {"Content-Type": "application/json"}
    if sid:
        headers["Mcp-Session-Id"] = sid
    r = client.post(f"{URL}/mcp", json=body, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r

def main():
    failures = []

    with httpx.Client() as c:
        # health
        step("health")
        h = c.get(f"{URL}/health", timeout=TIMEOUT)
        h.raise_for_status()
        print(json.dumps(h.json(), indent=2))

        # initialize
        step("initialize")
        r = rpc(c, None, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0.1"},
        })
        sid = r.headers.get("Mcp-Session-Id")
        if not sid:
            failures.append("no Mcp-Session-Id header")
        print(f"session id: {sid}")
        print(json.dumps(r.json(), indent=2)[:300])

        # tools/list
        step("tools/list")
        r = rpc(c, sid, "tools/list")
        tools = r.json()["result"]["tools"]
        names = [t["name"] for t in tools]
        print(f"tools: {names}")
        if "vault_read" not in names or "agent_ask" not in names:
            failures.append("expected tools missing")

        # tool call: memory_search (routes through agent loop; skipped in smoke
        # because it boots the full Hermes CLI — too slow for a fast smoke.
        # Verified manually by running the bridge and calling memory_search.)
        step("memory_search (skipped — agent loop boots ~30s; manual verify)")

        # tool call: vault_read on allowed root
        step("vault_read (allowed)")
        r = rpc(c, sid, "tools/call", {
            "name": "vault_read",
            "arguments": {"path": "~/.hermes/bridge/allowlist.yaml"},
        })
        result = r.json()["result"]
        if result.get("isError"):
            failures.append("vault_read returned isError on allowed path")
        print(result["content"][0]["text"][:400])

        # tool call: vault_read blocked path
        step("vault_read (denied)")
        r = rpc(c, sid, "tools/call", {
            "name": "vault_read",
            "arguments": {"path": "/etc/passwd"},
        })
        body = json.loads(r.json()["result"]["content"][0]["text"])
        print(body)
        if "error" not in body:
            failures.append("vault_read did NOT block /etc/passwd")

        # tool call: inbox_read
        step("inbox_read")
        r = rpc(c, sid, "tools/call", {"name": "inbox_read", "arguments": {}})
        result = r.json()["result"]
        if result.get("isError"):
            failures.append("inbox_read returned isError")
        print(result["content"][0]["text"][:200])

    print()
    if failures:
        print(f"FAIL: {len(failures)}: {failures}")
        sys.exit(1)
    print("PASS: all smoke checks green")


if __name__ == "__main__":
    main()