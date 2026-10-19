"""JSON state persisted in the repo between runs (committed back by the workflow)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_PATH = Path(os.environ.get("WATCHER_STATE", "state/state.json"))


def load() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"seen": {}, "watchlist": {}, "market": {}, "alerts": []}


def save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def prune(state: dict, keep_days: int = 3) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    for bucket in ("seen", "watchlist"):
        state[bucket] = {
            k: v for k, v in state[bucket].items()
            if datetime.fromisoformat(v["end_time"]) > cutoff
        }
    state["alerts"] = state["alerts"][-300:]
