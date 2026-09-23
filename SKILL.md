---
name: hermes-bridge
description: Tailnet-only MCP bridge that exposes Hermes to cloud assistants (Muse, Claude Code, Codex CLI, custom clients). Installs a systemd-managed FastAPI server with a TOML peer config, an allowlist, and a Telegram-gated approval queue for risky actions.
tags: [mcp, tailscale, bridge, muse, plugins, networking]
related_skills: [hermes-agent-platform, hermes-ops]
---

# Hermes Bridge

A thin MCP server that lets a cloud assistant (your `muse` tailnet node, Claude
Code on another machine, a custom Python client, etc.) talk to your Hermes
over a Tailscale network.

The bridge runs on your Hermes box, bound to a tailnet IP, no public exposure.
It exposes five tools; four are default-allow, one (`agent_ask`) routes through
the Hermes agent loop. Risky actions (writes, sends, Home Assistant control)
are queued for approval on your Home Telegram thread.

## When to use

- "I want my cloud assistant to talk to my Hermes"
- "Set up the muse ↔ hermes MCP bridge"
- "Add another peer to my hermes-bridge"
- "Bridge is broken, debug it"
- "Bridge approval prompt went to my DM, can't reply to it"
- "Cron doesn't pick up my yes/no reply"
- "Bridge is silently running but resolver never resolves"

## Install

```bash
~/.hermes/skills/devops/hermes-bridge/scripts/setup.sh
```

This will:
1. Detect your Tailscale IP and write `~/.hermes/bridge/peer_config.toml`
2. Copy bridge files to `~/.hermes/bridge/`
3. Generate `allowlist.yaml` from the peer config
4. Render a systemd unit pointing at your install
5. Register `hermes-bridge` as a local MCP server so your *own* Hermes toolset picks up the bridge tools

## Configuration

Edit `~/.hermes/bridge/peer_config.toml` then rerun `setup.sh`:

```toml
[server]
bind_host = "100.x.x.x"   # your tailnet IP
port = 7777               # avoid 6666 (probe magnet)

[vault]
allowed_roots = [
  "~/.hermes/memory",
  "~/.hermes/skills",
  "~/.hermes/cron",
  "~/nvme-data/Documents/Obsidian",
]

[approval]
channel = "telegram"      # where risky-action requests land
ttl_seconds = 1800        # how long an approval stays valid (30 min)
```

The `[approval]` block is **fully wired** — `~/.hermes/.env` gets
`TELEGRAM_BRIDGE_APPROVALS_CHANNEL=<chat_id>` and optionally
`TELEGRAM_BRIDGE_APPROVALS_THREAD=<thread_id>` set during install, and the
`hermes-bridge-approvals` cron posts pending requests there every minute.
See [Approval flow](#approval-flow) below for the routing rules and known
pitfalls.

## Tools exposed

| Tool            | Default-allow | What it does                                              |
|-----------------|---------------|-----------------------------------------------------------|
| `memory_search`     | ✅           | memex8 semantic search via the agent loop                 |
| `vault_read`        | ✅           | Read files under `[vault].allowed_roots`                  |
| `session_search`    | ✅           | FTS5 search of past Hermes conversations                  |
| `inbox_read`        | ✅           | Read the shared coordination note                         |
| `agent_ask`         | ✅           | Free-form prompt through the agent loop                   |
| `send_message`      | ⚠️ gated     | Send via Hermes gateway (Telegram/Discord/Matrix)         |
| `vault_write`       | ⚠️ gated     | Write to a file under allowed vault roots (snapshots first) |

`memory_search`, `session_search`, and `agent_ask` route through the Hermes
agent loop. Their output is post-processed by `_strip_cli_chrome()` which
removes box-drawing characters (`╭─`, `╰─`, `│`, `┊`), spinner lines
(`⚡ preparing tool details…`), `Warning: Unknown toolsets` lines,
`Query: <prompt>` echoes, the `Resume this session` footer, and
`⚠ Deprecated .env settings` warnings. Cloud callers receive a clean
string. `stderr_tail` in the response carries only the last 500
characters of stderr, also chrome-stripped.

## Approval flow

1. Cloud-side calls a gated tool (e.g. `send_message`).
2. Bridge writes `~/.hermes/bridge/approvals/pending/<uuid>.json`.
3. Cron `hermes-bridge-approvals` (every minute) posts the request to your
   configured Telegram chat with the uuid.
4. You reply `yes <uuid>` or `no <uuid>` in that same chat.
5. Cron's next tick reads the gateway log, finds your reply, flips the record
   to `approved`/`denied`, moves it to `audit/<date>.jsonl`.
6. Bridge's blocked HTTP call returns the tool's result.

**Default approval chat: your normal Hermes Telegram group** (where you talk
to Hermes). NOT your personal DM with the bot — that path is broadcast-only
and replying there doesn't reach the resolver. Configure via
`TELEGRAM_BRIDGE_APPROVALS_CHANNEL` in `~/.hermes/.env`.

Cloud clients should set `tools/call` timeout > TTL (default 30 min).

## Smoke test

After setup, from any tailnet box:

```bash
HERMES_BRIDGE_URL=http://100.x.x.x:7777 \
  ~/.hermes/hermes-agent/venv/bin/python \
  ~/.hermes/skills/devops/hermes-bridge/scripts/smoke.py
```

Should print `PASS: all smoke checks green`.

## Security model

- **Bind**: tailnet IP only (`100.x.x.x`). Not 0.0.0.0. Belt-and-braces middleware also rejects non-`100.x` clients with 403.
- **Auth**: tailnet membership IS the auth. No tokens. If your tailnet is compromised, your bridge is too.
- **Paths**: `vault_read` rejects anything outside `[vault].allowed_roots`. `..` traversal blocked at runtime.
- **Risky actions**: queued in `~/.hermes/bridge/approvals/pending/*.json`, audit trail under `audit/<date>.jsonl`.
- **No deletes through the bridge** — reads and writes only.
- **Snapshots on overwrite** (when write tools are added) go under `snapshots/`.

## Limitations / honest caveats

- **Latency**: `memory_search` and `session_search` spawn a Hermes CLI subprocess per call (~10-15 seconds cold). Don't hammer them.
- **Bot flood control**: if Hermes is rate-limited sending outbound (long conversation, lots of progress edits), inbound Telegram polling can stall. Approval messages arrive delayed. Not a bridge bug — Telegram-imposed.
- **TTL pressure**: `ttl_seconds=1800` (30 min) is the default. Cloud-side callers block that long. If you drive away and can't reply, the request auto-expires — nothing is leaked, just the call returns `expired`.

## Gotchas (lessons learned)

See `references/gotchas.md` for the hard-won lessons, including:

- **bash heredoc vs stdin redirect** — they don't compose. The bug that ate 4 hours of debugging.
- **Telegram bot polling is single-consumer** — can't have two long-pollers on one bot token.
- **TTL was too short** at 10 min; now 30 min. Future: consider a "yes pending" bulk approve.

## Files in this skill

```
hermes-bridge/
├── SKILL.md                    # this file
├── peer_config.toml            # user-side config template (copied + edited by setup.sh)
├── hermes_bridge.py            # FastAPI + uvicorn, MCP StreamableHTTP
├── approvals.py                # pending → audit queue (TTL handling)
├── hermes-bridge.service       # systemd unit template
├── inbox.template.md           # initial inbox contents
├── allowlist.template.yaml     # (reference) what setup.sh generates
├── references/
│   └── gotchas.md              # hard-won lessons
└── scripts/
    ├── setup.sh                # the install entry point
    ├── smoke.py                # 60-line MCP client smoke test
    ├── telegram_resolver.sh    # bash cron: notify + resolve
    └── _resolve_chunk.py       # python parser for gateway log chunks
```

After setup, the live install lives at `~/.hermes/bridge/` and `setup.sh`
should be rerun only to update files (your `peer_config.toml` is preserved).