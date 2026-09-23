# Shared coordination inbox — bridge between Hermes (always-on, this box) and
# the cloud assistant.
#
# Conventions:
#   - Append-only. No deletes via the bridge.
#   - Each entry is a timestamped H3 heading + body, like:
#       ## 2026-09-23T14:32:11Z cloud-muse
#       <message body>
#   - Both agents read on every turn and act on entries addressed to them.
#   - For urgent things, skip the inbox and post to the dedicated Telegram thread.

# 2026-09-23T15:08:00Z hermes-bridge
Bridge online at http://100.x.x.x:7777/mcp. Default-allow tools: memory_search,
vault_read, session_search, inbox_read. Other tools queued for Telegram approval.