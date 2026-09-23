# Gotchas — Lessons from the hermes-bridge build

These are the bugs and design choices that cost real time. Each one is
load-bearing — read this before changing the resolver or the bridge.

## 1. bash heredoc vs `< file` redirect — they don't compose

**The bug**:
```bash
python3 - "$ARGS" < "$CHUNK" <<'PYEOF'
...python source...
PYEOF
```

**Why it's broken**: when bash sees both `< file` AND a heredoc (`<<EOF`),
the **heredoc wins**. Python's stdin reads the heredoc contents (the Python
source code itself), not `$CHUNK`. The chunk file is silently ignored.

**Symptom**: python runs without error, prints nothing, the chunk isn't
parsed. Test the python standalone → works perfectly. Test the script →
silent. Hours of debugging.

**Fix**: put the python in a separate file. Use `python3 script.py args <
chunk` — no heredoc, no contention.

**Why I almost shipped this**: my "successful" tests were the python
running standalone, not the script that wraps it. Same logic, completely
different stdin source.

## 2. Telegram bot long-poll is single-consumer

**The constraint**: Telegram only delivers each `update` to **one** consumer
of `getUpdates`. If two processes call `getUpdates` on the same bot token,
one gets the updates and the other gets `{"ok":true,"result":[]}` forever.

**Why this matters here**: Hermes's gateway runs in long-poll mode against
the bot token. Any second consumer (e.g. a "watch for replies" script)
will see empty results.

**Workaround we used**: don't poll Telegram at all. The gateway already
long-polls and writes inbound messages to `~/.hermes/logs/gateway.log`.
Tail that log instead. The resolver sees the same updates without
competing for them.

**Alternatives we didn't take**:
- Switch the gateway to webhook mode (would require a publicly reachable URL).
- Add a `bot` user to the gateway that exposes the inbound stream (touches
  gateway code, violates the plugin contract).
- Use a different bot token for approvals (operational complexity, two
  bots to manage).

## 3. dd bs=1 over a multi-MB log is unusable

**The bug**:
```bash
LAST=4500000
CUR=4505000
dd if=log bs=1 skip=$LAST count=$((CUR - LAST))  # 5KB
```

This takes **minutes** because dd reads one byte at a time and seeks per
byte. Even for "only 5KB to read".

**Fix**: use `tail -c +N` which reads forward in O(1). For the first
run with no prior offset, use `tail -c 65536` to get the last 64KB.

## 4. bash multi-line string in `body="..."` strips newlines

**The bug**:
```bash
body="line 1

line 2"
```

In bash, `body="..."` with literal newlines inside the quotes **collapses**
newlines — `body` ends up as just `line 1`. The empty line and line 2 are
lost.

**Fix**: use a heredoc with selective variable substitution, or write to a
temp file with `cat > tmp <<EOF ... EOF` then `sed -i` placeholders.

## 5. UUID length check rejected test ids

**The bug**: regex required `len(uuid) == 12`. Real bridge-generated uuids
are `uuid.uuid4().hex[:12]` (12 chars). But human-typed test ids are
variable length (8-15). My E2E test ids (`e2e0001xyz` = 10 chars) got
silently rejected.

**Fix**: accept any reasonable length range (4-64). The UUID format is
validated by file existence (`pending/<uuid>.json`), not by length.

## 6. Bridge approval chat must be where you actually reply

**The bug**: I initially routed approval prompts to `TELEGRAM_HOME_CHANNEL`,
which is the user's **personal DM with the bot**. The user expected to be
able to reply there but couldn't (the bot's DM is broadcast-only by
convention).

**Fix**: route to the **group where the user already talks to Hermes**
(chat_id `-1003743484579`, thread 5 in this case). User replies `yes/no`
in that group, gateway.log captures it, resolver picks it up.

**Lesson**: "Home channel" can mean different things in different contexts.
For approval gates, "home" should be where the user's normal conversation
loop already is — not a one-way notification stream.

## 7. Telegram flood control stalls inbound during long sessions

**The bug**: in a long conversation with lots of progress-edit messages,
Telegram's rate limit hits the bot. Outbound sending backs off. The bot's
long-poll also stalls. New inbound messages (your "yes <uuid>" reply)
arrive in Telegram but **the gateway doesn't see them** because polling
is stuck in backoff.

**Symptom**: approval prompt lands but your reply never resolves the
pending record. Eventually times out as `expired`.

**Not a bridge bug**: it's Telegram rate-limiting. The bridge's resolver
just can't see what hasn't been delivered.

**Mitigations**:
- Send approval prompts at a less-busy time
- Use a dedicated approval channel with its own bot token (clean of the
  main conversation's flood-control noise)
- Document that users shouldn't expect approvals to resolve during peak
  outbound traffic

## 8. 10-minute TTL was too short for a driving user

**The bug**: when the user is driving, 10 minutes goes by in seconds.
The approval times out before they can pull over and reply.

**Fix**: bumped default to 30 minutes (1800s). Cloud-side callers block
that long, but the gate is still effective — and the user can actually
respond.

**Lesson**: when designing timeouts for human-in-the-loop systems,
assume the human is distracted. 30 minutes is a reasonable upper bound
for a "I'll reply when I can" pattern.

## 9. CLI chrome leaks through to MCP responses

**The bug**: when `_run_agent` shells out to `hermes chat --cli --oneshot`,
the captured stdout is decorated with Hermes CLI chrome: box-drawing
characters (`╭─`, `╰─`, `│`, `┊`), spinner lines (`⚡ preparing tool
details…`), `Warning: Unknown toolsets`, `Query: <prompt>` echoes,
`Resume this session` footers, and `⚠ Deprecated .env settings`
warnings. Cloud callers (Muse) were parsing this and choking on the
unfamiliar unicode.

**Fix**: a `_strip_cli_chrome()` post-processor walks the output line by
line and drops: box corners, vertical-bar lines, horizontal-rule lines,
known chrome prefixes (`Warning:`, `Query:`, `Initializing agent`,
`Thinking...`, `Resume this session with:`, `hermes --resume`, `hermes -c`,
`Session:`, `Title:`, `Duration:`, `Messages:`, etc.), progress-edit
noise, and dashed separators. It also takes the prompt as input and
builds a sliding-window set of prompt fragments; any line containing a
30+ char substring of the prompt is dropped (the CLI sometimes echoes
part of the prompt back, possibly truncated or line-wrapped).

**Lesson**: every CLI tool wrapper needs an explicit output cleaner.
Don't trust the subprocess to emit clean text. The cleaner should be
based on observed output patterns, not regex against the prompt alone.
