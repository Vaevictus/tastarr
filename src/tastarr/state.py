"""Per-torrent progress and rejection history, persisted as one JSON file."""

from __future__ import annotations

import json
import os
from typing import Any


class State:
    def __init__(self, path: str):
        self.path = path
        self.torrents: dict[str, dict[str, Any]] = {}
        self.rejections: list[dict[str, Any]] = []

    def load(self) -> State:
        try:
            with open(self.path) as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return self
        self.torrents = raw.get("torrents", {})
        self.rejections = raw.get("rejections", [])
        return self

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"torrents": self.torrents, "rejections": self.rejections}, fh, indent=1)
        os.replace(tmp, self.path)

    def recent_rejections(self, key: str, since: float) -> int:
        return sum(1 for r in self.rejections if r["key"] == key and r["ts"] >= since)

    def prune(self, live_hashes: set, now: float, retry_window: int, keep_gone: int = 7 * 86400) -> None:
        for h, rec in list(self.torrents.items()):
            if h in live_hashes:
                rec["last_seen"] = now
            elif now - rec.get("last_seen", now) > keep_gone:
                del self.torrents[h]
        self.rejections = [r for r in self.rejections if now - r["ts"] <= retry_window]
