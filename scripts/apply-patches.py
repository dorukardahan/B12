#!/usr/bin/env python3
"""
B12 Patch Applier — re-applies all B12 patches to sqlite_vec.py after pipx upgrade.

Patches:
  1. FTS5 method definitions (_init_fts5, _build_fts_query, _get_hybrid_weights, _get_fts5_scores)
  2. FTS5 init call in __init__
  3. Hybrid scoring in retrieve() (over-fetch, m.id SELECT, hybrid re-ranking, relevance)
  4. Hybrid scoring + deleted_at in recall() semantic path
  5. deleted_at IS NULL in recall() time-based path

Usage:
  python3 apply-patches.py          # Apply all patches
  python3 apply-patches.py --check  # Detection only, no modifications

Designed for: mcp-memory-service v10.7.2
"""

import os
import sys
import shutil
from datetime import datetime
from pathlib import Path


# ─── Patch content ────────────────────────────────────────────────────

FTS5_METHODS = '''\
    def _init_fts5(self):
        """Initialize FTS5 full-text search for hybrid retrieval (B12 patch)."""
        try:
            self.conn.execute(\'\'\'
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    content, tags, tokenize='unicode61'
                )
            \'\'\')

            # Sync triggers: INSERT (only non-deleted)
            self.conn.execute(\'\'\'
                CREATE TRIGGER IF NOT EXISTS fts_insert AFTER INSERT ON memories
                WHEN new.deleted_at IS NULL
                BEGIN
                    INSERT INTO memory_fts(rowid, content, tags)
                    VALUES (new.id, new.content, COALESCE(new.tags, ''));
                END
            \'\'\')
            # Sync triggers: UPDATE (re-index if still active)
            self.conn.execute(\'\'\'
                CREATE TRIGGER IF NOT EXISTS fts_update AFTER UPDATE ON memories
                WHEN new.deleted_at IS NULL
                BEGIN
                    DELETE FROM memory_fts WHERE rowid = old.id;
                    INSERT INTO memory_fts(rowid, content, tags)
                    VALUES (new.id, new.content, COALESCE(new.tags, ''));
                END
            \'\'\')
            # Sync triggers: soft-delete (remove from index)
            self.conn.execute(\'\'\'
                CREATE TRIGGER IF NOT EXISTS fts_softdel AFTER UPDATE ON memories
                WHEN new.deleted_at IS NOT NULL
                BEGIN
                    DELETE FROM memory_fts WHERE rowid = old.id;
                END
            \'\'\')
            # Sync triggers: hard delete
            self.conn.execute(\'\'\'
                CREATE TRIGGER IF NOT EXISTS fts_hardel AFTER DELETE ON memories
                BEGIN
                    DELETE FROM memory_fts WHERE rowid = old.id;
                END
            \'\'\')

            # Backfill existing memories into FTS5 index
            fts_count = self.conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
            mem_count = self.conn.execute(
                "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
            ).fetchone()[0]
            if fts_count == 0 and mem_count > 0:
                self.conn.execute(\'\'\'
                    INSERT INTO memory_fts(rowid, content, tags)
                    SELECT id, content, COALESCE(tags, '')
                    FROM memories WHERE deleted_at IS NULL
                \'\'\')
                self.conn.commit()
                logger.info(f"FTS5: Backfilled {mem_count} memories into full-text index")

            self._fts5_available = True
            logger.info("FTS5 full-text search initialized successfully")
        except Exception as e:
            self._fts5_available = False
            logger.warning(f"FTS5 init failed (non-fatal, keyword search disabled): {e}")

    def _build_fts_query(self, query: str) -> str:
        """Build FTS5 query with phrase detection from natural language input."""
        import re
        cleaned = re.sub(r'[^\\w\\s-]', ' ', query)
        words = [w.strip() for w in cleaned.split() if len(w.strip()) > 1]
        if not words:
            return ''
        # Detect bigram phrases (adjacent words that appear together in query)
        parts = []
        i = 0
        while i < len(words):
            if i + 1 < len(words):
                bigram = f"{words[i]} {words[i+1]}"
                if bigram.lower() in cleaned.lower():
                    parts.append(f'NEAR("{words[i]}" "{words[i+1]}", 2)')
                    i += 2
                    continue
            parts.append(f'"{words[i]}"')
            i += 1
        return ' OR '.join(parts[:10])

    def _get_hybrid_weights(self, query: str) -> tuple:
        """Adaptive hybrid weights based on query characteristics.
        Returns (vec_weight, fts_weight).
        - Technical/specific queries \u2192 higher FTS5 (keyword matches matter more)
        - Conceptual/vague queries \u2192 higher vector (semantic similarity matters more)
        """
        import re
        # Technical indicators: file paths, error codes, function names, specific identifiers
        technical_patterns = [
            r'[/\\\\][\\w.-]+\\.\\w+',         # file paths
            r'\\b[A-Z_]{3,}\\b',            # constants/env vars (e.g., ECONNREFUSED)
            r'\\b\\w+\\.\\w+\\(\\)',            # function calls (e.g., retrieve())
            r'\\b0x[0-9a-fA-F]+\\b',        # hex values
            r'(?:error|bug|fix|crash)\\b',  # error-related terms
            r'\\b\\d{3,}\\b',                # numeric codes
            r'\\bv\\d+\\.\\d+',              # version numbers
        ]
        tech_score = sum(1 for p in technical_patterns if re.search(p, query))

        if tech_score >= 3:
            return (0.5, 0.5)   # Heavy technical \u2192 balanced (keywords very important)
        elif tech_score >= 1:
            return (0.6, 0.4)   # Some technical \u2192 slightly more vector
        else:
            return (0.7, 0.3)   # Conceptual \u2192 default (vector dominant)

    def _get_fts5_scores(self, query: str, limit: int) -> Dict[int, float]:
        """Get normalized FTS5 BM25 scores. Returns {memory_id: 0.0-1.0}."""
        if not getattr(self, '_fts5_available', False):
            return {}
        try:
            fts_query = self._build_fts_query(query)
            if not fts_query:
                return {}
            cursor = self.conn.execute(
                'SELECT rowid, rank FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?',
                (fts_query, limit)
            )
            raw = {}
            for rowid, rank in cursor.fetchall():
                raw[rowid] = -rank  # FTS5 rank is negative (closer to 0 = better)
            if not raw:
                return {}
            max_score = max(raw.values())
            if max_score <= 0:
                return {}
            return {k: v / max_score for k, v in raw.items()}
        except Exception as e:
            logger.debug(f"FTS5 search failed (non-fatal): {e}")
            return {}

'''


# ─── Patch application engine ────────────────────────────────────────

def find_sqlite_vec():
    """Find sqlite_vec.py in the pipx venv."""
    venv_base = Path.home() / ".local" / "pipx" / "venvs" / "mcp-memory-service"
    if not venv_base.exists():
        print(f"  [ERROR] pipx venv not found: {venv_base}")
        sys.exit(1)

    candidates = list(venv_base.rglob("storage/sqlite_vec.py"))
    if not candidates:
        print(f"  [ERROR] sqlite_vec.py not found under {venv_base}")
        sys.exit(1)

    return str(candidates[0])


def get_version(venv_base=None):
    """Get installed mcp-memory-service version."""
    if venv_base is None:
        venv_base = Path.home() / ".local" / "pipx" / "venvs" / "mcp-memory-service"
    metadata_dirs = list(venv_base.rglob("mcp_memory_service-*.dist-info/METADATA"))
    if metadata_dirs:
        for line in metadata_dirs[0].read_text().splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    return "unknown"


def backup_file(filepath):
    """Create timestamped backup. Returns backup path."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{filepath}.b12backup.{ts}"
    shutil.copy2(filepath, backup)
    return backup


def clear_bytecache(filepath):
    """Clear __pycache__ dirs near the file."""
    parent = Path(filepath).parent
    for cache_dir in parent.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)
    # Also clear the storage package cache
    storage_pkg = parent
    cache = storage_pkg / "__pycache__"
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)


def replace_once(content, find, replace, name):
    """Replace first occurrence. Returns (new_content, success, message).
    Handles trailing whitespace on blank lines transparently."""
    import re

    # Try exact match first
    if find in content:
        count = content.count(find)
        if count > 1:
            return content, False, f"[{name}] SKIP - pattern found {count} times (ambiguous)"
        return content.replace(find, replace, 1), True, f"[{name}] APPLIED"

    # Build flexible regex: allow optional trailing whitespace on blank lines
    # Split find pattern into lines, escape each for regex, then allow flexible
    # whitespace on blank lines (upstream often has trailing spaces on empty lines)
    lines = find.split('\n')
    regex_lines = []
    for line in lines:
        if line.strip() == '':
            # Blank line — allow any amount of whitespace
            regex_lines.append(r'[ \t]*')
        else:
            regex_lines.append(re.escape(line))
    pattern = r'\n'.join(regex_lines)

    matches = list(re.finditer(pattern, content))
    if len(matches) == 1:
        m = matches[0]
        return content[:m.start()] + replace + content[m.end():], True, f"[{name}] APPLIED (ws-flex)"
    elif len(matches) > 1:
        return content, False, f"[{name}] SKIP - pattern found {len(matches)} times (ambiguous)"

    return content, False, f"[{name}] SKIP - pattern not found"


def apply_all_patches(content, check_only=False):
    """Apply all B12 patches. Returns (new_content, report_lines, applied_count)."""
    report = []
    applied = 0

    # ── Patch 1: FTS5 method definitions ──
    marker = "def _init_fts5(self):"
    if marker in content:
        report.append("[patch-1: fts5_methods] ALREADY PRESENT")
    elif check_only:
        report.append("[patch-1: fts5_methods] MISSING")
    else:
        anchor = "    def _check_extension_support(self):"
        if anchor not in content:
            report.append("[patch-1: fts5_methods] SKIP - anchor _check_extension_support not found")
        else:
            idx = content.index(anchor)
            content = content[:idx] + FTS5_METHODS + content[idx:]
            report.append("[patch-1: fts5_methods] APPLIED (4 methods inserted)")
            applied += 1

    # ── Patch 2: FTS5 init call ──
    # Must target the __init__ new-DB path (12-space indent), NOT the existing-DB path (20-space)
    marker = "self._init_fts5()"
    if marker in content:
        report.append("[patch-2: fts5_init_call] ALREADY PRESENT")
    elif check_only:
        report.append("[patch-2: fts5_init_call] MISSING")
    else:
        find = """            self._run_graph_migrations()

            # Mark as initialized to prevent re-initialization"""
        repl = """            self._run_graph_migrations()

            # Initialize FTS5 full-text search for hybrid retrieval (B12 patch)
            self._init_fts5()

            # Mark as initialized to prevent re-initialization"""
        content, s, m = replace_once(content, find, repl, "patch-2: fts5_init_call")
        if s:
            report.append("[patch-2: fts5_init_call] APPLIED")
            applied += 1
        else:
            report.append(m)

    # ── Patch 3: retrieve() modifications ──
    retrieve_marker = "# FTS5 hybrid re-ranking (B12 patch v2"
    # Check if already applied by looking for the marker in retrieve context
    retrieve_start = content.find("async def retrieve(self, query: str")
    if retrieve_start == -1:
        report.append("[patch-3: retrieve_hybrid] SKIP - retrieve() method not found")
    else:
        # Find end of retrieve method
        next_method = content.find("\n    async def ", retrieve_start + 10)
        retrieve_end = next_method if next_method != -1 else len(content)
        retrieve_code = content[retrieve_start:retrieve_end]

        if retrieve_marker in retrieve_code:
            report.append("[patch-3: retrieve_hybrid] ALREADY PRESENT")
        elif check_only:
            report.append("[patch-3: retrieve_hybrid] MISSING")
        else:
            ok = True
            # 3a: Over-fetch line
            find = "                return []\n            \n            # Perform vector similarity search"
            repl = "                return []\n\n            # Over-fetch for hybrid re-ranking (B12 patch: FTS5 + vector)\n            vector_k = min(n_results * 3, embedding_count)\n\n            # Perform vector similarity search"
            content, s, m = replace_once(content, find, repl, "patch-3a: overfetch")
            if not s:
                report.append(m)
                ok = False

            # 3b: Remove old comments + add m.id to SELECT
            find = """                # Try direct rowid join first - use k=? syntax for sqlite-vec
                # Note: ORDER BY distance is implicit with k=? and redundant in subquery
                cursor = self.conn.execute(\'\'\'
                    SELECT m.content_hash, m.content, m.tags, m.memory_type, m.metadata,"""
            repl = """                cursor = self.conn.execute(\'\'\'
                    SELECT m.id, m.content_hash, m.content, m.tags, m.memory_type, m.metadata,"""
            content, s, m = replace_once(content, find, repl, "patch-3b: select_id")
            if not s:
                report.append(m)
                ok = False

            # 3c: Change k param from n_results to vector_k (tuple form, unique to retrieve)
            find = "''', (serialize_float32(query_embedding), n_results))\n                \n                # Check if we got results\n                results = cursor.fetchall()"
            repl = "''', (serialize_float32(query_embedding), vector_k))\n\n                results = cursor.fetchall()"
            content, s, m = replace_once(content, find, repl, "patch-3c: k_param")
            if not s:
                report.append(m)
                ok = False

            # 3d: Remove "Log debug info" comment
            find = "                if not results:\n                    # Log debug info\n                    logger.debug"
            repl = "                if not results:\n                    logger.debug"
            content, s, m = replace_once(content, find, repl, "patch-3d: comment")
            if not s:
                report.append(m)
                # Non-critical, continue

            # 3e: Insert hybrid block + change loop + parse
            find = """            search_results = await self._execute_with_retry(search_memories)

            results = []
            for row in search_results:
                try:
                    # Parse row data
                    content_hash, content, tags_str, memory_type, metadata_str = row[:5]
                    created_at, updated_at, created_at_iso, updated_at_iso, distance = row[5:]"""
            repl = """            search_results = await self._execute_with_retry(search_memories)

            # FTS5 hybrid re-ranking (B12 patch v2: adaptive weights)
            fts_scores = self._get_fts5_scores(query, vector_k)
            vec_weight, fts_weight = self._get_hybrid_weights(query)
            scored_results = []
            for row in search_results:
                mem_id = row[0]
                distance = row[10]
                vec_score = max(0.0, 1.0 - (float(distance) / 2.0)) if distance is not None else 0.0
                fts_score = fts_scores.get(mem_id, 0.0)
                hybrid = vec_weight * vec_score + fts_weight * fts_score if fts_scores else vec_score
                scored_results.append((row, hybrid))
            scored_results.sort(key=lambda x: x[1], reverse=True)

            results = []
            for row, hybrid_score in scored_results[:n_results]:
                try:
                    # Parse row data (column 0 is m.id, used for FTS5 matching)
                    content_hash, content, tags_str, memory_type, metadata_str = row[1:6]
                    created_at, updated_at, created_at_iso, updated_at_iso, distance = row[6:]"""
            content, s, m = replace_once(content, find, repl, "patch-3e: hybrid_block")
            if not s:
                report.append(m)
                ok = False

            # 3f: Change relevance score
            find = """                    # Calculate relevance score (lower distance = higher relevance)
                    # For cosine distance: distance ranges from 0 (identical) to 2 (opposite)
                    # Convert to similarity score: 1 - (distance/2) gives 0-1 range
                    relevance_score = max(0.0, 1.0 - (float(distance) / 2.0)) if distance is not None else 0.0"""
            repl = """                    # Hybrid relevance score (vector + FTS5 keyword boost)
                    relevance_score = hybrid_score"""
            content, s, m = replace_once(content, find, repl, "patch-3f: relevance")
            if not s:
                report.append(m)
                ok = False

            # 3g: Change debug_info
            find = 'debug_info={"distance": distance, "backend": "sqlite-vec"}'
            repl = 'debug_info={"distance": distance, "hybrid_score": hybrid_score, "backend": "sqlite-vec+fts5"}'
            content, s, m = replace_once(content, find, repl, "patch-3g: debug_info")
            if not s:
                report.append(m)
                ok = False

            if ok:
                report.append("[patch-3: retrieve_hybrid] APPLIED (7 sub-patches)")
                applied += 1
            else:
                report.append("[patch-3: retrieve_hybrid] PARTIAL — check sub-patches above")

    # ── Patch 4: recall() semantic path ──
    recall_marker = "# Combined semantic search with time filtering + FTS5 hybrid (B12 patch)"
    recall_start = content.find("async def recall(self,")
    if recall_start == -1:
        report.append("[patch-4: recall_hybrid] SKIP - recall() method not found")
    else:
        next_method = content.find("\n    async def ", recall_start + 10)
        recall_end = next_method if next_method != -1 else len(content)
        recall_code = content[recall_start:recall_end]

        if recall_marker in recall_code:
            report.append("[patch-4: recall_hybrid] ALREADY PRESENT")
        elif check_only:
            report.append("[patch-4: recall_hybrid] MISSING")
        else:
            ok = True

            # 4a: Comment + embedding + overfetch + SELECT m.id
            find = """                # Combined semantic search with time filtering
                try:
                    # Generate query embedding
                    query_embedding = self._generate_embedding(query)

                    # Build SQL query with time filtering
                    base_query = \'\'\'
                        SELECT m.content_hash, m.content, m.tags, m.memory_type, m.metadata,"""
            repl = """                # Combined semantic search with time filtering + FTS5 hybrid (B12 patch)
                try:
                    query_embedding = self._generate_embedding(query)

                    # Over-fetch for hybrid re-ranking
                    emb_count = self.conn.execute('SELECT COUNT(*) FROM memory_embeddings').fetchone()[0]
                    vector_k = min(n_results * 3, emb_count) if emb_count > 0 else n_results

                    base_query = \'\'\'
                        SELECT m.id, m.content_hash, m.content, m.tags, m.memory_type, m.metadata,"""
            content, s, m = replace_once(content, find, repl, "patch-4a: header+select")
            if not s:
                report.append(m)
                ok = False

            # 4b: WHERE deleted_at + k param (recall semantic path)
            find = """                    if time_where:
                        base_query += f" WHERE {time_where}"

                    base_query += " ORDER BY e.distance"

                    # Prepare parameters: embedding, limit, then time filter params
                    query_params = [serialize_float32(query_embedding), n_results] + params

                    cursor = self.conn.execute(base_query, query_params)

                    results = []
                    for row in cursor.fetchall():
                        try:
                            # Parse row data
                            content_hash, content, tags_str, memory_type, metadata_str = row[:5]
                            created_at, updated_at, created_at_iso, updated_at_iso, distance = row[5:]"""
            repl = """                    if time_where:
                        base_query += f" WHERE m.deleted_at IS NULL AND {time_where}"
                    else:
                        base_query += " WHERE m.deleted_at IS NULL"

                    base_query += " ORDER BY e.distance"

                    query_params = [serialize_float32(query_embedding), vector_k] + params

                    cursor = self.conn.execute(base_query, query_params)
                    all_rows = cursor.fetchall()

                    # FTS5 hybrid re-ranking (B12 patch v2: adaptive weights)
                    fts_scores = self._get_fts5_scores(query, vector_k)
                    vec_weight, fts_weight = self._get_hybrid_weights(query)
                    scored = []
                    for row in all_rows:
                        mem_id = row[0]
                        distance = row[10]
                        vec_score = max(0.0, 1.0 - (float(distance) / 2.0)) if distance is not None else 0.0
                        fts_score = fts_scores.get(mem_id, 0.0)
                        hybrid = vec_weight * vec_score + fts_weight * fts_score if fts_scores else vec_score
                        scored.append((row, hybrid))
                    scored.sort(key=lambda x: x[1], reverse=True)

                    results = []
                    for row, hybrid_score in scored[:n_results]:
                        try:
                            content_hash, content, tags_str, memory_type, metadata_str = row[1:6]
                            created_at, updated_at, created_at_iso, updated_at_iso, distance = row[6:]"""
            content, s, m = replace_once(content, find, repl, "patch-4b: where+hybrid")
            if not s:
                report.append(m)
                ok = False

            # 4b2: Remove stale upstream comments in recall (match production)
            find = """                            created_at, updated_at, created_at_iso, updated_at_iso, distance = row[6:]

                            # Parse tags and metadata
                            tags"""
            repl = """                            created_at, updated_at, created_at_iso, updated_at_iso, distance = row[6:]

                            tags"""
            content, s, _ = replace_once(content, find, repl, "patch-4b2: comment1")
            # Non-critical cosmetic cleanup

            find = """                            metadata = self._safe_json_loads(metadata_str, "memory_metadata")

                            # Create Memory object
                            memory = Memory("""
            repl = """                            metadata = self._safe_json_loads(metadata_str, "memory_metadata")

                            memory = Memory("""
            content, s, _ = replace_once(content, find, repl, "patch-4b3: comment2")
            # Non-critical cosmetic cleanup

            # 4c: Relevance score in recall
            find = """                            # Calculate relevance score (lower distance = higher relevance)
                            relevance_score = max(0.0, 1.0 - distance)"""
            repl = """                            relevance_score = hybrid_score"""
            content, s, m = replace_once(content, find, repl, "patch-4c: relevance")
            if not s:
                report.append(m)
                ok = False

            # 4d: Debug info in recall
            find = 'debug_info={"distance": distance, "backend": "sqlite-vec", "time_filtered": bool(time_where)}'
            repl = 'debug_info={"distance": distance, "hybrid_score": hybrid_score, "backend": "sqlite-vec+fts5", "time_filtered": bool(time_where)}'
            content, s, m = replace_once(content, find, repl, "patch-4d: debug_info")
            if not s:
                report.append(m)
                ok = False

            # 4e: Log message
            find = 'logger.info(f"Retrieved {len(results)} memories for semantic query with time filter")'
            repl = 'logger.info(f"Retrieved {len(results)} memories for hybrid query with time filter")'
            content, s, m = replace_once(content, find, repl, "patch-4e: log_msg")
            if not s:
                report.append(m)
                # Non-critical

            if ok:
                report.append("[patch-4: recall_hybrid] APPLIED (5 sub-patches)")
                applied += 1
            else:
                report.append("[patch-4: recall_hybrid] PARTIAL — check sub-patches above")

    # ── Patch 5: recall() time-based path deleted_at ──
    # This is the fallback path (non-semantic) in recall()
    time_marker = 'base_query += f" WHERE deleted_at IS NULL AND {time_where}"'
    if time_marker in content:
        report.append("[patch-5: recall_time_deleted_at] ALREADY PRESENT")
    elif check_only:
        report.append("[patch-5: recall_time_deleted_at] MISSING")
    else:
        # Find the time-based path pattern (after "Time-based filtering only" comment)
        find = """            if time_where:
                base_query += f" WHERE {time_where}"

            base_query += " ORDER BY created_at DESC LIMIT ?\""""
        repl = """            if time_where:
                base_query += f" WHERE deleted_at IS NULL AND {time_where}"
            else:
                base_query += " WHERE deleted_at IS NULL"

            base_query += " ORDER BY created_at DESC LIMIT ?\""""
        content, s, m = replace_once(content, find, repl, "patch-5: time_deleted_at")
        if s:
            report.append("[patch-5: recall_time_deleted_at] APPLIED")
            applied += 1
        else:
            report.append(m)

    return content, report, applied


# ─── Main ────────────────────────────────────────────────────────────

def main():
    check_only = "--check" in sys.argv

    print("=== B12 Patch Applier ===")
    print(f"Mode: {'CHECK (dry run)' if check_only else 'APPLY'}")

    # Find file
    filepath = find_sqlite_vec()
    print(f"File: {filepath}")

    # Version check
    version = get_version()
    print(f"Version: {version}")
    if not version.startswith("10.7."):
        print(f"  [WARN] Patches designed for v10.7.x, found v{version}")
        print(f"  [WARN] Anchor patterns may not match — proceeding with caution")

    # Read content
    content = Path(filepath).read_text()

    # Apply patches
    new_content, report, applied = apply_all_patches(content, check_only=check_only)

    # Print report
    print()
    for line in report:
        print(f"  {line}")
    print()

    if check_only:
        missing = sum(1 for r in report if "MISSING" in r)
        present = sum(1 for r in report if "ALREADY PRESENT" in r)
        print(f"Status: {present} present, {missing} missing")
        sys.exit(0 if missing == 0 else 1)

    if applied == 0:
        print("No patches needed — all already applied.")
        sys.exit(0)

    # Backup and write
    backup = backup_file(filepath)
    print(f"Backup: {backup}")

    Path(filepath).write_text(new_content)
    print(f"Written: {filepath}")

    # Clear bytecache
    clear_bytecache(filepath)
    print("Bytecache cleared.")

    print(f"\nDone: {applied} patch group(s) applied.")
    print("IMPORTANT: Restart Claude Code for changes to take effect.")


if __name__ == "__main__":
    main()
