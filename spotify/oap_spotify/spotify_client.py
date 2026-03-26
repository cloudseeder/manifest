"""Spotipy OAuth wrapper for OAP Spotify proxy."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
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

    def _classify_artists_by_genre(
        self,
        artist_names: list[str],
        genres: list[str],
        ollama_url: str = "http://localhost:11434",
    ) -> set[str]:
        """Ask qwen3 which artist names match the given genre keywords.

        Returns the set of matching artist names (lowercased for comparison).
        Falls back to returning all artists if the LLM call fails.
        """
        if not artist_names:
            return set()
        genre_str = ", ".join(genres)
        artist_list = "\n".join(f"- {n}" for n in artist_names)
        prompt = (
            f"You are a music genre classifier. Given this list of artist names, "
            f"return a JSON array containing ONLY the names of artists whose primary "
            f"music style fits any of these genres: {genre_str}.\n\n"
            f"Artists:\n{artist_list}\n\n"
            f"Return ONLY a valid JSON array of matching artist names, nothing else. "
            f"Example: [\"Artist One\", \"Artist Two\"]"
        )
        try:
            resp = httpx.post(
                f"{ollama_url}/api/generate",
                json={"model": "qwen3:8b", "prompt": prompt, "stream": False,
                      "options": {"temperature": 0, "num_predict": 1024}, "think": False},
                timeout=60.0,
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "").strip()
            # Extract JSON array from response (may have extra text)
            start, end = raw.find("["), raw.rfind("]")
            if start != -1 and end != -1:
                matched = json.loads(raw[start:end + 1])
                log.info("LLM classified %d/%d artists as %s", len(matched), len(artist_names), genres)
                return {n.lower() for n in matched if isinstance(n, str)}
        except Exception as exc:
            log.warning("Artist genre classification failed: %s — returning all artists", exc)
        return {n.lower() for n in artist_names}

    def top_tracks_filtered(
        self,
        genres: list[str],
        time_range: str = "long_term",
        pages: int = 4,
        ollama_url: str = "http://localhost:11434",
    ) -> dict:
        """Fetch up to pages*50 top tracks, filter by genre using qwen3.

        Spotify's genre tags are restricted for development apps, so we
        extract unique artist names from top tracks and ask qwen3 to
        classify which ones match the requested genre keywords.

        Returns {tracks: [...], total_fetched, total_matched, genres_used}.
        """
        sp = self._client()

        # Fetch top tracks across all pages
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
                break

        # Collect unique artist names from top tracks
        unique_artists: dict[str, str] = {}  # name_lower -> display_name
        for t in all_tracks:
            for a in t.get("artists") or []:
                name = a.get("name", "")
                if name:
                    unique_artists[name.lower()] = name

        # Ask qwen3 which artists match the genres
        matched_artists = self._classify_artists_by_genre(
            list(unique_artists.values()), genres, ollama_url=ollama_url
        )

        # Filter tracks — keep if any artist matched
        matched: list[dict] = []
        for t in all_tracks:
            track_artist_names = {(a.get("name") or "").lower() for a in t.get("artists") or []}
            if track_artist_names & matched_artists:
                matched.append({
                    "uri": t["uri"],
                    "name": t["name"],
                    "artists": [a["name"] for a in t.get("artists", [])],
                })

        return {
            "tracks": matched,
            "total_fetched": len(all_tracks),
            "total_matched": len(matched),
            "genres_used": genres,
        }

    def daily_mix(
        self,
        limit: int = 50,
        recent_limit: int = 50,
        top_limit: int = 50,
    ) -> dict:
        """Combine recently played + short-term top tracks, deduplicated.

        Returns up to `limit` unique tracks ordered by recency/rank,
        ready to pass to upsert.
        """
        sp = self._client()

        # Recently played (most recent first)
        recent_data = sp.current_user_recently_played(limit=recent_limit)
        seen_uris: set[str] = set()
        tracks: list[dict] = []
        for item in recent_data.get("items") or []:
            t = item.get("track") or {}
            uri = t.get("uri", "")
            if uri and uri not in seen_uris:
                seen_uris.add(uri)
                tracks.append({
                    "uri": uri,
                    "name": t.get("name", ""),
                    "artists": [a["name"] for a in t.get("artists", [])],
                })

        # Short-term top tracks (fill remaining slots)
        top_data = sp.current_user_top_tracks(time_range="short_term", limit=top_limit)
        for t in top_data.get("items") or []:
            uri = t.get("uri", "")
            if uri and uri not in seen_uris:
                seen_uris.add(uri)
                tracks.append({
                    "uri": uri,
                    "name": t.get("name", ""),
                    "artists": [a["name"] for a in t.get("artists", [])],
                })

        return {
            "tracks": tracks[:limit],
            "total_combined": len(tracks),
            "track_count": min(len(tracks), limit),
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
        result = {}
        for i in range(0, len(track_uris), 100):
            result = sp.playlist_add_items(playlist_id, track_uris[i:i + 100])
        return result

    def replace_tracks(self, playlist_id: str, track_uris: list[str]) -> dict:
        """Replace all tracks in an existing playlist."""
        sp = self._client()
        # Replace clears the playlist and adds the first batch; subsequent batches append
        result = sp.playlist_replace_items(playlist_id, track_uris[:100])
        for i in range(100, len(track_uris), 100):
            result = sp.playlist_add_items(playlist_id, track_uris[i:i + 100])
        return result

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
