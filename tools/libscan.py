#!/usr/bin/env python3
"""Run Tastarr's stage-1 sniffer over every video file in a library.

Checks the allowlist against real files: every playable file should come
back "video". Anything else is either a broken file or a gap in
signatures.py. Reads the first 8 KiB of each file, so on a large library
over a slow disk, point it at one directory at a time.

    python3 tools/libscan.py /media/tv

Exit status is 1 if any file was not recognised.
"""

from __future__ import annotations

import collections
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from tastarr.signatures import is_video_name, sniff  # noqa: E402

HEAD = 8192


def main(roots):
    tally = collections.Counter()
    odd = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "@"))]  # e.g. Synology @eaDir
            for fn in filenames:
                if not is_video_name(fn):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    with open(path, "rb") as fh:
                        head = fh.read(HEAD)
                except OSError as exc:
                    odd.append(("error", str(exc), path))
                    continue
                verdict = sniff(head)
                tally[(verdict.kind, verdict.label)] += 1
                if verdict.kind != "video":
                    odd.append((verdict.kind, verdict.label, path))
    total = sum(tally.values())
    print(f"scanned {total} video-named files")
    for (kind, label), n in tally.most_common():
        print(f"  {n:6d}  {kind:9s} {label}")
    if odd:
        print(f"\n{len(odd)} not recognised as video:")
        for kind, label, path in odd:
            print(f"  {kind:9s} {label:40s} {path}")
    return 0 if not odd else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["."]))
