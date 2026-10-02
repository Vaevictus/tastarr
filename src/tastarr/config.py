"""Settings, all from TASTARR_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class ArrConfig:
    name: str
    url: str
    api_key: str


@dataclass
class Config:
    transmission_url: str = "http://transmission:9091/transmission/rpc"
    transmission_user: str = ""
    transmission_password: str = ""
    arrs: list[ArrConfig] = field(default_factory=list)
    # Only torrents carrying one of these labels (or saved in a directory of
    # that name) are tasted. Manual downloads - e.g. software, where an .exe
    # is the point - are never touched.
    labels: tuple[str, ...] = ("tv", "movies")
    # "enforce" acts; "dry-run" only logs what it would do and changes nothing.
    mode: str = "enforce"
    interval: int = 15
    # Bytes read from the start of each tasted file.
    head_bytes: int = 8192
    # Queued torrents force-started (past Transmission's queue) to taste them.
    max_bumped: int = 2
    # Stop pushing for the first piece after this long (original settings
    # back, force-start slot returned); the torrent is still tasted when the
    # piece lands, at the latest on completion.
    taste_timeout: int = 2700
    # How long to wait for a rejected torrent to show up in an *arr queue
    # before leaving it stopped for a human.
    owner_grace: int = 900
    # Rejections of releases for the same episode/movie within retry_window
    # after which Tastarr blocklists without searching again (RSS still runs).
    retry_cap: int = 3
    retry_window: int = 86400
    data_dir: str = "/data"
    # "transmission/path=local/path" pairs, comma separated.
    path_map: list[tuple[str, str]] = field(default_factory=list)

    @property
    def dry_run(self) -> bool:
        return self.mode == "dry-run"

    @classmethod
    def from_env(cls, env=None) -> Config:
        env = os.environ if env is None else env

        def get(key, default=""):
            return env.get(f"TASTARR_{key}", default)

        cfg = cls()
        cfg.transmission_url = get("TRANSMISSION_URL", cfg.transmission_url)
        cfg.transmission_user = get("TRANSMISSION_USER")
        cfg.transmission_password = get("TRANSMISSION_PASSWORD")
        for name in ("sonarr", "radarr"):
            url, key = get(f"{name.upper()}_URL"), get(f"{name.upper()}_API_KEY")
            if url and key:
                cfg.arrs.append(ArrConfig(name, url, key))
        labels = get("LABELS")
        if labels:
            cfg.labels = tuple(x.strip() for x in labels.split(",") if x.strip())
        cfg.mode = get("MODE", cfg.mode).strip().lower()
        if cfg.mode not in ("enforce", "dry-run"):
            raise ValueError(f"TASTARR_MODE must be enforce or dry-run, not {cfg.mode!r}")
        for attr in (
            "interval",
            "head_bytes",
            "max_bumped",
            "taste_timeout",
            "owner_grace",
            "retry_cap",
            "retry_window",
        ):
            raw = get(attr.upper())
            if raw:
                setattr(cfg, attr, int(raw))
        cfg.data_dir = get("DATA_DIR", cfg.data_dir)
        for pair in filter(None, (p.strip() for p in get("PATH_MAP").split(","))):
            remote, _, local = pair.partition("=")
            cfg.path_map.append((remote.rstrip("/"), local.rstrip("/")))
        return cfg

    def local_path(self, path: str) -> str:
        for remote, local in self.path_map:
            if path == remote or path.startswith(remote + "/"):
                return local + path[len(remote) :]
        return path
