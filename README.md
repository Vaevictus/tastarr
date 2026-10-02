# Tastarr

[![CI](https://github.com/Vaevictus/tastarr/actions/workflows/ci.yml/badge.svg)](https://github.com/Vaevictus/tastarr/actions/workflows/ci.yml)

Tastarr checks the downloads Sonarr and Radarr send to Transmission and removes
the ones that are not video. It catches torrents containing executables or
scripts, and video files whose first bytes are not a known video container. A
rejected download is failed through the Sonarr or Radarr instance that grabbed it,
so the release is blocklisted and a new search runs.

## Background

Public trackers carry fake releases of newly aired episodes. A common form is a
single Windows executable named like a scene release:

```
Show.Name.S06E03.1080p.WEB-DL.DDP5.1.x265-GROUP.exe
```

Indexers often list these without the `.exe`, so release profiles and custom
formats cannot reject them before the grab. Sonarr and Radarr can fail downloads
that contain executables (the **Fail Downloads** option on each indexer), but only
after the download has finished. Until then the fake sits in the queue, and the real
release for that episode is not grabbed.

Tastarr makes the same decision as early as the data allows. It checks the file
list as soon as the torrent's metadata arrives, and the first few kilobytes of the
video file shortly after.

## How it works

Tastarr polls Transmission's RPC every 15 seconds. It only looks at torrents carrying
one of the configured labels (default `tv` and `movies`, the usual Sonarr/Radarr
categories); other torrents are never touched.

1. **File names**, checked as soon as metadata is available, before any data is
   downloaded. The torrent is rejected if:
   - any file has a dangerous extension (`.exe`, `.scr`, `.lnk`, `.msi`, `.bat`,
     `.cmd`, `.ps1`, `.vbs`, `.js`, `.hta`, `.jar`, `.zipx`, …; see `DANGEROUS_EXTS`
     in [`signatures.py`](src/tastarr/signatures.py)),
   - a file name contains a Unicode bidirectional control character (used to make
     `…<U+202E>vkm.exe` display as `…exe.mkv`), or
   - the torrent contains no video file.

2. **First bytes.** Tastarr switches the torrent to sequential download starting at
   the first piece of its largest video file. Once those pieces are complete, it reads
   the first 8 KiB from disk. The torrent is rejected unless the bytes match a known
   container: Matroska/WebM, ISO-BMFF (MP4, MOV), AVI, ASF/WMV, MPEG-PS/ES,
   MPEG-TS/M2TS, FLV, Ogg or RealMedia. This is an allowlist. Executable and archive
   signatures (`MZ`, ELF, ZIP, RAR, 7z, OLE/MSI, LNK, `#!`) are named in the log, and
   anything unrecognised is rejected as well.

   A torrent waiting in Transmission's download queue is started straight away so it
   can be checked (at most `TASTARR_MAX_BUMPED` at a time), then put back in the queue
   once it passes. A queued magnet link is started for the same reason, to fetch its
   file list.

3. **Outcome**
   - **Cleared.** The torrent's original sequential-download setting is restored.
   - **Rejected.** The torrent is stopped, labelled `tastarr-rejected`, and removed
     through the Sonarr/Radarr instance that owns it, using
     `DELETE /api/v3/queue/bulk?removeFromClient=true&blocklist=true`. The *arr
     deletes the data, blocklists the release and searches again. Removing the
     torrent from Transmission directly would not blocklist it.
   - **Repeat fakes.** If the same episode or movie has had `TASTARR_RETRY_CAP`
     releases rejected within `TASTARR_RETRY_WINDOW`, the release is blocklisted
     without a new search (`skipRedownload=true`). RSS sync picks up a real release
     later.
   - **No owner.** If no Sonarr or Radarr queue entry matches the torrent within
     `TASTARR_OWNER_GRACE`, it is left stopped and labelled, for manual review.
   - **Slow first piece.** If the first piece has not arrived after
     `TASTARR_TASTE_TIMEOUT`, Tastarr restores the torrent's settings and stops
     pushing, but still checks the torrent when the piece arrives. That happens at the
     latest on completion, before the *arr imports it.

Sonarr and Radarr need no configuration changes. Tastarr finds the owner of a torrent
by matching its info hash against the `downloadId` of each queue entry, and only acts
on downloads the *arr grabbed itself (not "unknown" queue items).

Tastarr complements the *arrs' own **Fail Downloads** option rather than replacing
it. Keep that option enabled as a second check.

## Requirements

- Transmission 4.1 or later (JSON-RPC 2.0 API and `sequential_download_from_piece`)
- Sonarr v4 and/or Radarr (API v3). Tested with Sonarr 4.0.20 and Radarr 6.4.4.
- Read access to Transmission's download and incomplete directories, at the paths
  Transmission reports (or mapped with `TASTARR_PATH_MAP`)
- Python 3.10 or later, standard library only

## Installation

### Docker

```sh
git clone https://github.com/Vaevictus/tastarr.git
cd tastarr
cp .env.example .env                  # credentials
cp compose.example.yaml compose.yaml  # URLs, user, volumes
docker compose up -d --build
```

In `compose.yaml`:

- Mount the download directory read-only, at the path Transmission reports. For the
  linuxserver.io image that is `/downloads`, which also contains the incomplete
  directory by default.
- Run as the same UID/GID as Transmission, so partial files are readable.
- `/data` holds state and logs, and must be writable by that user.

### From source

```sh
pip install .
export TASTARR_DATA_DIR=./data TASTARR_TRANSMISSION_URL=http://localhost:9091/transmission/rpc
tastarr --once --dry-run
```

## Configuration

All settings are environment variables.

| Variable | Default | Description |
|---|---|---|
| `TASTARR_MODE` | `enforce` | `dry-run` logs `WOULD REJECT` and `would taste` lines and changes nothing |
| `TASTARR_TRANSMISSION_URL` | `http://transmission:9091/transmission/rpc` | Transmission RPC endpoint |
| `TASTARR_TRANSMISSION_USER`, `TASTARR_TRANSMISSION_PASSWORD` | | RPC basic auth, if enabled |
| `TASTARR_SONARR_URL`, `TASTARR_SONARR_API_KEY` | | Sonarr base URL and API key |
| `TASTARR_RADARR_URL`, `TASTARR_RADARR_API_KEY` | | Radarr base URL and API key |
| `TASTARR_LABELS` | `tv,movies` | Torrents with one of these labels, or saved in a directory with one of these names, are checked |
| `TASTARR_INTERVAL` | `15` | Seconds between cycles |
| `TASTARR_HEAD_BYTES` | `8192` | Bytes read from the start of the video file |
| `TASTARR_MAX_BUMPED` | `2` | Queued torrents started early for checking, at once |
| `TASTARR_TASTE_TIMEOUT` | `2700` | Seconds before Tastarr stops pushing for the first piece (it keeps watching) |
| `TASTARR_OWNER_GRACE` | `900` | Seconds to wait for a rejected torrent to appear in a Sonarr/Radarr queue |
| `TASTARR_RETRY_CAP` | `3` | Rejections per episode/movie after which no new search is triggered |
| `TASTARR_RETRY_WINDOW` | `86400` | Window for `TASTARR_RETRY_CAP`, in seconds |
| `TASTARR_DATA_DIR` | `/data` | State, log and heartbeat directory |
| `TASTARR_PATH_MAP` | | `remote=local` prefix pairs, comma-separated, if Tastarr sees the download directory at a different path than Transmission |

Start with `TASTARR_MODE=dry-run` and read the log before switching to `enforce`.

## Files

`TASTARR_DATA_DIR` contains:

| File | Contents |
|---|---|
| `tastarr.log` | Log, rotated at 5 MB. Also written to stdout. |
| `events.jsonl` | One JSON line per verdict: `cleared`, `rejected`, `removed`, `orphaned`, `would-reject` |
| `state.json` | Per-torrent progress and rejection history. Lets a restart resume mid-check. |
| `heartbeat` | Time of the last completed cycle, read by `tastarr --healthcheck` |

## Transmission behaviour worth knowing

- **Do not set a torrent's upload limit to 0** to avoid sharing a suspected fake.
  Transmission counts outgoing piece requests against the upload limit, so the
  download stalls at 0 B/s. An earlier version of Tastarr did this. A torrent with 33
  connected seeders received nothing until the limit was lifted, then downloaded at
  15 MB/s.
- **A piece can be marked complete while still in the write cache,** so the file on
  disk may read as zeros. Tastarr treats an all-zero read as "not yet" and retries on
  the next cycle.
- **Sequential download is followed closely when uploads flow.** In testing, a torrent
  fetched pieces 0–51 in order and was cleared about two minutes after the switch.

## Limitations

- Only the container signature is checked. A real video of the wrong title, a short
  decoy clip, or a crafted file aimed at a media-parser bug will pass. Comparing the
  `ffprobe` duration with the expected runtime would catch decoys.
- Only the largest video file in a torrent is read. The other files are covered by the
  name check.
- Transmission is the only supported download client.

## Development

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
ruff check .
```

The tests run the main loop against fake Transmission and *arr clients. They cover:

- name and byte rejections, and an executable disguised as `.mkv`
- settings restore
- the queue force-start budget
- the re-search cap and orphaned rejects
- the timeout
- that dry-run mode changes nothing

[`tools/libscan.py`](tools/libscan.py) runs the byte check over an existing library and
lists any video file it does not recognise. On a 13,672-file TV library it recognised
every file (Matroska 6,983, MP4 3,674, AVI 2,982, FLV 24, MPEG-PS 5, WMV 4).

## License

[MIT](LICENSE)
