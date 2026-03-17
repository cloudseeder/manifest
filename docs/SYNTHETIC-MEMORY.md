# Synthetic Human Memory

A two-layer memory system that mirrors how human memory works — episodic experiences with emotional weight and temporal decay alongside durable semantic facts. This is what makes Manifest feel like a companion rather than a search engine.

## Why Simple Fact Storage Isn't Enough

The first version of Manifest's memory was a flat table of facts: "lives in Portland", "wife Amy works at KGW", "prefers dark roast coffee." Facts were extracted from conversations, embedded with nomic-embed-text, and retrieved via cosine similarity. It worked at 50 facts. It breaks down at 500.

The problem isn't retrieval accuracy — it's retrieval *relevance*. When a user says "I'm thinking about my dad today," a fact-only system retrieves `father Gordon was a Marine` and `father Gordon passed away in 2019`. Technically correct. But a human companion would surface the *memory* — the story about fishing at Cherry Creek, the way his voice sounded on the phone, the last Thanksgiving together. Facts are the skeleton. Memories are the muscle.

Human memory has properties that RAG-over-facts can't replicate:

- **Episodic vs semantic separation.** You remember *that you went to Paris* (episodic) and *that Paris is in France* (semantic) through different systems. The episodic memory carries sensory detail, emotional tone, and temporal context. The semantic fact is stripped of all that.
- **Emotional weighting.** You remember your wedding day more vividly than last Tuesday's lunch. Emotional intensity strengthens encoding and retrieval. A flat reference_count can't model this.
- **Temporal decay.** Memories fade unless reinforced. You don't remember every meal, but you remember the one where your daughter said her first word. Retrieval frequency fights decay; absence accelerates it.
- **Associative linking.** Remembering the smell of pine triggers the memory of your grandfather's workshop, which triggers the memory of building a birdhouse together. Memories form networks, not lists.

Manifest's synthetic memory system addresses the first three. Associative linking is Phase 2.

## Architecture

Two layers, both in SQLite (`oap_agent.db`), both embedded with nomic-embed-text for vector retrieval:

```
┌─────────────────────────────────────────────────────────┐
│  Semantic Layer (user_facts)                            │
│  Short durable facts: 3-15 words                        │
│  "lives in Portland", "son Kai born in 1996"            │
│  Dedup via UNIQUE constraint + semantic similarity       │
│  Supersession: new facts retire old contradicted ones    │
│  Eviction: LRU on unpinned facts beyond max_facts       │
├─────────────────────────────────────────────────────────┤
│  Episodic Layer (episodes)                              │
│  Rich narratives: 1-3 sentence descriptions             │
│  Emotional valence (-1.0 to 1.0) + intensity (0 to 1.0)│
│  People, location, tags, linked images                  │
│  Weighted retrieval: similarity + strength + emotion +  │
│    recency                                              │
│  No eviction — memories are permanent (decay via score) │
└─────────────────────────────────────────────────────────┘
```

Both layers are injected into the system prompt on every chat message, but formatted differently: facts as bullet points under category headers, episodes as narrative blocks with emotional tone markers.

### Semantic Layer: `user_facts`

```sql
CREATE TABLE IF NOT EXISTS user_facts (
    id              TEXT PRIMARY KEY,
    fact            TEXT NOT NULL UNIQUE,
    source_message  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    last_referenced TEXT NOT NULL,
    reference_count INTEGER NOT NULL DEFAULT 1,
    pinned          INTEGER NOT NULL DEFAULT 0,
    embedding       BLOB,
    superseded_by   TEXT,
    superseded_at   TEXT,
    image_path      TEXT
);
```

Facts are the "what I know" layer. They are short, durable, and deduplicated. The UNIQUE constraint on `fact` prevents exact duplicates. Semantic dedup (cosine similarity > 0.85 between candidate and existing fact embeddings) prevents near-duplicates like "lives in Portland" vs "resides in Portland, Oregon."

**Supersession.** When a new fact contradicts an old one (e.g., "works at Nike" replacing "works at Intel"), the LLM detects the replacement via a second extraction pass. The old fact is marked `superseded_by = <new_fact_id>` rather than deleted — preserving history. Supersession uses a similarity band (0.50-0.84): high enough to be topically related, low enough to not be a duplicate. Subject matching prevents cross-entity supersession (a fact about Amy can't supersede a fact about Kai).

**Pinning.** Users can pin facts via the Settings UI. Pinned facts are always included in retrieval results (similarity = 1.0), immune to eviction, and immune to supersession.

**Eviction.** When unpinned active facts exceed `max_facts` (default 500, configurable to 2000), the least-referenced and least-recently-referenced facts are deleted. This is LRU with reference_count as a tiebreaker.

### Episodic Layer: `episodes`

```sql
CREATE TABLE IF NOT EXISTS episodes (
    id                     TEXT PRIMARY KEY,
    description            TEXT NOT NULL,
    timestamp              TEXT NOT NULL,
    location               TEXT,
    people                 TEXT NOT NULL DEFAULT '[]',
    emotional_valence      REAL NOT NULL DEFAULT 0.0,
    emotional_intensity    REAL NOT NULL DEFAULT 0.0,
    source_conversation_id TEXT,
    source_message_id      TEXT,
    image_path             TEXT,
    embedding              BLOB,
    tags                   TEXT NOT NULL DEFAULT '[]',
    created_at             TEXT NOT NULL,
    last_referenced        TEXT NOT NULL,
    reference_count        INTEGER NOT NULL DEFAULT 1
);
```

Episodes are the "what we've been through together" layer. They are rich, narrative, and permanent. An episode is not evicted — it decays via the scoring formula, becoming harder to retrieve over time unless reinforced.

Key fields:

- **description** — 1-3 sentence narrative. Not a fact but a memory: "Kevin shared a photo of a craftsman house with a green door and said it's his dream home. He's been browsing listings in the Pearl District."
- **emotional_valence** — -1.0 (negative/painful) to 1.0 (positive/joyful). A memory of losing a parent is low valence; a memory of a child's graduation is high.
- **emotional_intensity** — 0.0 (mundane) to 1.0 (life-changing). Governs how strongly the memory surfaces. A casual mention of liking pizza is 0.1; sharing news of a cancer diagnosis is 0.9.
- **people** — JSON array of names mentioned. Enables retrieval by person: "tell me about conversations involving Amy."
- **location** — Where the experience happened or was discussed.
- **tags** — JSON array of topic keywords for additional retrieval paths.
- **image_path** — Link to a saved image when the episode was triggered by visual input.
- **reference_count / last_referenced** — Reinforcement tracking. Every time an episode is retrieved and injected into context, its count increments and timestamp updates. This is the "rehearsal" mechanism that fights temporal decay.

## Episode Extraction

Every chat message goes through two parallel fire-and-forget extraction pipelines: one for facts, one for episodes. Both call `POST /api/generate` on discovery's Ollama pass-through (same local LLM, no external API).

### The Gate: What Is an Episode?

The extraction system prompt defines a clear boundary:

```
NOT an episode: routine tasks (check email, set reminder), greetings,
simple questions, tool results, or mundane exchanges.

IS an episode: sharing a photo of a dream house, talking about visiting
parents, remembering a family story, expressing strong feelings, life
milestones, discovering a new interest, or meaningful personal revelations.
```

Most conversation turns produce no episode. The LLM returns `{"episode": null}` and the pipeline exits. Only meaningful shared moments — stories, emotions, milestones, personal revelations — produce episodes.

### Extraction Trigger

Extraction is gated on the user *sharing* rather than *asking*. The routing logic in `api.py` checks:

```python
_user_is_sharing = (
    "?" not in req.message
    and not _used_tools
    and not re.match(r"^(tell|show|what|list|describe|read|check|get|find)\b",
                     req.message.strip(), re.IGNORECASE)
)
```

This prevents the feedback loop where the LLM's response (which parrots stored facts) gets re-extracted as new facts or episodes.

### JSON Output Format

The LLM returns structured JSON:

```json
{
  "episode": {
    "description": "Kevin shared a photo of a 1920s craftsman house in the Pearl District with a deep green front door, saying this is exactly the style he wants when they move.",
    "people": ["Amy"],
    "emotional_valence": 0.8,
    "emotional_intensity": 0.5,
    "tags": ["house", "real-estate", "pearl-district", "dream-home"],
    "location": "Portland, Pearl District"
  }
}
```

Valence and intensity are clamped to valid ranges on storage (`max(-1.0, min(1.0, valence))` and `max(0.0, min(1.0, intensity))`). The description is capped at 500 characters.

After storage, the episode's description is immediately embedded via nomic-embed-text and the embedding BLOB is written back to the row. Batch embedding on startup (`embed_missing_episodes`) catches any that failed during initial extraction.

## Weighted Retrieval

This is the core of the system. When a user sends a chat message and RAG is active, episodes are retrieved by cosine similarity against the message embedding, then **re-ranked by a weighted scoring formula**:

```python
def _ep_score(ep):
    sim = ep.get("similarity", 0)
    strength = min(ep.get("reference_count", 1) / 10.0, 1.0)
    intensity = ep.get("emotional_intensity", 0)
    try:
        last_ref = datetime.fromisoformat(ep.get("last_referenced", ep.get("created_at", "")))
        days_ago = (now - last_ref).total_seconds() / 86400
    except (ValueError, TypeError):
        days_ago = 30
    recency = max(0, 1.0 - days_ago / 90.0)
    return sim * 0.4 + strength * 0.3 + intensity * 0.2 + recency * 0.1
```

### The Formula

```
score = similarity * 0.4 + strength * 0.3 + intensity * 0.2 + recency * 0.1
```

| Component | Weight | Range | What It Models |
|-----------|--------|-------|----------------|
| **similarity** | 0.4 | 0.0-1.0 | Cosine similarity to user's message. The primary signal — is this memory topically relevant? |
| **strength** | 0.3 | 0.0-1.0 | `min(reference_count / 10, 1.0)`. Memories referenced more often are stronger. Caps at 10 references. Models rehearsal/consolidation. |
| **intensity** | 0.2 | 0.0-1.0 | Emotional intensity from extraction. Life-changing events surface more readily than casual mentions. Models emotional encoding. |
| **recency** | 0.1 | 0.0-1.0 | Linear decay over 90 days: `max(0, 1 - days_ago / 90)`. Recent memories have a slight edge. Falls to 0 after 3 months without reinforcement. |

### What This Produces

The weights are deliberately chosen so that **emotionally significant memories beat topically similar but mundane ones.** Consider two episodes when the user says "thinking about family":

- Episode A: "Kevin mentioned his parents are visiting next weekend." similarity=0.6, strength=0.1 (referenced once), intensity=0.2 (routine), recency=0.8 (recent)
  - Score: 0.6\*0.4 + 0.1\*0.3 + 0.2\*0.2 + 0.8\*0.1 = 0.24 + 0.03 + 0.04 + 0.08 = **0.39**

- Episode B: "Kevin shared the story of his father's last Christmas, describing how they sat together on the porch and his dad told him he was proud of him." similarity=0.55, strength=0.5 (referenced 5 times), intensity=0.85 (deeply emotional), recency=0.2 (months ago)
  - Score: 0.55\*0.4 + 0.5\*0.3 + 0.85\*0.2 + 0.2\*0.1 = 0.22 + 0.15 + 0.17 + 0.02 = **0.56**

Episode B wins despite lower similarity and recency. The emotional weight and reinforcement history push it above the mundane recent event. This is how human memory works — you don't remember every visit, but you remember the meaningful ones.

### Temporal Decay via Reference Counting

Recency is a lightweight approximation of the Ebbinghaus forgetting curve. A memory starts with recency=1.0 and decays linearly to 0 over 90 days. But every time the memory is retrieved and injected into context, `last_referenced` is updated and `reference_count` increments — resetting the recency clock and strengthening the memory.

This creates a natural selection pressure: memories that keep getting retrieved stay strong. Memories that stop being relevant gradually fade from retrieval results (though they're never deleted). The 90-day window was chosen because the recency component is only 10% of the total score — it's a tiebreaker, not a primary signal. A highly emotional memory (intensity=0.9) still scores well even at recency=0.

## System Prompt Injection

Facts and episodes are injected into the system prompt as distinct blocks with different formatting to help the LLM distinguish between "what I know" and "what we've shared."

### Fact Injection

Facts are formatted as categorized bullet points:

```
About the user:
- lives in Portland
- works as a network engineer
- prefers dark roast coffee

Family and pets (NOT the user):
- wife Amy works as video editor at KGW
- son Kai born in 1996
- dog Bear is a golden retriever [image: /v1/agent/images/conv_abc123_msg_1.jpg]
```

The category separation (user vs family/pets) prevents the LLM from attributing family facts to the user — a regex splits facts by relationship-prefix patterns. Image-linked facts include a markdown-compatible URL.

### Episode Injection

Episodes are formatted as narrative blocks with emotional tone markers:

```
Shared experiences and memories:
  Kevin shared a photo of a 1920s craftsman house with a deep green door,
  saying this is exactly the style he wants. (involving Amy) [positive memory]

  Kevin talked about his father Gordon's time in the Marines and how it
  shaped his own work ethic. (involving Gordon) [positive memory]

  Kevin shared that Bear was diagnosed with hip dysplasia and they're
  considering surgery options. (involving Bear) [difficult memory]
  [image: /v1/agent/images/conv_def456_msg_3.jpg]
```

Key formatting details:
- People are listed parenthetically: `(involving Amy, Gordon)`
- Emotional valence above 0.3 gets `[positive memory]`; below -0.3 gets `[difficult memory]`
- Image-linked episodes include the serving URL for markdown rendering
- Top 3 episodes by weighted score are injected (prompt space budget)

The preamble instructs the LLM to use these memories naturally:

> "You have a personal memory of the user. These facts were learned from past conversations. Use them to give personalized, contextual answers. When the user asks about their preferences, history, or people they know, answer from these facts — do NOT say you can't help or suggest external searches."

## Visual Memory

Images sent in chat messages are saved to disk at `{db_dir}/images/{conv_id}_{msg}_{n}.jpg` and served via `GET /v1/agent/images/{filename}`. Both facts and episodes extracted from image-bearing conversation turns carry the `image_path` reference.

The chain:

1. User pastes/drops an image with context: "This is our new front door color — remember this for when I go to Sherwin Williams"
2. Image saved to disk, base64 passed to vision model (qwen3.5:9b)
3. Vision model describes the image in its response
4. Fact extractor saves: "front door is painted SW 6468 Hunt Club green" with `image_path` linking to the saved file
5. Episode extractor may save: "Kevin shared a photo of their newly painted front door in a deep hunter green, excited about the color choice"
6. On future retrieval, the fact or episode includes `[image: /v1/agent/images/conv_abc_msg_1.jpg]`
7. The LLM renders it as a markdown image in its response: `![front door](/v1/agent/images/conv_abc_msg_1.jpg)`

### Memory-First Routing

When RAG results include image-linked facts with high relevance (similarity > 0.55), the routing logic short-circuits to conversational mode:

```python
if _memory_has_images:
    memory_has_answer = True
    log.info("Memory-first: image-linked facts in context — routing conversational")
```

This bypasses the tool bridge entirely. If the user says "what color was my front door again?", the answer is already in the system prompt from the image-linked fact — spending 50 seconds on tool discovery would be wasteful.

## Embedding Strategy

Both layers use nomic-embed-text via discovery's Ollama pass-through (`POST /api/embed`). The embedding model uses task-type prefixes per its documentation:

- **Storage**: `search_document: <text>` prefix for facts and episode descriptions
- **Query**: `search_query: <text>` prefix for user messages at retrieval time

Embeddings are 768-dimensional float vectors, packed into compact BLOBs via `struct.pack("<768f")` (3072 bytes per embedding). Cosine similarity is computed in Python — no external vector database, no ChromaDB dependency for the agent's memory (unlike discovery's experience cache which uses ChromaDB).

Batch embedding on startup catches any facts or episodes that failed to embed during extraction:

```python
# On startup
await embed_missing_facts(db, discovery_url)
await embed_missing_episodes(db, discovery_url)
```

## Phase 2 Roadmap

### Associative Linking

A `memory_links` table connecting episodes to episodes, episodes to facts, and facts to facts:

```sql
CREATE TABLE IF NOT EXISTS memory_links (
    id        TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    link_type TEXT NOT NULL,  -- 'co-retrieval', 'causal', 'temporal', 'person'
    strength  REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL
);
```

When two episodes are retrieved together in the same context, a `co-retrieval` link forms between them. Over time, frequently co-retrieved memories develop strong links. Retrieval then spreads activation through the network: retrieving one memory boosts the scores of linked memories, even if they have low direct similarity to the query.

This models associative recall — "pine smell" → "grandfather's workshop" → "building a birdhouse" — where each hop follows a link rather than a vector similarity match.

### Nightly Consolidation

A cron job (or APScheduler task) that runs during idle hours:

1. **Semantic extraction from episodes.** Scan recent episodes and extract any durable facts that should also exist in the semantic layer. "Kevin and Amy went house hunting in the Pearl District last weekend" → fact: "interested in Pearl District real estate."

2. **Link strengthening.** Episodes that were co-retrieved in the same conversation get their link strength incremented. Links that haven't been traversed in 30 days get their strength decremented.

3. **Decay pass.** Episodes with reference_count = 1 and age > 180 days have their embeddings dropped (still queryable by metadata, but no longer appear in vector search). This is the "forgotten but not deleted" state — a direct prompt like "do you remember when..." could still find them via keyword search on description/tags/people.

### Explicit Temporal Decay (Ebbinghaus Curve)

Replace the current linear decay with an exponential forgetting curve:

```
retention = e^(-t / (S * stability_factor))
```

Where `t` is time since last reference, `S` is a base stability constant, and `stability_factor` incorporates emotional intensity and reference count. High-emotion memories have a higher stability factor, meaning they decay more slowly.

Each retrieval would reset `t` and increase `stability_factor` by a fixed increment — modeling the spacing effect where spaced retrieval strengthens memory more than massed retrieval.

## Comparison With Other Approaches

| Approach | Strengths | What's Missing |
|----------|-----------|----------------|
| **RAG over vector DB** (Pinecone, Chroma) | Fast similarity search at scale | No emotional weighting, no temporal decay, no distinction between facts and experiences. All entries are equally "remembered." |
| **Simple note-taking** (Mem0, MemGPT) | Structured storage | Facts only — no episodic narrative, no emotional valence, no weighted retrieval. Memory feels like a database query. |
| **Full conversation history** | Complete context | Doesn't scale. At 1000 conversations, you can't inject history into a 4K context window. No mechanism to surface *relevant* history. |
| **Manifest synthetic memory** | Emotional weighting, temporal decay, two-layer architecture, visual linking | No associative linking yet. Linear decay instead of exponential. No consolidation process. |

The key insight: **emotional weight and temporal decay make retrieval feel human rather than mechanical.** When you ask a RAG system "tell me about my family," it returns the top-K most similar entries. When you ask Manifest, it returns the entries that are similar *and* emotionally significant *and* well-rehearsed — the memories that a human companion would actually bring up.

This is the difference between an assistant that knows facts about you and a companion that remembers your life with you.

## Key Files

- `agent/oap_agent/db.py` — Schema, CRUD, `search_episodes()` with cosine similarity, `search_facts()` with pinned-first ranking, embedding pack/unpack
- `agent/oap_agent/memory.py` — `EPISODE_EXTRACTION_SYSTEM` prompt, `extract_and_store_episode()`, `extract_and_store_facts()`, `_semantic_dedup()`, `_check_supersession()`, `embed_missing_episodes()`
- `agent/oap_agent/api.py` — Weighted retrieval scoring (`_ep_score`), system prompt injection (fact bullets + episode narratives), memory-first routing, extraction trigger gating
