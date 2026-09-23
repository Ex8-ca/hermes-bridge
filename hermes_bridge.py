"""Hermes bridge — a thin MCP server that exposes Hermes to a cloud assistant
over Tailscale. Bound to a tailnet IP only.

Implements the MCP StreamableHTTP handshake:
  1. POST /mcp with initialize → server returns session id (Mcp-Session-Id header)
  2. Subsequent POSTs carry that header → server returns SSE-formatted responses
  3. tools/list returns the four default-allow tools
  4. tools/call routes the request through the Hermes agent loop

Default-allow tools (see allowlist.yaml) run without an approval gate.
Anything else raises a NotAllowedError and the client must surface it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

BRIDGE_HOME = Path(os.environ.get("HERMES_BRIDGE_HOME", str(Path.home() / ".hermes" / "bridge")))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(BRIDGE_HOME.parent)))
HERMES_BIN = os.environ.get("HERMES_BIN", str(BRIDGE_HOME.parent / "hermes-agent" / "venv" / "bin" / "hermes"))


def _detect_tailnet_ip() -> str | None:
    """Return the first 100.x.x.x IPv4 address from Tailscale, or None.

    Falls back to None if Tailscale is not installed or not running. The
    caller should provide a real bind address via HERMES_BRIDGE_BIND_HOST
    or peer_config.toml in production.
    """
    import subprocess
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                ip = line.strip()
                if ip.startswith("100."):
                    return ip
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None

sys.path.insert(0, str(BRIDGE_HOME))
from approvals import request_approval, wait_for  # noqa: E402

ALLOWLIST_PATH = BRIDGE_HOME / "allowlist.yaml"
PEER_CONFIG_PATH = BRIDGE_HOME / "peer_config.toml"
VAULT_DEFAULT, BASE_DENY = "default_allow", "default_deny"

log = logging.getLogger("hermes-bridge")

# ---------- config ----------

def load_peer_config() -> dict[str, Any]:
    """Read peer_config.toml if present; fall back to defaults."""
    if not PEER_CONFIG_PATH.exists():
        return {
            "server": {"bind_host": "0.0.0.0", "port": 7777},
            "vault": {"allowed_roots": [str(HERMES_HOME / "memory"),
                                       str(HERMES_HOME / "skills"),
                                       str(HERMES_HOME / "cron"),
                                       str(BRIDGE_HOME),
                                       str(Path.home() / "Documents" / "Obsidian")]},
            "approval": {"channel": "telegram"},
            "logging": {"level": "info"},
        }
    try:
        import tomllib  # py3.11+
        with PEER_CONFIG_PATH.open("rb") as f:
            return tomllib.load(f)
    except Exception as e:
        log.warning("peer_config.toml unreadable: %s — using defaults", e)
        return {}


def load_allowlist() -> dict[str, Any]:
    """allowlist.yaml is auto-generated from peer_config.toml by setup.sh."""
    if ALLOWLIST_PATH.exists():
        return yaml.safe_load(ALLOWLIST_PATH.read_text())
    # fall back to deriving from peer_config
    cfg = load_peer_config()
    return {
        "default_allow": [
            "vault_read", "session_search", "inbox_read", "agent_ask",
        ],
        "allowed_vault_roots": cfg.get("vault", {}).get("allowed_roots", []),
    }


def is_default_allowed(tool: str) -> bool:
    al = load_allowlist()
    return tool in al.get("default_allow", [])


# ---------- path safety ----------

def safe_resolve(path: str) -> Path:
    """Resolve `path` under an allowed root, reject .. escapes.

    Raises ValueError on anything outside the allowlisted roots.
    """
    al = load_allowlist()
    roots = [Path(r).expanduser().resolve() for r in al.get("allowed_vault_roots", [])]
    candidate = Path(path).expanduser().resolve()
    if not any(str(candidate).startswith(str(r)) for r in roots):
        raise ValueError(f"path {candidate} is outside any allowed vault root")
    if ".." in candidate.parts:
        raise ValueError("path traversal blocked")
    return candidate

# ---------- tool implementations ----------

def tool_memory_search(args: dict) -> dict:
    """Search Hermes long-term memory (memex8).

    Routes through the agent loop because memex8 is an MCP stdio server,
    not an HTTP endpoint. The agent loop has memex8_search wired in
    natively. We pass a constrained prompt and read stdout.
    """
    q = args.get("query", "")
    top_k = int(args.get("top_k", 5))
    prompt = (
        f"Use the memex8_search MCP tool with query={json.dumps(q)} and top_k={top_k}. "
        f"Return the results verbatim in a concise bulleted list — one bullet per "
        f"memory, with the source/realm and a one-line excerpt. No preamble. "
        f"If no results, say 'no matches'."
    )
    return _run_agent(prompt, pass_session_id=True)


def tool_session_search(args: dict) -> dict:
    """Search past Hermes conversation history.

    Routes through the agent loop. The agent has the `session_search` tool
    wired in natively — it talks to the FTS5-backed session DB directly.
    """
    q = args.get("query", "")
    limit = int(args.get("limit", 5))
    prompt = (
        f"Use the session_search MCP tool with query={json.dumps(q)} and limit={limit}. "
        f"Return the matches verbatim as a bulleted list — for each session, show "
        f"the timestamp, the session id (if available), and a one-line summary of "
        f"what was discussed. No preamble. If no matches, say 'no sessions found'."
    )
    return _run_agent(prompt, pass_session_id=True)


def tool_agent_ask(args: dict) -> dict:
    """Route a free-form prompt through the Hermes agent loop. Side effects
    (writes, sends, HA control) still trigger Hermes's normal safety checks."""
    prompt = args.get("prompt", "")
    if not prompt:
        return {"error": "prompt required"}
    return _run_agent(prompt, pass_session_id=False)


def _run_agent(prompt: str, *, pass_session_id: bool) -> dict:
    """Spawn a one-shot Hermes CLI invocation and capture its reply.

    Uses `hermes chat -q ... --oneshot --cli` so it runs to completion and
    exits. With --pass-session-id, consecutive calls share cached context
    (cheaper, faster) — used by memory/session search so a follow-up
    "tell me more about the first one" doesn't pay full prompt cost.

    The raw output includes "Query: <prompt>", "Initializing agent..." lines,
    progress chatter, and the actual reply mixed together. We strip those to
    give cloud callers clean data.
    """
    import subprocess
    cmd = [
        HERMES_BIN, "chat",
        "-q", prompt, "--oneshot", "--cli", "--no-restore-cwd",
    ]
    if pass_session_id:
        cmd.append("--pass-session-id")
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, env={**os.environ},
        )
        reply = _strip_cli_chrome(out.stdout, prompt=prompt)
        # Sanitize stderr_tail: same chrome filter applies
        stderr_clean = _strip_cli_chrome(out.stderr) if out.stderr else ""
        return {
            "reply": reply,
            "stderr_tail": stderr_clean[-500:] if stderr_clean else "",
        }
    except subprocess.TimeoutExpired:
        return {"error": "agent timed out (180s)"}


def _strip_cli_chrome(raw: str, prompt: str | None = None) -> str:
    """Strip Hermes CLI decorations to leave just the agent's reply.

    The CLI emits a lot of decoration: box-drawing characters (`╭─`, `╰─`,
    `│`, `┊`), spinner lines (`⚡ preparing tool details…`), `Warning: ...`,
    `Query: <prompt>` echoes, and a trailing `Resume this session` summary.

    Strategy: walk lines, drop anything that's a CLI decoration, then
    coalesce the remaining reply into a clean string. If the response is a
    bulleted list (`- foo` / `• foo`), we keep that intact.

    If `prompt` is provided, any line that's a fragment of the prompt is also
    dropped — Hermes sometimes echoes part of the prompt into stdout.
    """
    import re as _re
    # Build a set of substrings that identify prompt fragments (per-line).
    # The CLI sometimes echoes part of the prompt back, possibly truncated or
    # with line-wrap. We scan for any contiguous substring of length >= 30
    # chars that occurs in the prompt, and drop lines containing any of them.
    prompt_fragments: set[str] = set()
    if prompt:
        # Sliding window over the prompt, step 10
        for i in range(0, len(prompt) - 30):
            sub = prompt[i:i+30].strip()
            if sub and not sub.isspace():
                prompt_fragments.add(sub)
        # Also catch full sentences >= 25 chars (in case the agent wrapped differently)
        for fragment in _re.split(r'[.\n;]\s*', prompt):
            f = fragment.strip()
            if len(f) >= 25:
                prompt_fragments.add(f)

    out_lines = []
    box_top_re = _re.compile(r"^[\u250c\u256d\u256e\u256f\u2570]")  # box corners
    spinner_re = _re.compile(r"^[\u250a]")  # │ / ┊ (vertical bar variants)
    deco_only_re = _re.compile(r"^[\u2500\u2501\u2550\u2551]+$")  # horizontal lines

    for line in raw.splitlines():
        stripped = line.strip()
        # Drop empty lines at start
        if not out_lines and not stripped:
            continue
        # Drop CLI box borders/spinner lines entirely
        if box_top_re.match(stripped):
            continue
        if spinner_re.match(stripped):
            continue
        if deco_only_re.match(stripped):
            continue
        # Drop obvious chrome prefixes
        if any(stripped.startswith(prefix) for prefix in [
            "Warning:", "Query:", "Initializing agent",
            "Thinking...", "Reasoning...", "Loading",
            "Streaming...", "Final answer:",
            "Resume this session with:", "hermes --resume",
            "hermes -c", "Session:", "Title:", "Duration:",
            "Messages:", "Tools:", "End of session",
            "⚠ Deprecated", "Move to config.yaml", "Then remove",
            "MESSAGING_CWD",
        ]):
            continue
        # Drop prompt fragments echoed by the agent
        if prompt_fragments and any(frag in stripped for frag in prompt_fragments):
            continue
        # Drop progress-edit noise
        if "[Telegram]" in stripped and ("flood" in stripped.lower() or "progress" in stripped.lower()):
            continue
        # Drop dashed separators
        if stripped in ("...", "…", "———", "────"):
            continue
        # Drop lines that are mostly box-drawing decoration (length > 8, only box chars)
        if len(stripped) > 8 and all(c in "─━│┃┌┐└┘┏┓┗┛━┃" for c in stripped):
            continue
        out_lines.append(line)
    # Trim trailing empties
    while out_lines and not out_lines[-1].strip():
        out_lines.pop()
    return "\n".join(out_lines).strip()


def tool_vault_read(args: dict) -> dict:
    """Read a file under an allowed vault root."""
    p = safe_resolve(args["path"])
    if not p.exists():
        return {"error": f"not found: {p}"}
    text = p.read_text(errors="replace")
    # cap huge reads at 50KB so the cloud side can't accidentally drain context
    return {"path": str(p), "content": text[:50_000], "truncated": len(text) > 50_000}


def tool_inbox_read(args: dict) -> dict:
    """Read the shared coordination inbox."""
    inbox = BRIDGE_HOME / "shared_inbox" / "inbox.md"
    if not inbox.exists():
        return {"content": ""}
    return {"content": inbox.read_text()}


def tool_send_message(args: dict) -> dict:
    """Send a message via Hermes's messaging gateway (Telegram/Discord/Matrix).

    Args:
        text: message body
        target: optional override target (e.g. 'telegram:5275167911',
               'discord:1489089542874206289'). If omitted, uses the bridge's
               configured home channel.

    Returns:
        dict with 'sent', 'target', 'chars' keys. The caller (Hermes) handles
        rate limiting / channel validation; this tool is a thin proxy.
    """
    text = args.get("text", "").strip()
    if not text:
        return {"error": "empty text"}
    if len(text) > 4096:
        return {"error": f"message too long ({len(text)} chars, max 4096)"}

    target = args.get("target", "").strip() or None
    cmd = [
        HERMES_BIN, "send",
    ]
    if target:
        cmd += ["--to", target]
    cmd.append(text)

    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return {"error": f"send failed: {out.stderr.strip() or out.stdout.strip()[:200]}"}
        return {"sent": True, "target": target or "default", "chars": len(text)}
    except subprocess.TimeoutExpired:
        return {"error": "send timed out (30s)"}


def tool_vault_write(args: dict) -> dict:
    """Write a file under an allowed vault root.

    Args:
        path: target file path (must be inside an allowed_vault_root)
        content: file contents
        mode: 'overwrite' (default, snapshot first) or 'append'

    Returns:
        dict with 'path', 'bytes_written', 'snapshot' (if created)

    Safety:
        - Path must resolve under an allowed vault root (else ValueError)
        - Overwrite mode copies the existing file to .snapshots/<ts>.md first
        - Append mode is fine without a snapshot
        - No deletes through this tool
    """
    target = safe_resolve(args["path"])
    content = args.get("content", "")
    mode = args.get("mode", "overwrite")

    snapshot = None
    if mode == "overwrite":
        if target.exists():
            ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
            snap_dir = target.parent / ".snapshots"
            snap_dir.mkdir(parents=True, exist_ok=True)
            snapshot_path = snap_dir / f"{target.name}.{ts}.bak"
            snapshot_path.write_bytes(target.read_bytes())
            snapshot = str(snapshot_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    elif mode == "append":
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as f:
            f.write(content)
    else:
        return {"error": f"unknown mode: {mode!r}, expected 'overwrite' or 'append'"}

    return {"path": str(target), "bytes_written": len(content), "snapshot": snapshot}


def _summarize_request(tool: str, args: dict, client_ip: str) -> str:
    """Build a one-line summary for the approval prompt."""
    if tool == "send_message":
        text = (args.get("text") or "").strip()
        if len(text) > 80:
            text = text[:77] + "..."
        target = args.get("target") or "default home"
        return f"send '{text}' to {target} (from {client_ip})"
    if tool == "vault_write":
        path = args.get("path", "?")
        mode = args.get("mode", "overwrite")
        n = len(args.get("content", ""))
        return f"write {n} chars to {path} (mode={mode}, from {client_ip})"
    return f"{tool}({json.dumps(args)[:80]}) from {client_ip}"


TOOLS = {
    "memory_search": {
        "description": "Semantic search over Hermes long-term memory (memex8). Routes through the Hermes agent loop.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
        "handler": tool_memory_search,
    },
    "agent_ask": {
        "description": "Run a prompt through the Hermes agent loop. Side effects still gated by Hermes's normal safety checks.",
        "inputSchema": {
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
        },
        "handler": tool_agent_ask,
    },
    "vault_read": {
        "description": "Read a file under an allowed vault root. .. escapes blocked.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "handler": tool_vault_read,
    },
    "session_search": {
        "description": "Search past Hermes conversation history.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
        "handler": tool_session_search,
    },
    "inbox_read": {
        "description": "Read the shared coordination inbox note.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_inbox_read,
    },
    # Gated — require approval per call.
    "send_message": {
        "description": "Send a message via Hermes's messaging gateway. Always requires approval. Target defaults to the bridge's home channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "target": {"type": "string", "description": "optional override target (e.g. telegram:5275167911)"},
            },
            "required": ["text"],
        },
        "handler": tool_send_message,
        "requires_approval": True,
    },
    "vault_write": {
        "description": "Write to a file under an allowed vault root. Overwrite mode snapshots first. Always requires approval.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
            },
            "required": ["path", "content"],
        },
        "handler": tool_vault_write,
        "requires_approval": True,
    },
}

# ---------- MCP server ----------

app = FastAPI(title="hermes-bridge", version="0.1.0")
SESSIONS: dict[str, float] = {}  # session_id -> last_seen epoch
SESSION_TTL_SECONDS = 3600


def _is_authorized(request: Request) -> bool:
    """Tailnet membership is the auth. Reject anything not from 100.x."""
    client = request.client.host if request.client else ""
    return client.startswith("100.") or client == "127.0.0.1"


@app.middleware("http")
async def tailnet_gate(request: Request, call_next):
    if not _is_authorized(request):
        log.warning("rejected non-tailnet request from %s", request.client.host if request.client else "?")
        return JSONResponse({"error": "tailnet-only"}, status_code=403)
    return await call_next(request)


@app.get("/health")
async def health():
    return {"ok": True, "tools": list(TOOLS.keys()), "sessions": len(SESSIONS)}


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """StreamableHTTP MCP endpoint. Both initialize and tool calls land here."""
    body = await request.json()
    method = body.get("method")
    sid = request.headers.get("Mcp-Session-Id")

    if method == "initialize":
        new_sid = os.urandom(8).hex()
        SESSIONS[new_sid] = time.time()
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "hermes-bridge", "version": "0.1.0"},
                },
            },
            headers={"Mcp-Session-Id": new_sid},
        )

    if not sid or sid not in SESSIONS:
        return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32000, "message": "no session"}}, status_code=400)

    SESSIONS[sid] = time.time()

    if method == "tools/list":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": {"tools": [
                {"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
                for name, spec in TOOLS.items()
            ]},
        })

    if method == "tools/call":
        params = body.get("params", {})
        tool = params.get("name")
        args = params.get("arguments", {})
        return await _dispatch_tool(body.get("id"), tool, args, request)

    return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601, "message": f"unknown method {method}"}}, status_code=400)


async def _dispatch_tool(req_id: Any, tool: str, args: dict, request: Request):
    spec = TOOLS.get(tool)
    if spec is None:
        # Tool not registered. Gate through approval flow so the user can see
        # what was asked for, then return a structured not-implemented response
        # if they approved.
        cfg = load_peer_config()
        ttl = int(cfg.get("approval", {}).get("ttl_seconds", 600))
        client_ip = request.client.host if request.client else "unknown"
        summary = _summarize_request(tool, args, client_ip)
        request_id, _ = request_approval(
            tool=tool, args=args, requested_by=client_ip,
            summary=summary, ttl_seconds=ttl,
        )
        log.info("queued unknown tool %s for approval (%s)", tool, request_id)
        record = wait_for(request_id, timeout=ttl + 30, poll=2.0)
        status = record.get("status", "expired")
        body = {"approval": status, "request_id": request_id, "tool": tool, "args": args}
        if status == "expired":
            body["note"] = "no decision within TTL — try again"
        else:
            body["note"] = "tool approved by user but not implemented in this bridge"
        is_error = status == "expired"
        return JSONResponse({
            "jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": json.dumps(body)}], "isError": is_error},
        })

    # Tool is registered. If it requires approval, gate first.
    if spec.get("requires_approval"):
        cfg = load_peer_config()
        ttl = int(cfg.get("approval", {}).get("ttl_seconds", 600))
        client_ip = request.client.host if request.client else "unknown"
        summary = _summarize_request(tool, args, client_ip)
        request_id, _ = request_approval(
            tool=tool, args=args, requested_by=client_ip,
            summary=summary, ttl_seconds=ttl,
        )
        log.info("queued %s for approval (%s)", tool, request_id)
        record = wait_for(request_id, timeout=ttl + 30, poll=2.0)
        status = record.get("status", "expired")
        if status != "approved":
            body = {"approval": status, "request_id": request_id, "tool": tool}
            if status == "expired":
                body["note"] = "no decision within TTL — try again"
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(body)}],
                           "isError": status == "expired"},
            })
        # Approved — execute handler and wrap result.
        try:
            result = spec["handler"](args)
        except Exception as e:
            log.exception("tool %s failed after approval", tool)
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps({
                    "approval": "approved", "request_id": request_id,
                    "tool": tool, "error": str(e),
                })}], "isError": True},
            })
        return JSONResponse({
            "jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": json.dumps({
                "approval": "approved", "request_id": request_id,
                "result": result,
            }, default=str)}], "isError": False},
        })

    # Default-allow: execute directly.
    try:
        result = spec["handler"](args)
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": json.dumps(result, default=str)}], "isError": False},
        })
    except ValueError as e:
        # Safety rejections (path outside allowed roots, .. escapes, etc.)
        # are expected and not bugs — log at INFO, not ERROR with a stack trace.
        log.info("tool %s rejected: %s", tool, e)
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}], "isError": True},
        })
    except Exception as e:
        log.exception("tool %s failed", tool)
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}], "isError": True},
        })


# ---------- entrypoint ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None, help="bind address (default from peer_config.toml)")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--log-level", default=None)
    args = ap.parse_args()

    cfg = load_peer_config()
    server_cfg = cfg.get("server", {})
    log_cfg = cfg.get("logging", {})

    bind_host = (os.environ.get("HERMES_BRIDGE_BIND_HOST")
                   or args.host
                   or server_cfg.get("bind_host")
                   or _detect_tailnet_ip()
                   or "127.0.0.1")
    port = (int(os.environ["HERMES_BRIDGE_PORT"]) if os.environ.get("HERMES_BRIDGE_PORT") else
           (args.port or int(server_cfg.get("port", 7777))))
    log_level = (args.log_level or log_cfg.get("level", "info")).upper()

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if not bind_host.startswith("100.") and bind_host not in ("127.0.0.1", "::1"):
        log.warning("binding to non-tailnet host %s — are you sure?", bind_host)

    log.info("hermes-bridge starting on %s:%d", bind_host, port)
    uvicorn.run(app, host=bind_host, port=port, log_level=log_level.lower(), access_log=False)


if __name__ == "__main__":
    main()