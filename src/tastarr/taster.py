"""The tasting loop.

Every cycle, for each in-scope torrent:

  stage 0  as soon as metadata is in: refuse it if any file name is dangerous
           (``.exe``, ``.lnk``, bidi tricks, ...) or there is no video file.
  stage 1  taste the first ``head_bytes`` of the main video file. If those
           pieces aren't here yet, switch the torrent to sequential download
           starting at that file's first piece and (if it is stuck in
           Transmission's queue) force-start it. Once the bytes land: a video
           container clears it, anything else rejects it.

Uploads are deliberately left alone while tasting: Transmission counts its
own piece requests against a torrent's upload limit, so ``upload_limit 0``
stalls the download completely. If the first piece is late despite
sequential mode, the verdict simply comes later. After ``taste_timeout``
Tastarr stops pushing (original settings back, force-start slot returned) but
keeps watching: by completion every piece is there, so every torrent gets a
verdict before the *arr imports it.

A rejected torrent is stopped at once, then failed through the *arr that
owns it (remove from client + blocklist + search again, with a cap), so the
*arr knows why it vanished. A cleared torrent gets its original settings back
and, if it was force-started, goes back into the queue: stopped now, started
again next cycle. Transmission's ``torrent-stop`` only takes effect on its next
idle tick and ``torrent-start`` ignores a torrent that is still running, so
both in one go would leave it stopped.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from . import signatures
from .arr import Arr, ArrError, item_keys
from .config import Config
from .state import State
from .transmission import TransmissionError, decode_pieces, has_piece

log = logging.getLogger("tastarr")

FIELDS = [
    "hash_string",
    "name",
    "status",
    "labels",
    "download_dir",
    "percent_done",
    "metadata_percent_complete",
    "files",
    "wanted",
    "pieces",
    "piece_size",
    "sequential_download",
    "sequential_download_from_piece",
]
SESSION_FIELDS = ["incomplete_dir", "incomplete_dir_enabled"]
RESTORABLE = ("sequential_download", "sequential_download_from_piece")

# Transmission torrent status
STOPPED, CHECK_WAIT, CHECK, QUEUED_DOWNLOAD = 0, 1, 2, 3

NEW, TASTING = "new", "tasting"
REJECTED = "rejected"  # stopped, waiting for an *arr to claim it
CLEARED, REMOVED, ORPHANED, WOULD_REJECT = "cleared", "removed", "orphaned", "would-reject"
FINAL = frozenset({CLEARED, REMOVED, ORPHANED, WOULD_REJECT})
REJECT_LABEL = "tastarr-rejected"

Target = tuple[int, dict[str, Any], int]  # file index, file, byte offset in torrent


def read_head(path: str, size: int) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read(size)
    except FileNotFoundError:
        return None


def name_reasons(files: list[dict[str, Any]]) -> list[str]:
    reasons = []
    for f in files:
        problem = signatures.name_problem(f["name"])
        if problem:
            reasons.append(f"{f['name']}: {problem}")
    return reasons


def main_video(t: dict[str, Any]) -> Target | None:
    """The largest wanted video file: the thing the *arr will actually import."""
    wanted = t.get("wanted") or []
    best: Target | None = None
    offset = 0
    for i, f in enumerate(t["files"]):
        is_wanted = bool(wanted[i]) if i < len(wanted) else True
        if is_wanted and signatures.is_video_name(f["name"]):
            if best is None or f["length"] > best[1]["length"]:
                best = (i, f, offset)
        offset += f["length"]
    return best


class Taster:
    def __init__(
        self,
        cfg: Config,
        transmission,
        arrs: list[Arr],
        state: State,
        clock: Callable[[], float] = time.time,
        read: Callable[[str, int], bytes | None] = read_head,
    ):
        self.cfg = cfg
        self.tr = transmission
        self.arrs = arrs
        self.state = state
        self.clock = clock
        self.read = read
        self.events_path = os.path.join(cfg.data_dir, "events.jsonl")

    # -- loop ------------------------------------------------------------------

    def cycle(self) -> None:
        now = self.clock()
        session = self.tr.session(SESSION_FIELDS)
        torrents = self.tr.torrents(FIELDS)
        live = {t["hash_string"].lower() for t in torrents}
        for h, rec in self.state.torrents.items():
            if h not in live:
                rec["bumped"] = False  # gone: give its force-start slot back
        for t in torrents:
            h = t["hash_string"].lower()
            if not self.in_scope(t):
                continue
            rec = self.state.torrents.get(h)
            if rec is None:
                rec = self.state.torrents[h] = {
                    "name": t["name"],
                    "phase": NEW,
                    "first_seen": now,
                    "last_seen": now,
                }
            try:
                if rec.get("requeue"):
                    self.requeue(t, rec)
                if rec["phase"] in FINAL:
                    continue
                self.step(t, rec, session, now)
            except (TransmissionError, ArrError, OSError) as exc:
                log.warning("%s: %s (will retry)", t["name"], exc)
        self.state.prune(live, now, self.cfg.retry_window)
        self.state.save()
        with open(os.path.join(self.cfg.data_dir, "heartbeat"), "w") as fh:
            fh.write(f"{now:.0f}\n")

    def in_scope(self, t: dict[str, Any]) -> bool:
        if set(t.get("labels") or []) & set(self.cfg.labels):
            return True
        return os.path.basename((t.get("download_dir") or "").rstrip("/")) in self.cfg.labels

    def step(self, t: dict[str, Any], rec: dict[str, Any], session: dict[str, Any], now: float) -> None:
        if rec["phase"] == REJECTED:
            self.dispose(t, rec, now)
            return

        # Stage 0: names. A magnet stuck in the queue has no file list yet, so
        # bump it to fetch the metadata.
        if (t.get("metadata_percent_complete") or 0) < 1 or not t.get("files"):
            self.bump(t, rec)
            return
        if not rec.get("named"):
            reasons = name_reasons(t["files"])
            if reasons:
                self.reject(t, rec, now, "name", reasons)
                return
            rec["named"] = True
        target = main_video(t)
        if target is None:
            self.reject(t, rec, now, "name", ["no video file in torrent"])
            return

        # Stage 1: bytes.
        verdict = self.taste(t, session, target)
        if verdict is None:
            if rec["phase"] == NEW:
                self.begin_tasting(t, rec, target, now)
            elif now - rec["tasting_since"] > self.cfg.taste_timeout:
                if not rec.get("relaxed"):
                    self.relax(t, rec)
            else:
                self.bump(t, rec)
            return
        if verdict.kind == "video":
            self.finish(t, rec, CLEARED, verdict.label)
        else:
            self.reject(
                t,
                rec,
                now,
                "bytes",
                [f"{target[1]['name']}: starts like {verdict.label}, not a video container"],
            )

    # -- stage 1 -----------------------------------------------------------------

    def taste(self, t: dict[str, Any], session: dict[str, Any], target: Target) -> signatures.Sniff | None:
        """Sniff the target file's head, or None if those bytes aren't on disk yet."""
        _, f, offset = target
        need = min(self.cfg.head_bytes, f["length"])
        piece_size = t["piece_size"]
        bits = decode_pieces(t.get("pieces", ""))
        if not all(
            has_piece(bits, p) for p in range(offset // piece_size, (offset + need - 1) // piece_size + 1)
        ):
            return None
        for path in self.candidates(t, session, f["name"]):
            head = self.read(path, need)
            if head is None:
                continue
            verdict = signatures.sniff(head)
            # Zero-filled: the piece is verified but may still be in
            # Transmission's write cache (preallocated file). Try next cycle.
            return None if verdict.kind == "empty" else verdict
        return None

    def candidates(self, t: dict[str, Any], session: dict[str, Any], name: str) -> list[str]:
        dirs = []
        if session.get("incomplete_dir_enabled") and session.get("incomplete_dir"):
            dirs.append(session["incomplete_dir"])
        dirs.append(t["download_dir"])
        paths = []
        for d in dirs:
            base = self.cfg.local_path(os.path.join(d, name))
            paths += [base + ".part", base]
        return paths

    def begin_tasting(self, t: dict[str, Any], rec: dict[str, Any], target: Target, now: float) -> None:
        _, f, offset = target
        first_piece = offset // t["piece_size"]
        rec.update(phase=TASTING, tasting_since=now, target=f["name"])
        if self.cfg.dry_run:
            log.info("would taste %s (piece %d)", f["name"], first_piece)
            return
        rec["orig"] = {k: t.get(k) for k in RESTORABLE}
        self.tr.set([t["hash_string"]], sequential_download=True, sequential_download_from_piece=first_piece)
        log.info("tasting %s: sequential from piece %d", t["name"], first_piece)
        self.bump(t, rec)

    def bump(self, t: dict[str, Any], rec: dict[str, Any]) -> None:
        """Force-start a queued torrent so it can be tasted now, within a budget."""
        if self.cfg.dry_run or rec.get("bumped") or rec.get("relaxed") or t.get("status") != QUEUED_DOWNLOAD:
            return
        active = sum(1 for r in self.state.torrents.values() if r.get("bumped"))
        if active >= self.cfg.max_bumped:
            return
        self.tr.start_now([t["hash_string"]])
        rec["bumped"] = True
        log.info("force-started queued %s to taste it", t["name"])

    def restore(self, t: dict[str, Any], rec: dict[str, Any]) -> None:
        """Undo begin_tasting/bump: original settings back, force-start slot returned."""
        h = t["hash_string"]
        if not self.cfg.dry_run:
            orig = {k: v for k, v in (rec.pop("orig", None) or {}).items() if v is not None}
            if orig:
                self.tr.set([h], **orig)
            if rec.get("bumped") and (t.get("percent_done") or 0) < 1:
                self.tr.stop([h])
                rec["requeue"] = True  # started again once the stop has happened
        rec["bumped"] = False

    def requeue(self, t: dict[str, Any], rec: dict[str, Any]) -> None:
        """Second half of restore(): start a bumped torrent again, through the queue."""
        if t.get("status") in (CHECK_WAIT, CHECK):
            return  # Transmission ignores a start while verifying; try next cycle
        if t.get("status") == STOPPED:
            self.tr.start([t["hash_string"]])
            log.info("re-queued %s", t["name"])
        # Anything else: it was started again by someone else; leave it.
        rec.pop("requeue", None)

    def relax(self, t: dict[str, Any], rec: dict[str, Any]) -> None:
        """Stop pushing for the first piece, but keep watching for it."""
        self.restore(t, rec)
        rec["relaxed"] = True
        log.info(
            "still waiting for the first piece of %s after %ds; settings restored, "
            "will taste it when it lands",
            t["name"],
            self.cfg.taste_timeout,
        )

    def finish(self, t: dict[str, Any], rec: dict[str, Any], phase: str, detail: str) -> None:
        self.restore(t, rec)
        rec.update(phase=phase, verdict=detail, decided_at=self.clock())
        log.info("%s %s: %s", phase.upper(), t["name"], detail)
        self.event(t, phase, detail)

    # -- rejection -------------------------------------------------------------

    def reject(
        self, t: dict[str, Any], rec: dict[str, Any], now: float, stage: str, reasons: list[str]
    ) -> None:
        h = t["hash_string"]
        detail = "; ".join(reasons)
        rec.update(stage=stage, reasons=reasons, rejected_at=now, bumped=False)
        rec.pop("requeue", None)  # stays stopped
        if self.cfg.dry_run:
            rec["phase"] = WOULD_REJECT
            log.warning("WOULD REJECT %s (%s): %s", t["name"], stage, detail)
            self.event(t, WOULD_REJECT, detail)
            return
        rec["phase"] = REJECTED
        log.warning("REJECT %s (%s): %s", t["name"], stage, detail)
        self.event(t, "rejected", detail)
        self.tr.stop([h])
        labels = list(t.get("labels") or [])
        if REJECT_LABEL not in labels:
            self.tr.set([h], labels=labels + [REJECT_LABEL])
        self.dispose(t, rec, now)

    def dispose(self, t: dict[str, Any], rec: dict[str, Any], now: float) -> None:
        """Fail the download through its *arr: removes it, blocklists it, searches again."""
        h = t["hash_string"].lower()
        for arr in self.arrs:
            records = arr.queue_for(h)
            if not records:
                continue
            keys = item_keys(arr.name, records)
            since = now - self.cfg.retry_window
            skip = any(self.state.recent_rejections(k, since) >= self.cfg.retry_cap - 1 for k in keys)
            arr.fail([r["id"] for r in records], skip_redownload=skip)
            self.state.rejections.extend({"key": k, "ts": now, "hash": h} for k in keys)
            rec.update(phase=REMOVED, owner=arr.name, skip_redownload=skip)
            what = "blocklisted, not searching again (retry cap)" if skip else "blocklisted, searching again"
            log.warning("REMOVED %s via %s: %s", t["name"], arr.name, what)
            self.event(t, REMOVED, f"{arr.name}: {what}")
            return
        if now - rec["rejected_at"] > self.cfg.owner_grace:
            rec["phase"] = ORPHANED
            detail = f"no *arr claimed it; left stopped and labelled {REJECT_LABEL}"
            log.warning("ORPHANED %s: %s", t["name"], detail)
            self.event(t, ORPHANED, detail)

    def event(self, t: dict[str, Any], kind: str, detail: str) -> None:
        line = {
            "ts": round(self.clock()),
            "event": kind,
            "hash": t["hash_string"].lower(),
            "name": t["name"],
            "detail": detail,
        }
        with open(self.events_path, "a") as fh:
            fh.write(json.dumps(line) + "\n")
