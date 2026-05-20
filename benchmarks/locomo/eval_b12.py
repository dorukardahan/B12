#!/usr/bin/env python3
"""
B12 LoCoMo Benchmark — Evaluate B12's retrieval against LoCoMo QA dataset.

Tests how well B12's FTS5 hybrid search retrieves relevant context for
answering questions about long-term conversations.

Storage modes:
  - observations: Pre-extracted facts per session (closest to B12's extraction)
  - summaries: Session-level summaries
  - dialogues: Raw dialogue turns

Search modes:
  - keyword: FTS5 BM25 only (baseline)
  - hybrid: 70% BM25 + 30% vector cosine (B12 production config)
  - vector: Vector-only cosine similarity

Metrics:
  - Recall@k: Does the top-k retrieved content contain the answer?
  - Token F1: Overlap between retrieved content and gold answer

Usage:
  python3 eval_b12.py [--mode observations|summaries|dialogues] [--search keyword|hybrid|vector]
"""

import json
import os
import re
import sqlite3
import string
import struct
import sys
import time
from collections import Counter
from pathlib import Path

# Optional: sentence-transformers for vector search
_embedding_model = None
EMBED_DIM = int(__import__('os').environ.get('B12_EMBED_DIM', '1024'))

def get_embedding_model():
    """Lazy-load embedding model (same as B12 production)."""
    global _embedding_model
    if _embedding_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _embedding_model = SentenceTransformer('BAAI/bge-m3')
            global EMBED_DIM
            EMBED_DIM = int(_embedding_model.get_sentence_embedding_dimension() or 1024)
            print(f"  Embedding model loaded (dim={_embedding_model.get_sentence_embedding_dimension()})")
        except ImportError:
            print("  ERROR: sentence-transformers not found. Use the b12-venv:")
            print("    $HOME/.local/b12-venv/bin/python3 eval_b12.py")
            sys.exit(1)
    return _embedding_model

def embed_texts(texts, batch_size=64):
    """Generate embeddings for a list of texts."""
    model = get_embedding_model()
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i+batch_size]
        embs = model.encode(batch, normalize_embeddings=True, show_progress_bar=False)
        all_embeddings.extend(embs)
    return all_embeddings

def serialize_f32(vector):
    """Serialize a float32 vector for sqlite-vec (little-endian)."""
    return struct.pack(f'{len(vector)}f', *vector)

# ── Config ──────────────────────────────────────────────────────────────

DATA_FILE = Path(__file__).parent / "locomo10.json"
TEST_DB = Path(__file__).parent / "test_memory.db"
TOP_K_VALUES = [1, 3, 5, 10]

CATEGORIES = {1: "multi-hop", 2: "single-hop", 3: "temporal", 4: "open-domain", 5: "adversarial"}

# ── Metrics (from LoCoMo's evaluation.py) ───────────────────────────────

def normalize_answer(s):
    """Lower text, remove punctuation, articles and extra whitespace."""
    s = str(s).lower()
    s = s.replace(",", "")
    # Remove articles
    s = re.sub(r'\b(a|an|the|and)\b', ' ', s)
    # Remove punctuation
    s = s.translate(str.maketrans('', '', string.punctuation))
    # Fix whitespace
    s = ' '.join(s.split())
    return s

def f1_score(prediction, ground_truth):
    """Token-level F1 between prediction and ground truth."""
    pred_tokens = normalize_answer(prediction).split()
    truth_tokens = normalize_answer(ground_truth).split()
    if not pred_tokens or not truth_tokens:
        return float(pred_tokens == truth_tokens)
    common = Counter(pred_tokens) & Counter(truth_tokens)
    num_common = sum(common.values())
    if num_common == 0:
        return 0.0
    precision = num_common / len(pred_tokens)
    recall = num_common / len(truth_tokens)
    return (2 * precision * recall) / (precision + recall)

def has_answer(text, answer):
    """Check if normalized answer appears in normalized text."""
    norm_text = normalize_answer(text)
    norm_answer = normalize_answer(answer)
    if not norm_answer:
        return False
    # Check both as substring and as token overlap
    if norm_answer in norm_text:
        return True
    # Also check token-level: all answer tokens present
    answer_tokens = norm_answer.split()
    text_tokens = set(norm_text.split())
    return all(t in text_tokens for t in answer_tokens)

# ── Database ────────────────────────────────────────────────────────────

def create_test_db(db_path, use_vectors=False):
    """Create a fresh test database with FTS5 and optionally sqlite-vec."""
    if db_path.exists():
        db_path.unlink()
    # Also clean WAL/SHM from previous runs
    for ext in ['-wal', '-shm']:
        p = Path(str(db_path) + ext)
        if p.exists():
            p.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            content_hash TEXT,
            memory_type TEXT DEFAULT 'observation',
            tags TEXT DEFAULT '',
            metadata TEXT DEFAULT '{}',
            created_at INTEGER,
            deleted_at INTEGER,
            valid_until INTEGER,
            strength REAL DEFAULT 1.0,
            last_accessed_at INTEGER,
            conv_id INTEGER,
            session_key TEXT
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            content,
            content='memories',
            content_rowid='id'
        )
    """)
    # Sync triggers
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS memory_fts_insert AFTER INSERT ON memories BEGIN
            INSERT INTO memory_fts(rowid, content) VALUES (new.id, new.content);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS memory_fts_delete AFTER DELETE ON memories BEGIN
            INSERT INTO memory_fts(memory_fts, rowid, content) VALUES('delete', old.id, old.content);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS memory_fts_update AFTER UPDATE ON memories BEGIN
            INSERT INTO memory_fts(memory_fts, rowid, content) VALUES('delete', old.id, old.content);
            INSERT INTO memory_fts(rowid, content) VALUES (new.id, new.content);
        END
    """)

    if use_vectors:
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
                    memory_id INTEGER PRIMARY KEY,
                    embedding float[{EMBED_DIM}]
                )
            """)
            print(f"  sqlite-vec loaded ({EMBED_DIM}-dim vector table created)")
        except ImportError:
            print("  WARNING: sqlite-vec not found, vector search disabled")
            print("    Use: $HOME/.local/b12-venv/bin/python3 eval_b12.py")
            use_vectors = False

    conn.commit()
    return conn, use_vectors

# ── Ingest ──────────────────────────────────────────────────────────────

def _embed_and_store(conn, use_vectors):
    """Generate embeddings for all memories and store in memory_vec."""
    if not use_vectors:
        return
    rows = conn.execute("SELECT id, content FROM memories WHERE deleted_at IS NULL").fetchall()
    if not rows:
        return
    ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    print(f"  Generating embeddings for {len(texts)} memories...")
    t0 = time.time()
    embeddings = embed_texts(texts)
    elapsed = time.time() - t0
    print(f"  Embeddings generated in {elapsed:.1f}s")
    for mem_id, emb in zip(ids, embeddings):
        conn.execute(
            "INSERT INTO memory_vec (memory_id, embedding) VALUES (?, ?)",
            (mem_id, serialize_f32(emb))
        )
    conn.commit()

def ingest_observations(conn, data, use_vectors=False):
    """Store observations as memories (closest to B12's extraction)."""
    count = 0
    for conv_idx, conv_data in enumerate(data):
        obs = conv_data.get('observation', {})
        conv = conv_data['conversation']
        for session_key, session_obs in obs.items():
            # session_key like "session_1_observation"
            session_num = session_key.replace('_observation', '')
            dt_key = f"{session_num}_date_time"
            date_time = conv.get(dt_key, '')
            if isinstance(session_obs, dict):
                for speaker, facts in session_obs.items():
                    for fact_item in facts:
                        if isinstance(fact_item, list) and len(fact_item) >= 1:
                            text = fact_item[0]
                            content = f"[{date_time}] {text}"
                            conn.execute(
                                "INSERT INTO memories (content, memory_type, tags, conv_id, session_key) VALUES (?, ?, ?, ?, ?)",
                                (content, 'observation', f'speaker:{speaker}', conv_idx, session_num)
                            )
                            count += 1
    conn.commit()
    _embed_and_store(conn, use_vectors)
    return count

def ingest_summaries(conn, data, use_vectors=False):
    """Store session summaries as memories."""
    count = 0
    for conv_idx, conv_data in enumerate(data):
        ss = conv_data.get('session_summary', {})
        conv = conv_data['conversation']
        for session_key, summary in ss.items():
            session_num = session_key.replace('_summary', '')
            dt_key = f"{session_num}_date_time"
            date_time = conv.get(dt_key, '')
            content = f"[{date_time}] {summary}"
            conn.execute(
                "INSERT INTO memories (content, memory_type, tags, conv_id, session_key) VALUES (?, ?, ?, ?, ?)",
                (content, 'session_summary', '', conv_idx, session_num)
            )
            count += 1
    conn.commit()
    _embed_and_store(conn, use_vectors)
    return count

def ingest_dialogues(conn, data, use_vectors=False):
    """Store raw dialogue turns grouped by session."""
    count = 0
    for conv_idx, conv_data in enumerate(data):
        conv = conv_data['conversation']
        session_keys = [k for k in conv.keys() if k.startswith('session_') and not k.endswith('_date_time')]
        for session_key in sorted(session_keys):
            turns = conv[session_key]
            dt_key = f"{session_key}_date_time"
            date_time = conv.get(dt_key, '')
            # Group turns into a single memory per session
            lines = []
            for turn in turns:
                if isinstance(turn, dict):
                    speaker = turn.get('speaker', '?')
                    text = turn.get('text', '')
                    lines.append(f"{speaker}: {text}")
            if lines:
                content = f"[{date_time}]\n" + "\n".join(lines)
                conn.execute(
                    "INSERT INTO memories (content, memory_type, tags, conv_id, session_key) VALUES (?, ?, ?, ?, ?)",
                    (content, 'dialogue', '', conv_idx, session_key)
                )
                count += 1
    conn.commit()
    _embed_and_store(conn, use_vectors)
    return count

# ── Retrieval (B12-style FTS5) ──────────────────────────────────────────

STOP_WORDS = {
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'shall', 'can', 'to', 'of', 'in', 'for',
    'on', 'with', 'at', 'by', 'from', 'as', 'into', 'about', 'between',
    'through', 'during', 'before', 'after', 'above', 'below', 'up', 'down',
    'out', 'off', 'over', 'under', 'again', 'further', 'then', 'once',
    'and', 'but', 'or', 'nor', 'not', 'so', 'yet', 'both', 'each',
    'this', 'that', 'these', 'those', 'what', 'which', 'who', 'whom',
    'how', 'when', 'where', 'why', 'all', 'any', 'some', 'no', 'every',
    'it', 'its', 'he', 'she', 'they', 'them', 'his', 'her', 'their',
    'i', 'me', 'my', 'we', 'us', 'our', 'you', 'your',
}

def extract_keywords(question):
    """Extract search keywords from question (B12-style)."""
    words = re.findall(r'[a-zA-Z0-9]+', question.lower())
    keywords = [w for w in words if w not in STOP_WORDS and len(w) > 1]
    return keywords[:12]

def build_fts_query_v8(question, keywords):
    """v8 phrase-aware FTS5 query builder — detects bigrams for NEAR() queries."""
    safe_keywords = [re.sub(r"['\";(){}]", "", k) for k in keywords]
    if not safe_keywords:
        return ""

    question_lower = question.lower()
    fts_parts = []
    prev = None

    for kw in safe_keywords:
        if prev:
            bigram = f"{prev} {kw}"
            if bigram in question_lower:
                fts_parts.append(f"NEAR({prev} {kw}, 2)")
        if not fts_parts:
            fts_parts.append(kw)
        else:
            fts_parts.append(f"{kw}")
        prev = kw

    return " OR ".join(fts_parts)

def get_adaptive_weights(question):
    """v8 adaptive hybrid weights — technical queries get more FTS5 weight."""
    technical_patterns = [
        r'[/\\][\w.-]+\.\w+',         # file paths
        r'\b[A-Z_]{3,}\b',            # constants/env vars
        r'\b\w+\.\w+\(\)',            # function calls
        r'(?:error|bug|fix|crash)\b',  # error-related
        r'\b(?:0x[0-9a-f]+|[0-9]{4,})\b',  # hex/long numbers
        r'(?:config|setup|install)\b', # setup terms
    ]
    tech_score = sum(1 for p in technical_patterns if re.search(p, question, re.IGNORECASE))
    if tech_score >= 3:
        return (0.5, 0.5)
    elif tech_score >= 1:
        return (0.6, 0.4)
    else:
        return (0.7, 0.3)

def retrieve_fts5(conn, question, conv_id, top_k=5):
    """B12-style FTS5 retrieval with BM25 ranking (v8: phrase-aware)."""
    keywords = extract_keywords(question)
    if not keywords:
        return []

    # v8: phrase-aware FTS5 query
    fts_query = build_fts_query_v8(question, keywords)

    try:
        results = conn.execute("""
            SELECT m.id, m.content, m.memory_type, m.session_key,
                   rank as bm25_score
            FROM memories m
            JOIN memory_fts f ON m.id = f.rowid
            WHERE memory_fts MATCH ?
              AND m.conv_id = ?
              AND m.deleted_at IS NULL
            ORDER BY rank
            LIMIT ?
        """, (fts_query, conv_id, top_k)).fetchall()
    except Exception:
        # Fallback: try individual keywords
        safe_keywords = [re.sub(r"['\";(){}]", "", k) for k in keywords]
        results = []
        for kw in safe_keywords[:3]:
            try:
                r = conn.execute("""
                    SELECT m.id, m.content, m.memory_type, m.session_key,
                           rank as bm25_score
                    FROM memories m
                    JOIN memory_fts f ON m.id = f.rowid
                    WHERE memory_fts MATCH ?
                      AND m.conv_id = ?
                      AND m.deleted_at IS NULL
                    ORDER BY rank
                    LIMIT ?
                """, (kw, conv_id, top_k)).fetchall()
                results.extend(r)
            except Exception:
                pass
        # Dedup by id, keep best rank
        seen = {}
        for r in results:
            if r[0] not in seen:
                seen[r[0]] = r
        results = sorted(seen.values(), key=lambda x: x[4])[:top_k]

    return results

def retrieve_vector(conn, question, conv_id, top_k=5):
    """Vector-only retrieval using cosine similarity."""
    q_emb = embed_texts([question])[0]
    q_blob = serialize_f32(q_emb)

    results = []
    try:
        # sqlite-vec: query with embedding, then join
        results = conn.execute("""
            SELECT m.id, m.content, m.memory_type, m.session_key,
                   v.distance as cosine_dist
            FROM memory_vec v
            JOIN memories m ON m.id = v.memory_id
            WHERE v.embedding MATCH ?
              AND k = ?
              AND m.conv_id = ?
              AND m.deleted_at IS NULL
        """, (q_blob, top_k * 3, conv_id)).fetchall()
    except Exception:
        # Fallback: get all vectors for this conv, compute manually
        rows = conn.execute("""
            SELECT m.id, m.content, m.memory_type, m.session_key
            FROM memories m
            WHERE m.conv_id = ? AND m.deleted_at IS NULL
        """, (conv_id,)).fetchall()
        if not rows:
            return []
        ids = [r[0] for r in rows]
        texts = [r[1] for r in rows]
        # Get stored embeddings
        import numpy as np
        scores = []
        for row in rows:
            mem_id = row[0]
            emb_row = conn.execute(
                "SELECT embedding FROM memory_vec WHERE memory_id = ?", (mem_id,)
            ).fetchone()
            if emb_row:
                stored = struct.unpack(f'{EMBED_DIM}f', emb_row[0])
                cos_sim = sum(a*b for a,b in zip(q_emb, stored))
                scores.append((row[0], row[1], row[2], row[3], 1.0 - cos_sim))
        scores.sort(key=lambda x: x[4])
        results = scores[:top_k]

    return results[:top_k]


def retrieve_hybrid(conn, question, conv_id, top_k=5, bm25_weight=0.7, vec_weight=0.3):
    """B12-style hybrid: FTS5 candidates + vector rerank + vector expansion.

    Strategy (matching B12 v8 production):
    1. FTS5 generates candidate set (top_k * 3) — with phrase-aware queries
    2. Vector similarity reranks those candidates
    3. Top vector-only results added as expansion (catches paraphrased content)
    4. Combined score uses adaptive weights (technical vs conceptual)
       Pure cosine for expansion candidates (penalized by 0.8x)
    """
    # v8: adaptive weights based on query type
    bm25_weight, vec_weight = get_adaptive_weights(question)

    # Step 1: FTS5 candidates (phrase-aware via build_fts_query_v8)
    bm25_results = retrieve_fts5(conn, question, conv_id, top_k=top_k * 3)

    # Step 2: Query embedding
    q_emb = embed_texts([question])[0]

    # Step 3: Compute vector scores for FTS5 candidates
    candidate_scores = {}
    for r in bm25_results:
        mem_id, content, mtype, skey, bm25_rank = r
        emb_row = conn.execute(
            "SELECT embedding FROM memory_vec WHERE memory_id = ?", (mem_id,)
        ).fetchone()
        cos_sim = 0.0
        if emb_row:
            stored = struct.unpack(f'{EMBED_DIM}f', emb_row[0])
            cos_sim = max(0.0, sum(a * b for a, b in zip(q_emb, stored)))

        # Normalize BM25: rank is negative, closer to 0 = better
        # Use rank directly as penalty (less negative = better)
        bm25_norm = 1.0 / (1.0 + abs(bm25_rank))  # Sigmoid-like normalization

        combined = bm25_weight * bm25_norm + vec_weight * cos_sim
        candidate_scores[mem_id] = (content, mtype, skey, combined)

    # Step 4: Vector expansion — top similar memories NOT in FTS5 set
    fts_ids = set(candidate_scores.keys())
    expansion_ids = set()

    # Get top vector matches from this conversation
    all_mems = conn.execute("""
        SELECT m.id, m.content, m.memory_type, m.session_key
        FROM memories m
        WHERE m.conv_id = ? AND m.deleted_at IS NULL
    """, (conv_id,)).fetchall()

    vec_candidates = []
    for mem in all_mems:
        if mem[0] in fts_ids:
            continue
        emb_row = conn.execute(
            "SELECT embedding FROM memory_vec WHERE memory_id = ?", (mem[0],)
        ).fetchone()
        if emb_row:
            stored = struct.unpack(f'{EMBED_DIM}f', emb_row[0])
            cos_sim = sum(a * b for a, b in zip(q_emb, stored))
            if cos_sim > 0.3:  # Only add if reasonably similar
                vec_candidates.append((mem[0], mem[1], mem[2], mem[3], cos_sim))

    # Sort by similarity, take top few as expansion
    vec_candidates.sort(key=lambda x: x[4], reverse=True)
    for vc in vec_candidates[:max(1, top_k // 2)]:
        # Penalize expansion results (no keyword match = less confident)
        expansion_score = vec_weight * vc[4] * 0.8
        candidate_scores[vc[0]] = (vc[1], vc[2], vc[3], expansion_score)

    # Step 5: Rank all candidates, return top_k
    ranked = sorted(candidate_scores.items(), key=lambda x: x[1][3], reverse=True)[:top_k]

    results = []
    for mem_id, (content, mtype, skey, score) in ranked:
        results.append((mem_id, content, mtype, skey, score))

    return results


# ── Query-Adaptive Search Mode ─────────────────────────────────────────

def classify_query(question):
    """Classify query to pick optimal retrieval strategy.

    Logic based on LoCoMo benchmark findings:
    - Negation/adversarial → hybrid (vector expansion filters well, +18pp)
    - Attribute/preference/open-domain → keyword (exact match wins, +4.7pp)
    - Default → hybrid (wins on multi-hop, single-hop, temporal)
    """
    q_lower = question.lower()

    # 1. Negation/adversarial → hybrid (highest priority)
    if re.search(r'\b(never|nobody|no one|nothing|nowhere)\b|n\'t\b| not ', q_lower):
        return 'hybrid'
    if re.search(r'\b(false|untrue|incorrect|is it true)\b', q_lower):
        return 'hybrid'

    # 2. Attribute/preference/open-domain → keyword
    # These questions ask about personal traits where observations use literal words
    attribute_patterns = [
        r'\b(favorite|favourite|like[sd]?|enjoy[sed]*|prefer[sed]*)\b',
        r'\b(hobb(y|ies)|interest(s|ed)?|passion(ate)?|obsess|fond)\b',
        r'\b(love[sd]?|hate[sd]?|dislike[sd]?)\b',
        r'\bwhat (kind|type|sort) of\b',
        r'\btell me about\b',
        r'\bdescribe\b',
        r'\bwhat (do|does|did) .+ (think|feel|say|believe) about\b',
        r'\bhow (does|did|do) .+ (feel|react|respond)\b',
        r'\bopinion|views? on|attitude|outlook\b',
        r'\bin common\b',
        r'\brelationship (with|between)\b',
    ]
    if any(re.search(p, q_lower) for p in attribute_patterns):
        return 'keyword'

    # 3. Default → hybrid (wins on multi-hop, single-hop, temporal)
    return 'hybrid'


def retrieve_adaptive(conn, question, conv_id, top_k=5):
    """Query-adaptive retrieval: classify then pick best strategy.

    - Specific/factoid/adversarial → hybrid (vector reranking helps)
    - Broad/opinion → keyword (BM25 exact matching wins)
    - Fallback: if keyword returns < 2 results → try hybrid
    """
    mode = classify_query(question)

    if mode == 'hybrid':
        return retrieve_hybrid(conn, question, conv_id, top_k)
    else:
        # Keyword with fallback
        results = retrieve_fts5(conn, question, conv_id, top_k)
        if len(results) < 2:
            hybrid_results = retrieve_hybrid(conn, question, conv_id, top_k)
            if len(hybrid_results) > len(results):
                return hybrid_results
        return results


# ── Evaluation ──────────────────────────────────────────────────────────

def evaluate(data, conn, top_k_values, search_mode='keyword', use_vectors=False):
    """Run LoCoMo QA evaluation against B12 retrieval."""
    # Import ranking metrics
    from metrics import reciprocal_rank, ndcg_at_k, precision_at_k

    results = {k: {cat: {'recall': 0, 'total': 0, 'f1_sum': 0.0,
                          'rr_sum': 0.0, 'ndcg_sum': 0.0, 'precision_sum': 0.0}
                    for cat in CATEGORIES}
               for k in top_k_values}

    no_retrieval = 0
    total_qs = 0
    classify_stats = {'hybrid': 0, 'keyword': 0}

    # Select retrieval function
    if search_mode == 'adaptive' and use_vectors:
        retrieve_fn = retrieve_adaptive
    elif search_mode == 'hybrid' and use_vectors:
        retrieve_fn = retrieve_hybrid
    elif search_mode == 'vector' and use_vectors:
        retrieve_fn = retrieve_vector
    else:
        retrieve_fn = retrieve_fts5

    for conv_idx, conv_data in enumerate(data):
        for qa in conv_data['qa']:
            question = qa['question']
            answer = qa.get('answer', qa.get('adversarial_answer', ''))
            category = qa['category']
            total_qs += 1

            # Track classification stats for adaptive mode
            if search_mode == 'adaptive':
                classify_stats[classify_query(question)] += 1

            for top_k in top_k_values:
                retrieved = retrieve_fn(conn, question, conv_idx, top_k)

                if not retrieved:
                    no_retrieval += 1 if top_k == top_k_values[0] else 0
                    # For adversarial: no retrieval = correct behavior
                    if category == 5:
                        results[top_k][category]['recall'] += 1
                    results[top_k][category]['total'] += 1
                    continue

                # Combine retrieved content
                combined = " ".join([r[1] for r in retrieved])

                # Per-item relevance for ranking metrics
                retrieved_ids = list(range(len(retrieved)))
                if category == 5:
                    # Adversarial: "relevant" means NOT containing the answer
                    relevant_ids = {i for i, r in enumerate(retrieved)
                                    if not has_answer(r[1], answer)}
                else:
                    relevant_ids = {i for i, r in enumerate(retrieved)
                                    if has_answer(r[1], answer)}

                # Recall: does retrieved content contain the answer?
                if category == 5:
                    if not has_answer(combined, answer):
                        results[top_k][category]['recall'] += 1
                else:
                    if has_answer(combined, answer):
                        results[top_k][category]['recall'] += 1

                # F1: token overlap between retrieved and answer
                f1 = f1_score(combined, answer)
                results[top_k][category]['f1_sum'] += f1

                # Ranking metrics
                results[top_k][category]['rr_sum'] += reciprocal_rank(retrieved_ids, relevant_ids)
                results[top_k][category]['ndcg_sum'] += ndcg_at_k(retrieved_ids, relevant_ids, top_k)
                results[top_k][category]['precision_sum'] += precision_at_k(retrieved_ids, relevant_ids, top_k)

                results[top_k][category]['total'] += 1

    return results, total_qs, no_retrieval, classify_stats

# ── Report ──────────────────────────────────────────────────────────────

def compute_aggregates(results, top_k):
    """Compute aggregate metrics for a given top_k from per-category results."""
    total_recall = 0
    total_count = 0
    total_f1 = 0.0
    total_rr = 0.0
    total_ndcg = 0.0
    total_precision = 0.0

    for cat_id in CATEGORIES:
        r = results[top_k][cat_id]
        total_recall += r['recall']
        total_count += r['total']
        total_f1 += r['f1_sum']
        total_rr += r['rr_sum']
        total_ndcg += r['ndcg_sum']
        total_precision += r['precision_sum']

    if total_count == 0:
        return {}

    return {
        f'recall@{top_k}': total_recall / total_count,
        'token_f1': total_f1 / total_count,
        'mrr': total_rr / total_count,
        f'ndcg@{top_k}': total_ndcg / total_count,
        f'precision@{top_k}': total_precision / total_count,
    }


def results_to_json(mode, search_mode, results, top_k_values, total_qs,
                    no_retrieval, elapsed, category_detail=False):
    """Convert results to JSON-serializable dict."""
    key = f"{mode}-{search_mode}"
    metrics = {}

    for top_k in top_k_values:
        agg = compute_aggregates(results, top_k)
        metrics.update(agg)

    output = {key: metrics}

    if category_detail:
        for top_k in top_k_values:
            for cat_id in sorted(CATEGORIES.keys()):
                cat_name = CATEGORIES[cat_id]
                r = results[top_k][cat_id]
                if r['total'] == 0:
                    continue
                cat_key = f"{key}/{cat_name}"
                if cat_key not in output:
                    output[cat_key] = {}
                output[cat_key][f'recall@{top_k}'] = r['recall'] / r['total']
                output[cat_key]['token_f1'] = r['f1_sum'] / r['total']
                output[cat_key]['mrr'] = r['rr_sum'] / r['total']
                output[cat_key][f'ndcg@{top_k}'] = r['ndcg_sum'] / r['total']
                output[cat_key][f'precision@{top_k}'] = r['precision_sum'] / r['total']

    return output


def print_report(mode, mem_count, results, total_qs, no_retrieval, elapsed,
                 search_mode='keyword', classify_stats=None):
    """Print evaluation results."""
    print(f"\n{'='*65}")
    print(f"  B12 LoCoMo Benchmark — Storage: {mode} | Search: {search_mode}")
    print(f"{'='*65}")
    print(f"  Memories ingested: {mem_count}")
    print(f"  Total QA questions: {total_qs}")
    print(f"  Questions with no retrieval: {no_retrieval} ({no_retrieval/total_qs*100:.1f}%)")
    if classify_stats and (classify_stats.get('hybrid', 0) + classify_stats.get('keyword', 0)) > 0:
        h = classify_stats['hybrid']
        k = classify_stats['keyword']
        print(f"  Query routing: {h} hybrid ({h*100//(h+k)}%) + {k} keyword ({k*100//(h+k)}%)")
    print(f"  Evaluation time: {elapsed:.1f}s")
    print()

    for top_k in sorted(results.keys()):
        print(f"  ── Recall@{top_k} {'─'*45}")
        total_recall = 0
        total_count = 0
        answerable_recall = 0
        answerable_count = 0

        for cat_id in sorted(CATEGORIES.keys()):
            cat_name = CATEGORIES[cat_id]
            r = results[top_k][cat_id]
            if r['total'] == 0:
                print(f"    {cat_name:15s}  —  (no questions)")
                continue
            recall = r['recall'] / r['total']
            avg_f1 = r['f1_sum'] / r['total']
            total_recall += r['recall']
            total_count += r['total']
            if cat_id != 5:  # Exclude adversarial from answerable
                answerable_recall += r['recall']
                answerable_count += r['total']
            bar = '█' * int(recall * 20) + '░' * (20 - int(recall * 20))
            print(f"    {cat_name:15s}  {bar}  {recall*100:5.1f}%  (F1: {avg_f1:.3f})  n={r['total']}")

        if total_count > 0:
            overall = total_recall / total_count
            print(f"    {'─'*55}")
            print(f"    {'OVERALL':15s}  {'':20s}  {overall*100:5.1f}%  n={total_count}")
        if answerable_count > 0:
            ans_recall = answerable_recall / answerable_count
            print(f"    {'ANSWERABLE':15s}  {'':20s}  {ans_recall*100:5.1f}%  n={answerable_count}")

        # Print new metrics summary
        agg = compute_aggregates(results, top_k)
        if agg:
            print(f"    MRR: {agg['mrr']:.4f}  |  NDCG@{top_k}: {agg.get(f'ndcg@{top_k}', 0):.4f}  |  P@{top_k}: {agg.get(f'precision@{top_k}', 0):.4f}")
        print()


def check_regression(current_results, baseline_path, threshold=0.05):
    """Compare current results against a baseline file.

    Returns (passed, regressions) where regressions is a list of
    (config, metric, baseline_val, current_val, drop) tuples.
    """
    with open(baseline_path) as f:
        baseline = json.load(f)

    baseline_results = baseline.get('results', {})
    regressions = []

    for config_key, metrics in current_results.items():
        if '/' in config_key:  # Skip category detail entries
            continue
        if config_key not in baseline_results:
            continue
        base_metrics = baseline_results[config_key]
        for metric_name, current_val in metrics.items():
            base_val = base_metrics.get(metric_name)
            if base_val is None or base_val == 0:
                continue
            drop = (base_val - current_val) / base_val
            if drop > threshold:
                regressions.append((config_key, metric_name, base_val, current_val, drop))

    return len(regressions) == 0, regressions

# ── Main ────────────────────────────────────────────────────────────────

def main():
    import argparse
    from datetime import date
    parser = argparse.ArgumentParser(description='B12 LoCoMo Benchmark')
    parser.add_argument('--mode', choices=['observations', 'summaries', 'dialogues', 'all'],
                       default='all', help='Storage mode')
    parser.add_argument('--search', choices=['keyword', 'hybrid', 'vector', 'adaptive', 'compare'],
                       default='keyword', help='Search mode (compare runs keyword+hybrid+adaptive)')
    parser.add_argument('--top-k', type=int, nargs='+', default=[1, 3, 5, 10],
                       help='Top-k values for recall')
    parser.add_argument('--output', choices=['text', 'json'], default='text',
                       help='Output format (default: text)')
    parser.add_argument('--compare', metavar='FILE',
                       help='Compare against baseline JSON file for regression detection')
    parser.add_argument('--threshold', type=float, default=0.05,
                       help='Max allowed metric drop for regression (default: 0.05 = 5%%)')
    parser.add_argument('--save-baseline', metavar='FILE',
                       help='Save current run results as a new baseline JSON file')
    parser.add_argument('--category-detail', action='store_true',
                       help='Include per-category breakdown in JSON output')
    args = parser.parse_args()

    # Load data
    print("Loading LoCoMo dataset...")
    with open(DATA_FILE) as f:
        data = json.load(f)
    print(f"  {len(data)} conversations, {sum(len(c['qa']) for c in data)} QA pairs")

    # Determine search modes to run
    need_vectors = args.search in ('hybrid', 'vector', 'adaptive', 'compare')
    search_modes = ['keyword', 'hybrid', 'adaptive'] if args.search == 'compare' else [args.search]

    # Pre-load embedding model if needed
    if need_vectors:
        print("\nLoading embedding model...")
        get_embedding_model()

    modes = ['observations', 'summaries', 'dialogues'] if args.mode == 'all' else [args.mode]
    all_json_results = {}

    for mode in modes:
        # Create fresh test DB (with vectors if needed)
        conn, vectors_ok = create_test_db(TEST_DB, use_vectors=need_vectors)

        # Ingest
        print(f"\nIngesting ({mode})...")
        if mode == 'observations':
            count = ingest_observations(conn, data, use_vectors=vectors_ok)
        elif mode == 'summaries':
            count = ingest_summaries(conn, data, use_vectors=vectors_ok)
        elif mode == 'dialogues':
            count = ingest_dialogues(conn, data, use_vectors=vectors_ok)
        print(f"  {count} memories stored")

        for search_mode in search_modes:
            effective_mode = search_mode
            if search_mode in ('hybrid', 'vector') and not vectors_ok:
                print(f"\n  Skipping {search_mode} (no vector support)")
                continue

            # Evaluate
            print(f"\nEvaluating ({search_mode} search)...")
            t0 = time.time()
            results, total_qs, no_retrieval, classify_stats = evaluate(
                data, conn, args.top_k,
                search_mode=effective_mode, use_vectors=vectors_ok
            )
            elapsed = time.time() - t0

            # Console report (always)
            print_report(mode, count, results, total_qs, no_retrieval, elapsed,
                        search_mode=effective_mode, classify_stats=classify_stats)

            # Collect JSON results
            json_data = results_to_json(
                mode, effective_mode, results, args.top_k,
                total_qs, no_retrieval, elapsed,
                category_detail=args.category_detail
            )
            all_json_results.update(json_data)

        conn.close()

    # Cleanup test DB
    if TEST_DB.exists():
        TEST_DB.unlink()
    for ext in ['-wal', '-shm']:
        p = Path(str(TEST_DB) + ext)
        if p.exists():
            p.unlink()

    # JSON output
    if args.output == 'json' or args.save_baseline or args.compare:
        json_output = {
            "version": "12.0.0",
            "date": str(date.today()),
            "results": all_json_results,
        }

        if args.output == 'json':
            print(json.dumps(json_output, indent=2))

        # Save baseline
        if args.save_baseline:
            with open(args.save_baseline, 'w') as f:
                json.dump(json_output, f, indent=2)
            print(f"\nBaseline saved to {args.save_baseline}")

        # Regression detection
        if args.compare:
            if not os.path.exists(args.compare):
                print(f"\nWARNING: Baseline file not found: {args.compare}")
                print("  Run with --save-baseline to create one.")
            else:
                passed, regressions = check_regression(
                    all_json_results, args.compare, args.threshold
                )
                if passed:
                    print(f"\n  PASS: No regressions detected (threshold: {args.threshold*100:.0f}%)")
                else:
                    print(f"\n  FAIL: {len(regressions)} regression(s) detected!")
                    for cfg, metric, base, curr, drop in regressions:
                        print(f"    {cfg} / {metric}: {base:.4f} → {curr:.4f} (↓{drop*100:.1f}%)")
                    sys.exit(1)

    print("\nDone. Test database cleaned up.")

if __name__ == '__main__':
    main()
