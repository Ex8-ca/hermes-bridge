#!/usr/bin/env bash
# hermes-bridge telegram approval resolver
#
# Run by hermes cron every 30s. Three jobs:
#   1. Send a Telegram approval request for any pending record that hasn't
#      been notified yet.
#   2. Read recent Telegram updates via getUpdates; flip matching pending
#      records to approved / denied.
#   3. Expire pending records past their TTL.
#
# Conventions:
#   - Pending records: ~/.hermes/bridge/approvals/pending/<uuid>.json
#   - Resolved records: ~/.hermes/bridge/approvals/audit/<date>.jsonl
#   - Reply grammar: "yes <uuid>" approves, "no <uuid>" denies. Plain "yes"
#     without a uuid is silently ignored (forces explicit per-request).
#   - Source allowlist: $TELEGRAM_ALLOWED_USERS comma list.

set -euo pipefail

BRIDGE_HOME="${HERMES_BRIDGE_HOME:-$HOME/.hermes/bridge}"
PENDING="$BRIDGE_HOME/approvals/pending"
AUDIT="$BRIDGE_HOME/approvals/audit"
LOG_PREFIX="[telegram-resolver]"

HERMES_VENV="${HERMES_VENV:-~/.hermes/hermes-agent/venv}"
HERMES="$HERMES_VENV/bin/hermes"

mkdir -p "$PENDING" "$AUDIT"

# Load Telegram creds from .env (only the keys we need; avoid sourcing blindly)
ENV_FILE="$HOME/.hermes/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "$LOG_PREFIX ERROR: $ENV_FILE missing" >&2
  exit 1
fi
TELEGRAM_BOT_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
TELEGRAM_HOME_CHAT=$(grep -E '^TELEGRAM_HOME_CHANNEL=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
BRIDGE_APPROVALS_CHAT=$(grep -E '^TELEGRAM_BRIDGE_APPROVALS_CHANNEL=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
BRIDGE_APPROVALS_THREAD=$(grep -E '^TELEGRAM_BRIDGE_APPROVALS_THREAD=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
ALLOWED=$(grep -E '^TELEGRAM_ALLOWED_USERS=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")

# Approval routing: if TELEGRAM_BRIDGE_APPROVALS_CHANNEL is set, send there.
# Falls back to TELEGRAM_HOME_CHANNEL (which is your DM with the bot — usually wrong).
if [[ -z "${BRIDGE_APPROVALS_CHAT:-}" ]]; then
  BRIDGE_APPROVALS_CHAT="$TELEGRAM_HOME_CHAT"
  echo "$LOG_PREFIX WARNING: TELEGRAM_BRIDGE_APPROVALS_CHANNEL not set, falling back to HOME_CHANNEL ($TELEGRAM_HOME_CHAT)" >&2
fi

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_HOME_CHAT:-}" ]]; then
  echo "$LOG_PREFIX ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_HOME_CHANNEL missing" >&2
  exit 1
fi

NOW=$(date +%s)
LAST_UPDATE_FILE="$BRIDGE_HOME/.last_telegram_update"

# ---------- 1. Send notifications for unnotified pending records ----------
for f in "$PENDING"/*.json; do
  [[ -e "$f" ]] || continue
  rec=$(cat "$f")
  status=$(echo "$rec" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','pending'))")
  notified=$(echo "$rec" | python3 -c "import sys,json; print(json.load(sys.stdin).get('notified_at',0))")
  [[ "$status" == "pending" ]] || continue
  [[ "$notified" -gt 0 ]] && continue

  uuid=$(echo "$rec" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
  tool=$(echo "$rec" | python3 -c "import sys,json; print(json.load(sys.stdin)['tool'])")
  summary=$(echo "$rec" | python3 -c "import sys,json; print(json.load(sys.stdin).get('summary',''))")
  requested_by=$(echo "$rec" | python3 -c "import sys,json; print(json.load(sys.stdin).get('requested_by','unknown'))")
  expires=$(echo "$rec" | python3 -c "import sys,json; print(int(json.load(sys.stdin)['expires_at']))")
  expires_in=$(( expires - NOW ))

  # Build the body via heredoc to avoid bash escaping nightmares in inline strings.
  # Quoted EOF ('EOF') prevents bash from expanding backticks / variables, so we
  # selectively swap in our values with sed instead.
  tmp_body=$(mktemp)
  cat > "$tmp_body" <<EOF
🤖 *[bridge approval — system message]*

Do not reply conversationally. This prompt is for the human to approve or deny.

Tool: \`__TOOL__\`
From: __REQUESTER__
Summary: __SUMMARY__
Request ID: \`__UUID__\`
Expires in: __EXPIRES__s

Reply \`yes __UUID__\` to approve, \`no __UUID__\` to deny. Plain yes/no without the uuid is ignored.
EOF
  sed -i \
    -e "s|__TOOL__|${tool}|g" \
    -e "s|__REQUESTER__|${requested_by}|g" \
    -e "s|__SUMMARY__|${summary}|g" \
    -e "s|__UUID__|${uuid}|g" \
    -e "s|__EXPIRES__|${expires_in}|g" \
    "$tmp_body"
  body=$(cat "$tmp_body")
  rm -f "$tmp_body"

  # Use hermes send — reuses bot credentials + chat routing
  # Build target with optional thread_id for groups with topics
  if [[ -n "${BRIDGE_APPROVALS_THREAD:-}" ]]; then
    SEND_TARGET="telegram:${BRIDGE_APPROVALS_CHAT}:${BRIDGE_APPROVALS_THREAD}"
  else
    SEND_TARGET="telegram:${BRIDGE_APPROVALS_CHAT}"
  fi

  if "$HERMES" send --to "$SEND_TARGET" --subject "🔔 bridge approval: ${tool}" "$body" >/dev/null 2>&1; then
    python3 -c "
import json
p = '$f'
rec = json.load(open(p))
rec['notified_at'] = $NOW
open(p, 'w').write(json.dumps(rec, indent=2))
"
    echo "$LOG_PREFIX notified: $uuid ($tool)"
  else
    echo "$LOG_PREFIX ERROR sending notification for $uuid" >&2
  fi
done

# ---------- 2. Read gateway log for "yes/no <uuid>" replies ----------
#
# We CANNOT call https://api.telegram.org/getUpdates here. The Hermes gateway
# runs in long-polling mode against the same bot token, so Telegram delivers
# every update to the gateway's long-poll and gives us an empty result.
# Instead, the gateway logs every inbound message in $HERMES_HOME/logs/gateway.log
# (or agent.log). We tail that log and extract yes/no replies.
#
# CAVEAT: this means the resolver sees every inbound message, not just the
# approval chat. We filter by $TELEGRAM_BRIDGE_APPROVALS_CHAT and the
# $TELEGRAM_ALLOWED_USERS list, then match "yes <uuid>" / "no <uuid>".

GATEWAY_LOG="${HERMES_GATEWAY_LOG:-$HERMES_HOME/logs/gateway.log}"
LOG_OFFSET_FILE="$BRIDGE_HOME/.last_gateway_log_offset"

if [[ -f "$GATEWAY_LOG" ]]; then
  # Get file size and our last-seen offset; only read what we haven't seen.
  if [[ -f "$LOG_OFFSET_FILE" ]]; then
    LAST=$(cat "$LOG_OFFSET_FILE")
  else
    LAST=0
  fi
  CUR=$(stat -c '%s' "$GATEWAY_LOG")
  if [[ "$CUR" -lt "$LAST" ]]; then
    # log rotated/truncated — start over from the beginning
    LAST=0
  fi
  if [[ "$CUR" -gt "$LAST" ]]; then
    # Read new bytes since $LAST. We use tail -c +N which reads forward in O(1)
    # (no dd bs=1, that was unusably slow over a 4.5MB log).
    #
    # On the very first run (LAST=0), we have no idea where recent yes/no votes
    # are in the file, so cap to last 64KB. On subsequent runs we read exactly
    # the new bytes since the previous offset.
    if [[ "$LAST" -eq 0 ]]; then
      tail -c 65536 "$GATEWAY_LOG" > "$BRIDGE_HOME/.gateway_log_chunk"
    else
      NEW_BYTES=$((CUR - LAST))
      if [[ "$NEW_BYTES" -gt 0 && "$NEW_BYTES" -le 1048576 ]]; then
        tail -c +$((LAST + 1)) "$GATEWAY_LOG" > "$BRIDGE_HOME/.gateway_log_chunk"
      else
        # Anomalously large jump (log rotation or massive catch-up); cap to 64KB
        tail -c 65536 "$GATEWAY_LOG" > "$BRIDGE_HOME/.gateway_log_chunk"
      fi
    fi

    # Parse the chunk. We use a separate Python file (not a heredoc) so that
    # the chunk can be redirected into stdin without competing with the heredoc
    # for the same stream. (See bash semantics: heredoc wins over < redirect.)
    PY_RESOLVER="$BRIDGE_HOME/scripts/_resolve_chunk.py"
    if [[ -f "$PY_RESOLVER" ]]; then
      python3 "$PY_RESOLVER" "$PENDING" "$AUDIT" "$ALLOWED" "$BRIDGE_APPROVALS_CHAT" < "$BRIDGE_HOME/.gateway_log_chunk" || true
    else
      echo "$LOG_PREFIX ERROR: $PY_RESOLVER missing — install the skill" >&2
    fi
    echo "$CUR" > "$LOG_OFFSET_FILE"
    rm -f "$BRIDGE_HOME/.gateway_log_chunk"
  fi
fi

# ---------- 3. Expire pending records past their TTL ----------
for f in "$PENDING"/*.json; do
  [[ -e "$f" ]] || continue
  rec=$(cat "$f")
  expires=$(echo "$rec" | python3 -c "import sys,json; print(int(json.load(sys.stdin).get('expires_at',0)))")
  status=$(echo "$rec" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','pending'))")
  [[ "$status" == "pending" ]] || continue
  if [[ "$NOW" -gt "$expires" ]]; then
    python3 -c "
import json, os, sys
p = '$f'
rec = json.load(open(p))
rec['status'] = 'expired'
day = __import__('datetime').datetime.utcnow().strftime('%Y-%m-%d')
audit = os.path.join('$AUDIT', f'{day}.jsonl')
with open(audit, 'a') as f:
    f.write(json.dumps(rec) + '\n')
os.unlink(p)
print('expired', rec['id'])
"
  fi
done

# ---------- 4. Drain any blocking bridge callers waiting on now-resolved requests ----------
# If a cloud-side request is blocked in the agent loop, the next poll cycle will
# see the file gone and return. Nothing to do here.