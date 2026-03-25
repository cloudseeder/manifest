"""Configuration for OAP Spotify proxy service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class SpotifyConfig:
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://localhost:8306/auth/callback"
    # Scopes needed for taste modeling
    scopes: list[str] = field(default_factory=lambda: [
        "user-top-read",
        "user-read-recently-played",
        "playlist-read-private",
        "playlist-read-collaborative",
        "user-library-read",
        "playlist-modify-private",
        "playlist-modify-public",
    ])
    # Where spotipy caches the OAuth token
    token_cache_path: str = "~/.oap_spotify_token_cache"


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8306
    spotify: SpotifyConfig = field(default_factory=SpotifyConfig)

    @property
    def scopes_str(self) -> str:
        return " ".join(self.spotify.scopes)


_CONFIG_PATHS = [
    Path.cwd() / "config.yaml",
    Path(__file__).parent.parent / "config.yaml",
]

_cfg: Config | None = None


def load_config(path: str | None = None) -> Config:
    global _cfg
    if _cfg is not None and path is None:
        return _cfg

    raw: dict = {}
    candidates = [Path(path)] if path else _CONFIG_PATHS
    for p in candidates:
        if p.exists():
            with open(p) as f:
                raw = yaml.safe_load(f) or {}
            break

    sp_raw = raw.get("spotify", {})
    spotify = SpotifyConfig(
        client_id=os.environ.get("SPOTIFY_CLIENT_ID") or sp_raw.get("client_id", ""),
        client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET") or sp_raw.get("client_secret", ""),
        redirect_uri=sp_raw.get("redirect_uri", "http://localhost:8306/auth/callback"),
        scopes=sp_raw.get("scopes", SpotifyConfig.__dataclass_fields__["scopes"].default_factory()),
        token_cache_path=sp_raw.get("token_cache_path", ".spotify_token_cache"),
    )

    cfg = Config(
        host=raw.get("api", {}).get("host", "127.0.0.1"),
        port=raw.get("api", {}).get("port", 8306),
        spotify=spotify,
    )

    if path is None:
        _cfg = cfg
    return cfg
