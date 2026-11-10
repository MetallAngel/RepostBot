"""Persist one random deadline per quiet spell, independently for each chat."""

import json
import secrets


def ready(store, scope, chat, message_id, at, minimum, spread, now):
    key = f"{scope}_quiet_schedule:{chat}"
    anchor = [int(message_id), int(at), int(minimum), int(spread)]
    try:
        saved = json.loads(store.state(key, "{}"))
    except (ValueError, TypeError):
        saved = {}
    if not isinstance(saved, dict) or saved.get("anchor") != anchor:
        saved = {"anchor": anchor, "due": int(at) + int(minimum) + secrets.randbelow(int(spread) + 1)}
        store.set_state(key, json.dumps(saved))
    return now >= saved["due"]
