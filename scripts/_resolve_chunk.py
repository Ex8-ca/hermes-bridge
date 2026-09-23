#!/usr/bin/env python3
"""Parse a chunk of the gateway log and resolve any matching yes/no <uuid> replies.

Reads stdin (the chunk), processes each line looking for gateway.run inbound
message entries, and resolves pending bridge approval records.

The bash caller (telegram_resolver.sh) feeds the chunk via stdin redirection.
This script is invoked as:

    python3 _resolve_chunk.py PENDING_DIR AUDIT_DIR ALLOWED_USERS APPROVAL_CHAT < chunk
"""

import json
import os
import re
import sys
from datetime import datetime


def main():
    if len(sys.argv) != 5:
        sys.stderr.write("usage: _resolve_chunk.py PENDING AUDIT ALLOWED CHAT\n")
        sys.exit(2)

    pending_dir = sys.argv[1]
    audit_dir = sys.argv[2]
    allowed = sys.argv[3]
    approval_chat = sys.argv[4]

    allowed_set = set(x.strip() for x in allowed.split(',') if x.strip())

    # Gateway log format:
    # 2026-09-23 15:46:08,070 INFO gateway.run: inbound message: platform=telegram
    #     user=Marc chat=-1003743484579 msg='yes <uuid>' reply_to_id=5 reply_to_text=''
    pattern = re.compile(
        r"^(\S+ \S+).*inbound message:.*chat=([\-\d]+) msg='([^']*)'",
    )

    resolved_count = 0
    for line in sys.stdin:
        m = pattern.match(line)
        if not m:
            continue
        chat = m.group(2)
        text = m.group(3).strip().lower()
        if str(chat) != str(approval_chat):
            continue

        parts = text.split()
        if len(parts) != 2:
            continue
        verb, uuid = parts
        if verb not in ('yes', 'no'):
            continue
        # Accept any reasonable uuid length. Real cloud-side calls produce
        # 12-hex by default; humans hand-typing test ids may produce 8-20.
        if not (4 <= len(uuid) <= 64):
            continue

        pending_path = os.path.join(pending_dir, f"{uuid}.json")
        if not os.path.exists(pending_path):
            continue

        rec = json.load(open(pending_path))
        if rec.get('status') != 'pending':
            continue

        # In gateway-log-tail mode we don't have user.id from Telegram
        # directly. We rely on (1) chat filter to approval chat, (2) uuid
        # match against pending, (3) TTL of 10 min. For higher assurance,
        # swap to gateway inbox persistence (which carries from.id).
        if verb == 'yes':
            rec['status'] = 'approved'
            rec['approved_via'] = 'gateway_log_tail'
            rec['approved_at'] = int(datetime.utcnow().timestamp())
        else:
            rec['status'] = 'denied'
            rec['denied_via'] = 'gateway_log_tail'
            rec['denied_at'] = int(datetime.utcnow().timestamp())

        # Audit trail
        day = datetime.utcnow().strftime('%Y-%m-%d')
        audit_path = os.path.join(audit_dir, f"{day}.jsonl")
        with open(audit_path, 'a') as f:
            f.write(json.dumps(rec) + "\n")

        # Remove from pending
        os.unlink(pending_path)
        print(f"resolved {uuid} -> {rec['status']}")
        resolved_count += 1

    if resolved_count == 0:
        sys.stderr.write(f"[telegram-resolver] scanned chunk, no matches\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
