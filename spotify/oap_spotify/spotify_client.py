"""Spotipy OAuth wrapper for OAP Spotify proxy."""

from __future__ import annotations

import logging
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from .config import SpotifyConfig

log = logging.getLogger("oap.spotify")


class SpotifyClient:
    def __init__(self, cfg: SpotifyConfig) -> None:
        self._cfg = cfg
        cache_path = str(Path(cfg.token_cache_path).expanduser())
        self._auth = SpotifyOAuth(
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            redirect_uri=cfg.redirect_uri,
            scope=" ".join(cfg.scopes),
            cache_path=cache_path,
            open_browser=False,
        )
        self._sp: spotipy.Spotify | None = None

    def is_authorized(self) -> bool:
        """Return True if we have a valid (or refreshable) cached token."""
        token_info = self._auth.get_cached_token()
        return token_info is not None

    def get_auth_url(self) -> str:
        return self._auth.get_authorize_url()

    def exchange_code(self, code: str) -> None:
        """Exchange an OAuth authorization code for tokens and cache them."""
        self._auth.get_access_token(code, as_dict=False, check_cache=False)
        self._sp = None  # force re-init with new token

    def _client(self) -> spotipy.Spotify:
        """Return an authenticated Spotify client, refreshing token if needed."""
        token_info = self._auth.get_cached_token()
        if not token_info:
            raise RuntimeError("Not authorized — visit /auth/login to connect Spotify")
        if self._auth.is_token_expired(token_info):
            token_info = self._auth.refresh_access_token(token_info["refresh_token"])
        return spotipy.Spotify(auth=token_info["access_token"])

    # --- API methods used by proxy endpoints ---

    def top_artists(self, time_range: str = "medium_term", limit: int = 20) -> dict:
        sp = self._client()
        return sp.current_user_top_artists(time_range=time_range, limit=limit)

    def top_tracks(self, time_range: str = "medium_term", limit: int = 20, offset: int = 0) -> dict:
        sp = self._client()
        return sp.current_user_top_tracks(time_range=time_range, limit=limit, offset=offset)

    def recently_played(self, limit: int = 20) -> dict:
        sp = self._client()
        return sp.current_user_recently_played(limit=limit)

    def search(self, q: str, types: str = "track,artist", limit: int = 10, market: str = "US") -> dict:
        sp = self._client()
        return sp.search(q=q, type=types, limit=limit, market=market)

    def playlists(self, limit: int = 20) -> dict:
        sp = self._client()
        return sp.current_user_playlists(limit=limit)

    def playlist_tracks(self, playlist_id: str, limit: int = 50) -> dict:
        sp = self._client()
        return sp.playlist_tracks(playlist_id, limit=limit)

    def artist(self, artist_id: str) -> dict:
        sp = self._client()
        return sp.artist(artist_id)

    def artist_top_tracks(self, artist_id: str, country: str = "US") -> dict:
        sp = self._client()
        return sp.artist_top_tracks(artist_id, country=country)

    def artist_related(self, artist_id: str) -> dict:
        sp = self._client()
        return sp.artist_related_artists(artist_id)

    def artists_batch(self, artist_ids: list[str]) -> list[dict]:
        """Fetch multiple artists in batches of 50 (Spotify API max)."""
        sp = self._client()
        results = []
        for i in range(0, len(artist_ids), 50):
            batch = artist_ids[i:i + 50]
            data = sp.artists(batch)
            results.extend(data.get("artists") or [])
        return results

    def top_tracks_filtered(
        self,
        genres: list[str],
        time_range: str = "long_term",
        pages: int = 4,
    ) -> dict:
        """Fetch up to pages*50 top tracks, filter by artist genre keywords.

        Returns {tracks: [...], total_fetched, total_matched, genres_used}.
        Genre matching is case-insensitive substring match against Spotify
        artist genre tags (e.g. 'texas country', 'americana', 'folk').
        """
        sp = self._client()
        genres_lower = [g.lower() for g in genres]

        # Fetch all pages
        all_tracks: list[dict] = []
        seen_uris: set[str] = set()
        for page in range(pages):
            data = sp.current_user_top_tracks(time_range=time_range, limit=50, offset=page * 50)
            for t in data.get("items") or []:
                uri = t.get("uri", "")
                if uri and uri not in seen_uris:
                    seen_uris.add(uri)
                    all_tracks.append(t)
            if not data.get("next"):
                break  # no more pages

        # Collect unique artist IDs across all tracks
        artist_id_map: dict[str, str] = {}  # id → name
        for t in all_tracks:
            for a in t.get("artists") or []:
                if a.get("id"):
                    artist_id_map[a["id"]] = a.get("name", "")

        # Batch-fetch artist metadata for genre tags
        artist_genres: dict[str, list[str]] = {}
        for artist in self.artists_batch(list(artist_id_map)):
            if artist and artist.get("id"):
                artist_genres[artist["id"]] = [g.lower() for g in (artist.get("genres") or [])]

        # Filter tracks — keep if any artist has a matching genre tag
        matched: list[dict] = []
        for t in all_tracks:
            track_genres: set[str] = set()
            for a in t.get("artists") or []:
                track_genres.update(artist_genres.get(a.get("id", ""), []))
            if any(kw in g for kw in genres_lower for g in track_genres):
                matched.append({
                    "uri": t["uri"],
                    "name": t["name"],
                    "artists": [a["name"] for a in t.get("artists", [])],
                    "genres": sorted(track_genres),
                })

        return {
            "tracks": matched,
            "total_fetched": len(all_tracks),
            "total_matched": len(matched),
            "genres_used": genres,
        }

    def saved_tracks(self, limit: int = 50) -> dict:
        sp = self._client()
        return sp.current_user_saved_tracks(limit=limit)

    def get_granted_scopes(self) -> list[str]:
        """Return the scopes granted in the current cached token."""
        token_info = self._auth.get_cached_token()
        if not token_info:
            return []
        scope_str = token_info.get("scope", "")
        return scope_str.split() if scope_str else []

    def create_playlist(self, name: str, description: str = "", public: bool = False) -> dict:
        sp = self._client()
        data = {"name": name, "public": public, "description": description}
        return sp._post("me/playlists", payload=data)

    def add_tracks(self, playlist_id: str, track_uris: list[str]) -> dict:
        sp = self._client()
        return sp.playlist_add_items(playlist_id, track_uris)

    def replace_tracks(self, playlist_id: str, track_uris: list[str]) -> dict:
        """Replace all tracks in an existing playlist."""
        sp = self._client()
        return sp.playlist_replace_items(playlist_id, track_uris)

    def find_playlist_by_name(self, name: str, marker: str | None = None) -> dict | None:
        """Return the first owned playlist matching name exactly, or None.

        If marker is given, only match playlists whose description contains it —
        prevents accidentally overwriting manually-created playlists.
        """
        sp = self._client()
        user_id = sp.current_user()["id"]
        offset = 0
        while True:
            page = sp.current_user_playlists(limit=50, offset=offset)
            for pl in page.get("items", []):
                if pl.get("name") != name:
                    continue
                if pl.get("owner", {}).get("id") != user_id:
                    continue
                if marker and marker not in (pl.get("description") or ""):
                    continue
                return pl
            if page.get("next"):
                offset += 50
            else:
                return None
