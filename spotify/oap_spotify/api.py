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
    return {"status": "ok", "authorized": c.is_authorized()}


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
        return c.top_artists(time_range=time_range, limit=limit)
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
        return c.top_tracks(time_range=time_range, limit=limit)
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
        return c.recently_played(limit=limit)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.error("recently_played error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/proxy/search")
async def search(
    q: str = Query(..., description="Search query"),
    type: str = Query("track,artist", description="Comma-separated types: track, artist, album, playlist"),
    limit: int = Query(20, ge=1, le=50),
):
    """Search Spotify catalog for tracks, artists, albums, or playlists."""
    c = _require_client()
    try:
        return c.search(q=q, types=type, limit=limit)
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
