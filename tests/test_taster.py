import base64
import copy
import tempfile
import unittest

from tastarr.config import Config
from tastarr.state import State
from tastarr.taster import CLEARED, ORPHANED, REJECT_LABEL, REJECTED, REMOVED, TASTING, WOULD_REJECT, Taster

TMP = tempfile.TemporaryDirectory()


def tearDownModule():
    TMP.cleanup()


MKV = b"\x1a\x45\xdf\xa3" + b"\x01" * 8188
EXE = b"MZ\x90\x00" + b"\x01" * 8188
PIECE = 1 << 20


def bitfield(count, have):
    bits = bytearray((count + 7) // 8)
    for i in have:
        bits[i >> 3] |= 0x80 >> (i & 7)
    return base64.b64encode(bytes(bits)).decode()


def torrent(h, files, have=(), status=4, labels=("tv",), percent=0.1, metadata=1.0):
    total = sum(length for _, length in files)
    count = max(1, (total + PIECE - 1) // PIECE)
    return {
        "hash_string": h,
        "name": files[0][0].split("/")[0],
        "status": status,
        "labels": list(labels),
        "download_dir": "/downloads/" + (labels[0] if labels else "misc"),
        "percent_done": percent,
        "metadata_percent_complete": metadata,
        "files": [{"name": n, "length": length} for n, length in files] if metadata == 1 else [],
        "wanted": [1] * len(files),
        "piece_size": PIECE,
        "pieces": bitfield(count, have),
        "sequential_download": False,
        "sequential_download_from_piece": 0,
    }


class FakeTransmission:
    def __init__(self, *torrents):
        self.t = {t["hash_string"]: t for t in torrents}
        self.calls = []

    def session(self, fields):
        return {"incomplete_dir": "/downloads/incomplete", "incomplete_dir_enabled": True}

    def torrents(self, fields, hashes=None):
        return [copy.deepcopy(t) for t in self.t.values()]

    def set(self, hashes, **fields):
        self.calls.append(("set", hashes[0], fields))
        self.t[hashes[0]].update(fields)

    def start_now(self, hashes):
        self.calls.append(("start_now", hashes[0]))
        self.t[hashes[0]]["status"] = 4

    def start(self, hashes):
        self.calls.append(("start", hashes[0]))
        self.t[hashes[0]]["status"] = 3

    def stop(self, hashes):
        self.calls.append(("stop", hashes[0]))
        self.t[hashes[0]]["status"] = 0


class FakeArr:
    def __init__(self, name, tr, records=()):
        self.name, self.tr = name, tr
        self.records = list(records)
        self.failed = []

    def queue_for(self, info_hash):
        return [r for r in self.records if r["downloadId"].lower() == info_hash.lower()]

    def fail(self, ids, skip_redownload):
        self.failed.append((sorted(ids), skip_redownload))
        gone = {r["downloadId"].lower() for r in self.records if r["id"] in ids}
        self.records = [r for r in self.records if r["id"] not in ids]
        for h in list(self.tr.t):
            if h.lower() in gone:
                del self.tr.t[h]


class Harness:
    def __init__(self, *torrents, records=(), mode="enforce", disk=None, **cfg):
        self.dir = tempfile.mkdtemp(dir=TMP.name)
        self.now = 1_000_000.0
        self.tr = FakeTransmission(*torrents)
        self.sonarr = FakeArr("sonarr", self.tr, records)
        self.cfg = Config(mode=mode, data_dir=self.dir, **cfg)
        self.disk = disk or {}
        self.state = State(self.dir + "/state.json")
        self.taster = Taster(
            self.cfg, self.tr, [self.sonarr], self.state, clock=lambda: self.now, read=self.read
        )

    def read(self, path, size):
        data = self.disk.get(path)
        return None if data is None else data[:size]

    def cycle(self, advance=15):
        self.taster.cycle()
        self.now += advance

    def phase(self, h):
        return self.state.torrents[h]["phase"]


def rec(qid, h, episode):
    return {"id": qid, "downloadId": h.upper(), "episodeId": episode}


class StageZero(unittest.TestCase):
    def test_exe_rejected_and_failed_through_arr(self):
        t = torrent("aa", [("Show.Name.S06E03.1080p.WEB-DL-GROUP.exe", 10 * PIECE)])
        h = Harness(t, records=[rec(7, "aa", 501)])
        h.cycle()
        self.assertEqual(h.phase("aa"), REMOVED)
        self.assertEqual(h.sonarr.failed, [([7], False)])
        self.assertIn(("stop", "aa"), h.tr.calls)
        self.assertEqual(h.state.torrents["aa"]["stage"], "name")

    def test_no_video_file_rejected(self):
        t = torrent("ab", [("Show.S01E01/show.rar", PIECE), ("Show.S01E01/show.r00", PIECE)])
        h = Harness(t, records=[rec(8, "ab", 502)])
        h.cycle()
        self.assertEqual(h.phase("ab"), REMOVED)

    def test_out_of_scope_label_untouched(self):
        t = torrent("ac", [("Some.Game/setup.exe", PIECE)], labels=("software",))
        h = Harness(t)
        h.cycle()
        self.assertNotIn("ac", h.state.torrents)
        self.assertEqual(h.tr.calls, [])

    def test_queued_magnet_bumped_for_metadata(self):
        t = torrent("ad", [("x.mkv", PIECE)], status=3, metadata=0.0)
        h = Harness(t)
        h.cycle()
        self.assertEqual(h.tr.calls, [("start_now", "ad")])


class StageOne(unittest.TestCase):
    def test_exe_disguised_as_mkv(self):
        t = torrent("ba", [("Show.S01E01.mkv", 10 * PIECE)])
        h = Harness(t, records=[rec(9, "ba", 601)])
        h.cycle()  # piece 0 missing: start tasting
        self.assertEqual(h.phase("ba"), TASTING)
        self.assertIn(
            ("set", "ba", {"sequential_download": True, "sequential_download_from_piece": 0}), h.tr.calls
        )
        h.tr.t["ba"]["pieces"] = bitfield(10, [0])
        h.disk["/downloads/incomplete/Show.S01E01.mkv.part"] = EXE
        h.cycle()
        self.assertEqual(h.phase("ba"), REMOVED)
        self.assertIn("pe-executable", h.state.torrents["ba"]["reasons"][0])

    def test_real_mkv_cleared_and_settings_restored(self):
        t = torrent("bb", [("Show.S01E02.mkv", 10 * PIECE)])
        h = Harness(t)
        h.cycle()
        h.tr.t["bb"]["pieces"] = bitfield(10, [0])
        h.disk["/downloads/incomplete/Show.S01E02.mkv.part"] = MKV
        h.cycle()
        self.assertEqual(h.phase("bb"), CLEARED)
        self.assertEqual(
            h.tr.calls[-1], ("set", "bb", {"sequential_download": False, "sequential_download_from_piece": 0})
        )

    def test_zero_filled_read_waits(self):
        t = torrent("bc", [("Show.S01E03.mkv", 10 * PIECE)], have=[0])
        h = Harness(t, disk={"/downloads/incomplete/Show.S01E03.mkv.part": b"\x00" * 8192})
        h.cycle()
        self.assertEqual(h.phase("bc"), TASTING)

    def test_completed_torrent_read_from_download_dir(self):
        t = torrent("bd", [("Show.S01E04.mkv", 4 * PIECE)], have=range(4), status=6, percent=1.0)
        h = Harness(t, disk={"/downloads/tv/Show.S01E04.mkv": MKV})
        h.cycle()
        self.assertEqual(h.phase("bd"), CLEARED)
        self.assertEqual(h.tr.calls, [])  # nothing to change for a finished torrent

    def test_pack_tastes_largest_video_from_its_own_piece(self):
        files = [("Pack/S01E01.mkv", 3 * PIECE), ("Pack/S01E02.mkv", 5 * PIECE), ("Pack/info.nfo", 100)]
        h = Harness(torrent("be", files))
        h.cycle()
        self.assertEqual(h.state.torrents["be"]["target"], "Pack/S01E02.mkv")
        self.assertEqual(h.tr.calls[0][2]["sequential_download_from_piece"], 3)

    def test_timeout_relaxes_but_keeps_watching(self):
        h = Harness(torrent("bf", [("Show.S01E05.mkv", 10 * PIECE)], status=3), taste_timeout=60)
        h.cycle(advance=61)
        self.assertEqual(h.tr.calls[-1], ("start_now", "bf"))
        h.cycle()
        self.assertEqual(h.phase("bf"), TASTING)
        self.assertEqual(
            h.tr.calls[-3:],
            [
                ("set", "bf", {"sequential_download": False, "sequential_download_from_piece": 0}),
                ("stop", "bf"),
                ("start", "bf"),
            ],
        )
        h.tr.t["bf"]["status"] = 3
        h.cycle()
        self.assertEqual(len(h.tr.calls), 5)  # relaxed: not force-started again
        h.tr.t["bf"]["pieces"] = bitfield(10, range(10))  # completed: tasted at last
        h.disk["/downloads/incomplete/Show.S01E05.mkv.part"] = EXE
        h.cycle()
        self.assertEqual(h.phase("bf"), REJECTED)


class Queue(unittest.TestCase):
    def test_bump_budget_and_requeue(self):
        ts = [torrent(f"c{i}", [(f"Show.S01E0{i}.mkv", 4 * PIECE)], status=3) for i in range(3)]
        h = Harness(*ts, max_bumped=2)
        h.cycle()
        bumped = [c[1] for c in h.tr.calls if c[0] == "start_now"]
        self.assertEqual(bumped, ["c0", "c1"])
        h.tr.t["c0"]["pieces"] = bitfield(4, [0])
        h.disk["/downloads/incomplete/Show.S01E00.mkv.part"] = MKV
        h.cycle()
        self.assertEqual(h.phase("c0"), CLEARED)
        tail = h.tr.calls[-4:]
        self.assertIn(("stop", "c0"), tail)
        self.assertIn(("start", "c0"), tail)  # re-queued
        self.assertIn(("start_now", "c2"), tail)  # freed slot used


class Disposal(unittest.TestCase):
    def test_retry_cap_stops_research(self):
        h = Harness(retry_cap=3)
        for i, qid in enumerate((1, 2, 3)):
            hh = f"d{i}"
            h.tr.t[hh] = torrent(hh, [(f"Show.Name.S01E08.v{i}.exe", PIECE)])
            h.sonarr.records.append(rec(qid, hh, 30292))
            h.cycle()
        self.assertEqual([skip for _, skip in h.sonarr.failed], [False, False, True])

    def test_unowned_reject_waits_then_orphans(self):
        h = Harness(torrent("e1", [("Fake.S01E01.exe", PIECE)]), owner_grace=100)
        h.cycle()
        self.assertEqual(h.phase("e1"), REJECTED)
        self.assertIn(("set", "e1", {"labels": ["tv", REJECT_LABEL]}), h.tr.calls)
        h.cycle(advance=101)
        h.cycle()
        self.assertEqual(h.phase("e1"), ORPHANED)
        self.assertIn("e1", h.tr.t)  # left for a human

    def test_late_arr_claim_is_honoured(self):
        h = Harness(torrent("e2", [("Fake.S01E02.exe", PIECE)]))
        h.cycle()
        self.assertEqual(h.phase("e2"), REJECTED)
        h.sonarr.records.append(rec(11, "e2", 700))
        h.cycle()
        self.assertEqual(h.phase("e2"), REMOVED)


class DryRun(unittest.TestCase):
    def test_changes_nothing(self):
        bad = torrent("f1", [("Fake.S01E01.exe", PIECE)], status=3)
        slow = torrent("f2", [("Real.S01E01.mkv", 4 * PIECE)], status=3)
        h = Harness(bad, slow, records=[rec(1, "f1", 1)], mode="dry-run")
        h.cycle()
        h.cycle()
        self.assertEqual(h.tr.calls, [])
        self.assertEqual(h.sonarr.failed, [])
        self.assertEqual(h.phase("f1"), WOULD_REJECT)
        self.assertEqual(h.phase("f2"), TASTING)


if __name__ == "__main__":
    unittest.main()
