"""FastAPI application for OAP Spotify proxy."""

from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse

from .config import load_config
from .spotify_client import SpotifyClient


def _slim_track(track: dict) -> dict:
    """Strip a full Spotify track object to just what the LLM needs."""
    return {
        "uri": track.get("uri", ""),
        "name": track.get("name", ""),
        "artists": [a.get("name", "") for a in track.get("artists", [])],
        "id": track.get("id", ""),
    }


def _slim_artist(artist: dict) -> dict:
    """Strip a full Spotify artist object to just what the LLM needs."""
    return {
        "id": artist.get("id", ""),
        "name": artist.get("name", ""),
        "genres": artist.get("genres", []),
        "popularity": artist.get("popularity", 0),
    }

log = logging.getLogger("oap.spotify")

_client: SpotifyClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    cfg = load_config()
    if not cfg.spotify.client_id or not cfg.spotify.client_secret:
        log.warning(
            "Spotify client_id / client_secret not configured. "
            "Set in config.yaml or SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET env vars."
        )
    _client = SpotifyClient(cfg.spotify)
    authorized = _client.is_authorized()
    log.info(
        "Spotify proxy started (port=%d, authorized=%s)", cfg.port, authorized
    )
    if not authorized:
        log.warning("Not authorized — visit http://localhost:%d/auth/login", cfg.port)
    yield


app = FastAPI(title="OAP Spotify Proxy", lifespan=lifespan)


def _require_client() -> SpotifyClient:
    if _client is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    return _client


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.get("/auth/login")
async def auth_login():
    """Redirect to Spotify's OAuth authorization page."""
    c = _require_client()
    url = c.get_auth_url()
    return RedirectResponse(url=url)


@app.get("/auth/callback")
async def auth_callback(code: str = Query(...), state: str | None = Query(None)):
    """OAuth callback — exchange code for tokens."""
    c = _require_client()
    try:
        c.exchange_code(code)
    except Exception as e:
        log.error("OAuth exchange failed: %s", e)
        raise HTTPException(status_code=400, detail=f"OAuth exchange failed: {e}")
    log.info("Spotify authorization successful")
    return {"status": "authorized", "message": "Spotify connected. You can close this tab."}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    c = _require_client()
    authorized = c.is_authorized()
    scopes = c.get_granted_scopes() if authorized else []
    return {"status": "ok", "authorized": authorized, "scopes": scopes}


# ---------------------------------------------------------------------------
# Proxy endpoints (all under /proxy/ for manifest targeting)
# ---------------------------------------------------------------------------

@app.get("/proxy/me/top/artists")
async def top_artists(
    time_range: str = Query("medium_term", pattern="^(short_term|medium_term|long_term)$"),
    limit: int = Query(20, ge=1, le=50),
):
    """Return user's top artists.

    time_range: short_term (4 weeks), medium_term (6 months), long_term (years).
    """
    c = _require_client()
    try:
        data = c.top_artists(time_range=time_range, limit=limit)
        data["items"] = [_slim_artist(a) for a in data.get("items", [])]
        return data
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("top_artists error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/proxy/me/top/tracks")
async def top_tracks(
    time_range: str = Query("medium_term", pattern="^(short_term|medium_term|long_term)$"),
    limit: int = Query(20, ge=1, le=50),
):
    """Return user's top tracks.

    time_range: short_term (4 weeks), medium_term (6 months), long_term (years).
    """
    c = _require_client()
    try:
        data = c.top_tracks(time_range=time_range, limit=limit)
        data["items"] = [_slim_track(t) for t in data.get("items", [])]
        return data
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("top_tracks error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/proxy/me/player/recently-played")
async def recently_played(
    limit: int = Query(20, ge=1, le=50),
):
    """Return recently played tracks."""
    c = _require_client()
    try:
        data = c.recently_played(limit=limit)
        data["items"] = [
            {"played_at": item.get("played_at", ""), "track": _slim_track(item["track"])}
            for item in data.get("items", [])
            if item.get("track")
        ]
        return data
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("recently_played error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/proxy/search")
async def search(
    q: str = Query(..., description="Search query"),
    type: str = Query("track,artist", description="Comma-separated types: track, artist, album, playlist"),
    limit: int = Query(5, ge=0, le=10),
    market: str = Query("US"),
):
    """Search Spotify catalog for tracks, artists, albums, or playlists."""
    c = _require_client()
    try:
        return c.search(q=q, types=type, limit=limit, market=market)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("search error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/proxy/me/playlists")
async def playlists(
    limit: int = Query(20, ge=1, le=50),
):
    """Return user's playlists (owned and followed)."""
    c = _require_client()
    try:
        return c.playlists(limit=limit)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("playlists error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/proxy/playlists/{playlist_id}/tracks")
async def playlist_tracks(
    playlist_id: str,
    limit: int = Query(50, ge=1, le=100),
):
    """Return tracks from a specific playlist."""
    c = _require_client()
    try:
        return c.playlist_tracks(playlist_id, limit=limit)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("playlist_tracks error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


# Query-param variants for tool-bridge compatibility (path params can't be
# injected by the tool executor, so LLM tool calls use ?artist_id=... instead).
# These MUST be defined before the /{artist_id} wildcard route.

@app.get("/proxy/artists/related-artists")
async def artist_related_by_query(
    artist_id: str = Query(..., description="Spotify artist ID"),
):
    """Return artists similar to a given artist (query-param variant)."""
    c = _require_client()
    try:
        return c.artist_related(artist_id)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("artist_related error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/proxy/artists/top-tracks")
async def artist_top_tracks_by_query(
    artist_id: str = Query(..., description="Spotify artist ID"),
    country: str = Query("US"),
):
    """Return an artist's top tracks (query-param variant)."""
    c = _require_client()
    try:
        return c.artist_top_tracks(artist_id, country=country)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("artist_top_tracks error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/proxy/playlist/tracks")
async def playlist_tracks_by_query(
    playlist_id: str = Query(..., description="Spotify playlist ID"),
    limit: int = Query(50, ge=1, le=100),
):
    """Return tracks from a playlist (query-param variant)."""
    c = _require_client()
    try:
        return c.playlist_tracks(playlist_id, limit=limit)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("playlist_tracks error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# Playlist write endpoints
# ---------------------------------------------------------------------------

@app.post("/proxy/me/playlists")
async def create_playlist(body: dict):
    """Create a new private Spotify playlist.

    Body: name (required), description (optional), public (optional, default false).
    Returns the new playlist object including its id.
    """
    c = _require_client()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="'name' is required")
    description = body.get("description", "")
    public = bool(body.get("public", False))
    try:
        result = c.create_playlist(name, description=description, public=public)
        log.info("Created playlist '%s' (%s)", name, result.get("id", "?"))
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("create_playlist error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/proxy/playlists/add-tracks")
async def add_tracks(body: dict):
    """Add tracks to an existing Spotify playlist.

    Body: playlist_id (required), track_uris (required, list of spotify:track:ID strings).
    Returns Spotify snapshot_id on success.
    """
    c = _require_client()
    playlist_id = (body.get("playlist_id") or "").strip()
    track_uris = body.get("track_uris") or []
    if not playlist_id:
        raise HTTPException(status_code=400, detail="'playlist_id' is required")
    if not track_uris:
        raise HTTPException(status_code=400, detail="'track_uris' is required")
    if isinstance(track_uris, str):
        track_uris = [u.strip() for u in track_uris.split(",") if u.strip()]
    try:
        result = c.add_tracks(playlist_id, track_uris)
        log.info("Added %d track(s) to playlist %s", len(track_uris), playlist_id)
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("add_tracks error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


_OAP_MARKER = "managed-by-oap"


@app.post("/proxy/playlists/upsert")
async def upsert_playlist(body: dict):
    """Create-or-replace an OAP-managed Spotify playlist by name.

    Only updates playlists that were previously created by this endpoint
    (identified by a hidden marker in the description). Never touches
    manually-created or third-party playlists with the same name.

    Body: name (required), track_uris (required), description (optional), public (optional).
    Returns: {playlist_id, playlist_name, created, track_count}
    """
    c = _require_client()
    name = (body.get("name") or "").strip()
    track_uris = body.get("track_uris") or []
    if not name:
        raise HTTPException(status_code=400, detail="'name' is required")
    if not track_uris:
        raise HTTPException(status_code=400, detail="'track_uris' is required")
    if isinstance(track_uris, str):
        track_uris = [u.strip() for u in track_uris.split(",") if u.strip()]
    user_description = body.get("description", "")
    # Embed marker so we can safely identify OAP-managed playlists on future runs
    full_description = f"{user_description} [{_OAP_MARKER}]".strip()
    public = bool(body.get("public", False))
    try:
        existing = c.find_playlist_by_name(name, marker=_OAP_MARKER)
        if existing:
            playlist_id = existing["id"]
            c.replace_tracks(playlist_id, track_uris)
            log.info("Upsert: replaced %d tracks in OAP playlist '%s' (%s)", len(track_uris), name, playlist_id)
            created = False
        else:
            pl = c.create_playlist(name, description=full_description, public=public)
            playlist_id = pl["id"]
            c.add_tracks(playlist_id, track_uris)
            log.info("Upsert: created OAP playlist '%s' (%s) with %d tracks", name, playlist_id, len(track_uris))
            created = True
        return {"playlist_id": playlist_id, "playlist_name": name, "created": created, "track_count": len(track_uris)}
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("upsert_playlist error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


# Path-param variants (for direct curl access — wildcard must come after static routes above)

@app.get("/proxy/artists/{artist_id}")
async def artist(artist_id: str):
    """Return artist metadata — genres, popularity, follower count."""
    c = _require_client()
    try:
        return c.artist(artist_id)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("artist error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/proxy/artists/{artist_id}/top-tracks")
async def artist_top_tracks(
    artist_id: str,
    country: str = Query("US"),
):
    """Return an artist's top tracks in a given market."""
    c = _require_client()
    try:
        return c.artist_top_tracks(artist_id, country=country)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("artist_top_tracks error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/proxy/artists/{artist_id}/related-artists")
async def artist_related(artist_id: str):
    """Return artists similar to a given artist."""
    c = _require_client()
    try:
        return c.artist_related(artist_id)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("artist_related error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/proxy/me/tracks")
async def saved_tracks(
    limit: int = Query(50, ge=1, le=50),
):
    """Return user's saved (liked) tracks."""
    c = _require_client()
    try:
        return c.saved_tracks(limit=limit)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("saved_tracks error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="OAP Spotify Proxy API")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config(args.config)

    host = args.host or cfg.host
    port = args.port or cfg.port
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
