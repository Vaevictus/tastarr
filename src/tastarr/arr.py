"""Sonarr / Radarr v3 API: find who owns a torrent, and fail it the *arr way."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ArrError(RuntimeError):
    pass


class Arr:
    def __init__(self, name: str, url: str, api_key: str, timeout: float = 60):
        self.name = name
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _request(
        self, method: str, path: str, query: dict[str, Any] | None = None, body: dict[str, Any] | None = None
    ) -> Any:
        url = f"{self.url}/api/v3/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Api-Key", self.api_key)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise ArrError(f"{self.name} {method} {path}: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ArrError(f"{self.name} {method} {path}: {exc.reason}") from exc
        except OSError as exc:  # timeouts, resets
            raise ArrError(f"{self.name} {method} {path}: {exc}") from exc
        try:
            return json.loads(raw) if raw else None
        except ValueError as exc:
            raise ArrError(f"{self.name} {method} {path}: invalid JSON") from exc

    def queue_for(self, info_hash: str) -> list[dict[str, Any]]:
        """Queue records belonging to one torrent (a season pack has one per episode).

        Only downloads the *arr grabbed itself: an "unknown" item (a manual
        torrent in its category) has no grab to blocklist, so those are left
        to the orphan path.
        """
        page = self._request("GET", "queue", {"page": 1, "pageSize": 2000})
        wanted = info_hash.lower()
        return [r for r in page.get("records", []) if (r.get("downloadId") or "").lower() == wanted]

    def fail(self, queue_ids: list[int], skip_redownload: bool) -> None:
        """Remove from the client, blocklist the release, and (unless told not to) search again."""
        self._request(
            "DELETE",
            "queue/bulk",
            {
                "removeFromClient": "true",
                "blocklist": "true",
                "skipRedownload": "true" if skip_redownload else "false",
            },
            {"ids": queue_ids},
        )


def item_keys(arr_name: str, records: list[dict[str, Any]]) -> list[str]:
    """Stable ids for what the download was *for*, used by the re-search cap."""
    keys = []
    for r in records:
        if r.get("episodeId"):
            keys.append(f"{arr_name}:episode:{r['episodeId']}")
        elif r.get("movieId"):
            keys.append(f"{arr_name}:movie:{r['movieId']}")
    return sorted(set(keys))
