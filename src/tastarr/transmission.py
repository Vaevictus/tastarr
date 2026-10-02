"""Minimal Transmission JSON-RPC 2.0 client (Transmission >= 4.1, snake_case API)."""

from __future__ import annotations

import base64
import itertools
import json
import urllib.error
import urllib.request
from collections.abc import Iterable
from typing import Any


class TransmissionError(RuntimeError):
    pass


class Transmission:
    def __init__(self, url: str, user: str = "", password: str = "", timeout: float = 30):
        self.url = url
        self.timeout = timeout
        self._session_id = ""
        self._auth = ""
        if user or password:
            token = base64.b64encode(f"{user}:{password}".encode()).decode()
            self._auth = f"Basic {token}"
        self._ids = itertools.count(1)

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
                "id": next(self._ids),
            }
        ).encode()
        for _ in range(2):  # second pass after a 409 hands us the CSRF session id
            req = urllib.request.Request(self.url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            if self._auth:
                req.add_header("Authorization", self._auth)
            if self._session_id:
                req.add_header("X-Transmission-Session-Id", self._session_id)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    self._session_id = exc.headers.get("X-Transmission-Session-Id", "")
                    continue
                raise TransmissionError(f"{method}: HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                raise TransmissionError(f"{method}: {exc.reason}") from exc
            except (OSError, ValueError) as exc:  # timeouts, resets, bad JSON
                raise TransmissionError(f"{method}: {exc}") from exc
            if "error" in payload:
                err = payload["error"]
                detail = (err.get("data") or {}).get("error_string") or ""
                raise TransmissionError(f"{method}: {err.get('message')} {detail}".strip())
            return payload.get("result") or {}
        raise TransmissionError(f"{method}: no session id after 409")

    # -- convenience -------------------------------------------------------

    def session(self, fields: Iterable[str]) -> dict[str, Any]:
        return self.call("session_get", {"fields": list(fields)})

    def torrents(self, fields: Iterable[str], hashes: list[str] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"fields": list(fields)}
        if hashes:
            params["ids"] = list(hashes)
        return self.call("torrent_get", params).get("torrents", [])

    def set(self, hashes: list[str], **fields: Any) -> None:
        self.call("torrent_set", dict(fields, ids=list(hashes)))

    def start(self, hashes: list[str]) -> None:
        self.call("torrent_start", {"ids": list(hashes)})

    def start_now(self, hashes: list[str]) -> None:
        self.call("torrent_start_now", {"ids": list(hashes)})

    def stop(self, hashes: list[str]) -> None:
        self.call("torrent_stop", {"ids": list(hashes)})


def has_piece(bitfield: bytes, index: int) -> bool:
    """`pieces` is a base64 bitfield, most significant bit first."""
    byte = index >> 3
    return byte < len(bitfield) and bool(bitfield[byte] & (0x80 >> (index & 7)))


def decode_pieces(pieces_b64: str) -> bytes:
    return base64.b64decode(pieces_b64 or "")
