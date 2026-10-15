# Changelog

## 0.1.2 (2026-10-15)

- Fix: a queued torrent that Tastarr started early for checking was left stopped
  instead of going back into the queue, both after it cleared and after
  `TASTARR_TASTE_TIMEOUT`. Tastarr stopped and restarted it in one go, but
  Transmission's `torrent-stop` only takes effect on its next internal tick (about 1 s)
  and `torrent-start` ignores a torrent that is still running. Tastarr now stops it and
  starts it again on the next cycle, once Transmission reports it stopped.
- Upgrading: torrents already left stopped by 0.1.0/0.1.1 are not restarted
  automatically. Look for stopped, unfinished `tv`/`movies` torrents in Transmission
  and start them.

## 0.1.1 (2026-10-02)

- Container image published to `ghcr.io/vaevictus/tastarr` for linux/amd64 and
  linux/arm64 on each release. Tags: `latest`, `<major>.<minor>`, `<version>`.
  No code changes.

## 0.1.0 (2026-10-02)

First release.

- File-name check on torrent metadata: dangerous extensions, bidirectional control
  characters, torrents without a video file.
- First-bytes check of the largest video file against an allowlist of video
  container signatures, using Transmission's sequential download from that file's
  first piece.
- Rejected downloads are removed through Sonarr/Radarr with blocklisting and a new
  search, capped per episode/movie.
- Queued torrents are started early for checking, within a limit, and re-queued
  afterwards.
- Dry-run mode, JSON-lines event log, persisted state, container health check.
