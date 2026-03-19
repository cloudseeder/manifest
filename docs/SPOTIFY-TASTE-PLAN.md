# Spotify Taste Tool — Implementation Plan

Semantic music discovery grounded in your actual listening behavior. Not collaborative filtering — a taste model that reasons about *why* you like what you like and explains recommendations in plain English.

## Design Principle

Spotify's recommendation engine knows what correlates with your plays. This tool knows *why* you like what you play — expressed as audio feature patterns (acousticness, valence, energy, tempo) combined with LLM-generated natural language taste dimensions. Claude explains recommendations rather than just serving them.

## Architecture

New lightweight service (`spotify/oap_spotify/`) at `:8306`. Same pattern as email/reminder: FastAPI + SQLite + manifest.

```
Spotify API (recently played, audio features, recommendations, search)
  ↓ (sync)
oap_spotify.db (plays, track_features, taste_dimensions, recommendations_log)
  ↓ (taste update — after N new plays)
Discovery /v1/chat → LLM generates taste dimension descriptions
  ↓ (recommend)
Spotify /recommendations (seeded by taste profile)
  ↓ (explain)
Discovery /v1/chat → LLM explains why each track fits
  ↓ (ask)
User gets recommendations with reasoning
```

## Data Model: `oap_spotify.db`

### `plays`
Raw observation log — source of truth for taste modeling.

```sql
CREATE TABLE plays (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id         TEXT NOT NULL,
    track_name       TEXT NOT NULL,
    artist_name      TEXT NOT NULL,
    album_name       TEXT NOT NULL,
    played_at        TEXT NOT NULL,       -- ISO UTC from Spotify
    context_type     TEXT,                -- "playlist" | "album" | "artist" | null
    context_uri      TEXT,
    duration_ms      INTEGER,
    progress_ms      INTEGER,             -- how far in when ended (if available)
    play_type        TEXT NOT NULL DEFAULT 'play',  -- "play" | "skip" | "replay"
    created_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX plays_track_played ON plays(track_id, played_at);
```

### `track_features`
Spotify audio features, cached locally to avoid repeated API calls.

```sql
CREATE TABLE track_features (
    track_id         TEXT PRIMARY KEY,
    track_name       TEXT NOT NULL,
    artist_name      TEXT NOT NULL,
    tempo            REAL,
    valence          REAL,           -- 0=sad/tense, 1=happy/euphoric
    energy           REAL,           -- 0=calm, 1=intense
    acousticness     REAL,           -- 0=electric, 1=acoustic
    instrumentalness REAL,           -- 0=vocal, 1=no vocals
    danceability     REAL,
    loudness         REAL,
    speechiness      REAL,
    key              INTEGER,
    mode             INTEGER,        -- 0=minor, 1=major
    time_signature   INTEGER,
    fetched_at       TEXT NOT NULL
);
```

### `taste_dimensions`
LLM-generated semantic taste profile. Updated after each observation batch.

```sql
CREATE TABLE taste_dimensions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    dimension      TEXT NOT NULL UNIQUE,  -- "tempo_preference" | "mood_pattern" | etc.
    description    TEXT NOT NULL,         -- "gravitates toward minor-key acoustic tracks with low energy"
    value_json     TEXT NOT NULL,         -- JSON: {"acousticness_min": 0.6, "mode": 0}
    confidence     REAL NOT NULL DEFAULT 0.5,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);
```

### `recommendations_log`
Track what was recommended and what happened.

```sql
CREATE TABLE recommendations_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    recommended_at   TEXT NOT NULL,
    track_id         TEXT NOT NULL,
    track_name       TEXT NOT NULL,
    artist_name      TEXT NOT NULL,
    reason           TEXT NOT NULL,  -- LLM explanation
    feedback         TEXT,           -- null | "played" | "skipped" | "saved"
    feedback_at      TEXT
);
```

## Key Modules

### `spotify_client.py`
Async wrapper using Spotipy for OAuth token management. Spotipy is worth the dependency — OAuth PKCE from scratch is the hardest part of this service.

- `get_recently_played(limit=50)` → `/me/player/recently-played`
- `get_audio_features(track_ids)` → `/audio-features` (batch up to 100)
- `search(query, type, limit)` → `/search`
- `get_recommendations(seed_tracks, seed_genres, target_*)` → `/recommendations`

Scopes needed: `user-read-recently-played`, `user-library-read`, `user-top-read`

### `taste_engine.py`
The semantic layer — where the differentiation lives.

**`ingest_plays(plays, db)`**
- Store play events, detect play_type from sequence
- Fetch and cache audio features for new track IDs

**`classify_play_type(play, next_play)`**
Heuristic since Spotify doesn't expose skips directly:
- Same track again within 30s → `replay`
- progress_ms < 30% of duration_ms before next track → `skip`
- Otherwise → `play`

**`update_taste_model(db, discovery_url)`**
Called after N new plays (default: 20). Process:
1. Aggregate audio features by play_type: mean/stddev for plays vs skips
2. Identify top artists and any genre patterns
3. Build time-of-day patterns (morning vs late-night listening)
4. Send stats to `/v1/chat` on discovery:
   > "Here are this user's recent listening statistics: [JSON]. Generate 5 taste dimension descriptions explaining what this person actually likes and dislikes about music. Be specific about audio qualities, not just genre names."
5. Parse LLM response, store in `taste_dimensions`

**`explain_recommendation(track_features, taste_dims, discovery_url)`**
Given a candidate track's audio features and current taste profile:
> "Given this taste profile: [dimensions]. This track has acousticness=0.82, energy=0.31, valence=0.24, mode=minor. In 1-2 sentences, explain why this track fits or doesn't fit the taste profile."

Returns natural language explanation for display.

### `api.py`
FastAPI service, same structure as email/reminder.

- `POST /sync` — fetch recent plays, ingest, update taste if threshold reached
- `POST /api` — single-endpoint dispatcher
- `GET /auth/callback` — OAuth callback (Spotipy handles the flow)
- `GET /health`

## Dispatch Actions

| Action | Description |
|---|---|
| `ask` | Natural language — syncs, reasons about taste profile, returns recommendations with explanations |
| `sync` | Pull recent plays from Spotify, update taste model |
| `taste` | Return current taste profile summary |
| `recommend` | Find new tracks matching taste profile |
| `explain` | Explain why a specific track fits or doesn't fit the profile |
| `feedback` | Record feedback on a recommendation |

## Manifest: `oap-spotify.json`

Intent-first, same pattern as email/reminder:

```json
{
  "oap": "1.0",
  "name": "oap-spotify",
  "description": "Discover music that fits your taste and understand why you like what you like. Builds a semantic profile from your listening history — not just 'you liked X' but 'you gravitate toward minor-key acoustic tracks with low energy'. Ask: 'find me something new to listen to', 'what does my taste profile say about me?', 'recommend something for late-night coding', 'why do I keep replaying that Bon Iver song?', 'something calming for this afternoon'.",
  "input": {
    "format": "application/json",
    "parameters": {
      "action": {"type": "string", "description": "Always 'ask' for conversational use", "required": true},
      "question": {"type": "string", "description": "Your question about music or taste profile", "required": true}
    }
  },
  "invoke": {
    "method": "POST",
    "url": "http://localhost:8306/api"
  },
  "tags": ["music", "spotify", "recommendations", "taste", "playlist", "listening"]
}
```

## Config

```yaml
spotify:
  client_id: ""                          # or OAP_SPOTIFY_CLIENT_ID
  client_secret: ""                      # or OAP_SPOTIFY_CLIENT_SECRET
  redirect_uri: "http://localhost:8306/auth/callback"
  scopes:
    - user-read-recently-played
    - user-library-read
    - user-top-read

database:
  path: "oap_spotify.db"

api:
  host: "127.0.0.1"
  port: 8306

taste:
  update_after_plays: 20      # update taste model after N new plays
  update_max_age_days: 7      # also update if profile is stale
  sync_to_memory: true        # write taste dimensions to agent user_facts

discovery_url: "http://localhost:8300"
```

## Agent Memory Integration

After each taste update, if `sync_to_memory: true`, call the agent's memory API to store the top taste dimensions as user facts. This means conversational context ("play something mellow") can draw from both the taste profile AND general memory about the user.

Example fact stored: "gravitates toward minor-key acoustic tracks with low energy and high instrumentalness"

These facts are durable and update as taste evolves — they supersede older versions via the existing semantic dedup in `memory.py`.

## Phased Rollout

### Phase 1 — Foundation (~1 day)
- [ ] Service scaffold: `pyproject.toml`, `config.py`, `models.py`, `api.py`
- [ ] `spotify_client.py` with Spotipy — OAuth flow, recently played, audio features
- [ ] `plays` and `track_features` tables
- [ ] `sync` action: fetch recently played, cache audio features, detect play types
- [ ] `taste` action: return raw play statistics (no LLM reasoning yet)
- [ ] Manifest registered in discovery
- [ ] Add to `setup.sh`

### Phase 2 — Taste Model + Recommendations (~1 day)
- [ ] `taste_dimensions` table
- [ ] `taste_engine.py`: aggregate stats + LLM dimension generation
- [ ] `ask` and `recommend` actions with LLM explanation
- [ ] Integration test: "find me something to listen to tonight"
- [ ] `explain` action: "why do I keep replaying that Bon Iver song?"

### Phase 3 — Feedback + Memory (~half day)
- [ ] `recommendations_log` and `feedback` action
- [ ] Agent memory integration (`sync_to_memory`)
- [ ] Cron task in agent scheduler: "Sync Spotify" (daily)
- [ ] Tune skip detection heuristic from real data

## Open Questions

- **Skip detection accuracy**: Spotify recently-played API only shows tracks played >30s — true early skips are invisible. `progress_ms` helps when present. Taste model will be biased toward plays. Mitigation: weight replays as strong positive signal.
- **Spotipy vs raw httpx**: Spotipy handles OAuth token refresh automatically, worth the dependency. Raw httpx would require implementing PKCE from scratch.
- **LLM cost for taste updates**: Daily updates at ~1000 tokens = negligible. Route through local Ollama first, escalate to Claude only if configured.
- **Manifest competition**: Spotify manifest will compete with Ollama's general music knowledge for queries like "what's a good jazz album?". Manifest description needs strong personalization signal words: "your listening history", "your taste profile".
- **Privacy**: Play history and taste profile are local SQLite. The only external call is to `api.spotify.com`. Taste dimensions stored in agent memory are as private as all other user facts.
