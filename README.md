# hermes-bridge

A tailnet-only MCP bridge that exposes [Hermes](https://github.com/NousResearch/hermes-agent)
to a cloud assistant (your `muse` tailnet node, Claude Code on another machine,
a custom Python client, etc.) with a Telegram-gated approval queue for risky
actions.

## Why this exists

You want your cloud-side assistant to know what your always-on Hermes knows —
memory, vault, session history, your schedule. But you also want to stay in
control of what it can *do* (send messages, write files, change settings).

The bridge gives you the first without giving up the second:

- **Read-only tools** (memory search, vault read, session search, inbox) work
  automatically. Your cloud side can ask, Hermes answers.
- **Write/send tools** (send message, vault write) require you to approve
  each call. The bridge posts a request to your Telegram chat, you reply
  `yes <uuid>` / `no <uuid>`, and the call goes through or gets denied.

Tailscale membership is the auth — no tokens, no API keys. The bridge binds
to your tailnet IP only, so only nodes on your tailnet can reach it.

## Tools exposed

| Tool            | Default-allow | What it does                                              |
|-----------------|---------------|-----------------------------------------------------------|
| `memory_search` | ✅            | memex8 semantic search via the agent loop                 |
| `vault_read`    | ✅            | Read files under `[vault].allowed_roots`                  |
| `session_search`| ✅            | FTS5 search of past Hermes conversations                  |
| `inbox_read`    | ✅            | Read the shared coordination note                         |
| `agent_ask`     | ✅            | Free-form prompt through the agent loop                   |
| `send_message`  | ⚠️ gated      | Send via Hermes gateway (Telegram/Discord/Matrix)         |
| `vault_write`   | ⚠️ gated      | Write to a file under allowed vault roots (snapshots first) |

## Install

Requirements: a working Hermes install (`~/.hermes/`) on the same machine
where the bridge will run, and Tailscale.

```bash
git clone https://github.com/Ex8-ca/hermes-bridge
cd hermes-bridge
./scripts/setup.sh
```

The installer will:
1. Detect your Tailscale IP and write `~/.hermes/bridge/peer_config.toml`
2. Copy bridge files to `~/.hermes/bridge/`
3. Generate `allowlist.yaml` from your peer config
4. Render and link a systemd user unit (`hermes-bridge.service`)
5. Register `hermes-bridge` as a local MCP server (your Hermes picks it up)
6. Install `telegram_resolver.sh` and schedule the `hermes-bridge-approvals` cron

After install:
```bash
systemctl --user status hermes-bridge.service   # should be active
curl http://<your-tailnet-ip>:7777/health        # should return ok
```

## Configuration

`~/.hermes/bridge/peer_config.toml`:

```toml
[server]
bind_host = "100.x.x.x"   # your tailnet IP
port = 7777               # avoid 6666 (probe magnet)

[vault]
allowed_roots = [
  "~/.hermes/memory",
  "~/.hermes/skills",
  "~/nvme-data/Documents/Obsidian",
]

[approval]
channel = "telegram"      # where risky-action requests land
ttl_seconds = 1800        # how long an approval stays valid (30 min)
```

The `[approval]` block is fully wired — the installer adds
`TELEGRAM_BRIDGE_APPROVALS_CHANNEL` and `TELEGRAM_BRIDGE_APPROVALS_THREAD` to
`~/.hermes/.env`. The cron posts pending requests there every minute and
resolves them when you reply `yes <uuid>` / `no <uuid>`.

**Default approval chat: your normal Hermes Telegram group** (where you talk
to Hermes). NOT your personal DM with the bot — that path is broadcast-only.

## Connecting from a cloud-side client

Any MCP client works. From a Python script:

```python
import asyncio
from mcp import ClientSession, StdioServerParameters
# or use the SSE/StreamableHTTP transport

async def main():
    async with ClientSession(
        url="http://100.x.x.x:7777/mcp",  # your bridge
    ) as session:
        tools = await session.list_tools()
        result = await session.call_tool("memory_search", {"query": "warren"})
        print(result)
```

The smoke test in `scripts/smoke.py` is a 60-line MCP client you can copy
from.

## Architecture

```
Cloud side                 Tailscale              Hermes box
+----------+    HTTP/MCP   +--------+   StreamableHTTP    +-----------+
| Muse /   | -----------> | tailnet |  ----------------> | hermes-   |
| Claude   | <----------- |         |  <----------------- | bridge    |
| Code /   |              +--------+                       +-----------+
+----------+                                                       |
                                                          fastapi :7777
                                                                   |
                                                                   v
                                                          +-----------+
                                                          | Hermes    |
                                                          | agent     |
                                                          +-----------+
                                                                   |
                                              +-----------------+----------------+
                                              |                 |                |
                                          agent_ask        send_message    vault_write
                                              |                 |                |
                                              v                 v                v
                                         runs query      Telegram cron      file snapshot
                                         in-process      asks user          + write
```

For risky actions (`send_message`, `vault_write`), the bridge queues a
pending record, posts a request to Telegram, and **blocks the cloud-side
HTTP call** until you reply yes/no (or the 30-minute TTL expires). The
caller must set a long enough `tools/call` timeout.

## Security model

- **Bind**: tailnet IP only (`100.x.x.x`). Not 0.0.0.0.
- **Auth**: tailnet membership IS the auth. No tokens.
- **Middleware belt-and-braces**: rejects non-`100.x` clients with 403.
- **Paths**: `vault_read` / `vault_write` reject anything outside
  `[vault].allowed_roots`. `..` traversal blocked at runtime.
- **Risky actions**: queued, audited, expirable.
- **No deletes through the bridge** — reads and writes only.
- **Snapshots on overwrite**: `vault_write` mode=overwrite copies the
  existing file to `<file>.snapshots/<ts>.bak` first.

## Files in this repo

```
hermes-bridge/
├── README.md                  # this file
├── LICENSE                    # MIT
├── SKILL.md                   # detailed developer notes
├── peer_config.toml           # user-side config template
├── hermes_bridge.py           # FastAPI + uvicorn, MCP StreamableHTTP
├── approvals.py               # pending → audit queue (TTL handling)
├── hermes-bridge.service      # systemd unit template
├── inbox.template.md          # initial inbox contents
├── allowlist.template.yaml    # (reference) what setup.sh generates
├── references/
│   └── gotchas.md             # hard-won lessons (heredoc bug, polling, etc.)
└── scripts/
    ├── setup.sh               # the install entry point
    ├── smoke.py               # 60-line MCP client smoke test
    ├── telegram_resolver.sh   # bash cron: notify + resolve
    └── _resolve_chunk.py      # python parser for gateway log chunks
```

## Honest caveats

- **Latency**: `memory_search` and `session_search` spawn a Hermes CLI
  subprocess per call (~10-15 seconds cold). Don't hammer them.
- **Bot flood control**: if Hermes is rate-limited sending outbound (long
  conversation, lots of progress edits), inbound Telegram polling can
  stall. Approval messages arrive delayed. Not a bridge bug — Telegram-imposed.
- **TTL pressure**: `ttl_seconds=1800` (30 min) is the default. Cloud-side
  callers block that long. If you drive away and can't reply, the request
  auto-expires — nothing is leaked, just the call returns `expired`.

## Read more

- [`SKILL.md`](SKILL.md) — detailed developer notes, the full approval flow
  spec, and per-tool return shapes.
- [`references/gotchas.md`](references/gotchas.md) — eight hard-won lessons
  including the bash heredoc bug that ate 4 hours, the Telegram polling
  single-consumer constraint, and the TTL pressure design choice.

## License

MIT. See [`LICENSE`](LICENSE).
