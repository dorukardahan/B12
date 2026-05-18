#!/usr/bin/env python3
"""Migrate memory_embeddings from 384-dim (MiniLM) to 1024-dim (BGE-M3).

The vec0 virtual table hardcodes its dimension in the CREATE statement, so a
dim swap is destructive: the only safe path is DROP + recreate + re-encode.
This script gates that destructive step behind a filesystem backup of the
SQLite DB so a partial run can be rolled back manually.

Run:
  python3 scripts/migrate_embed_to_bge_m3.py            # do the migration
  python3 scripts/migrate_embed_to_bge_m3.py --dry-run  # report only
  python3 scripts/migrate_embed_to_bge_m3.py --self-test
  python3 scripts/migrate_embed_to_bge_m3.py --rollback PATH_TO_BACKUP

Safety:
  - DB is copied to ``memory.db.before-bge-m3-<timestamp>.bak`` (with the
    `-wal` / `-shm` sidecars if present) before any schema change. Failure
    to back up = abort.
  - vec0 virtual tables hardcode their dim and their aux ``_chunks`` /
    ``_rowids`` / ``_vector_chunks00`` tables cannot be ALTER-renamed, so the
    migration drops the shadow tables, drops the virtual table, and
    recreates it at the new dim before encoding rows in batches.
  - Each batch ``conn.commit()``s, so a mid-run crash leaves a smaller-than-
    intended index that ``--rollback PATH_TO_BAK`` restores cleanly.
  - On any exception (encode failure, row-count mismatch) the script raises
    and the caller restores from the backup.

Lineage:
  R10 of docs/B12_proactive_recall_plan_2026-05-18.md:
  "Embed dimension migration (Q1 critical): 384-dim → 1024-dim FULL reindex.
   Halfway-state NOT supported. Migration script must complete or roll back
   atomically. Snapshot DB before running."
"""
from __future__ import annotations

import argparse
import os
import shutil
import struct
import sqlite3
import sys
import time
from typing import Sequence


def _default_db_path() -> str:
    if sys.platform == 'darwin':
        return os.path.expanduser(
            '~/Library/Application Support/mcp-memory/sqlite_vec.db')
    if os.path.isdir(os.path.expanduser('~/AppData')):
        return os.path.expanduser('~/AppData/Local/mcp-memory/sqlite_vec.db')
    return os.path.expanduser('~/.local/share/mcp-memory/sqlite_vec.db')


def _open(db_path: str) -> sqlite3.Connection:
    import sqlite_vec  # type: ignore
    conn = sqlite3.connect(db_path, timeout=30)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def _probe_dim(conn: sqlite3.Connection) -> int | None:
    """Return the dimension of the first stored vector, or None if empty."""
    row = conn.execute(
        'SELECT content_embedding FROM memory_embeddings LIMIT 1'
    ).fetchone()
    if not row or not row[0]:
        return None
    return len(row[0]) // 4


def _candidate_memories(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Memories that should carry an embedding after the migration.

    Filters mirror the daemon's semantic_search WHERE clause so we don't
    waste compute encoding rows that are never surfaced anyway.
    """
    rows = conn.execute(
        """
        SELECT m.id, m.content
        FROM memories m
        WHERE m.deleted_at IS NULL
          AND (m.valid_until IS NULL OR m.valid_until > datetime('now'))
          AND m.memory_type NOT IN ('session_summary', 'progress')
          AND (m.tags IS NULL OR m.tags NOT LIKE '%session-summary%')
          AND m.content IS NOT NULL
          AND length(m.content) > 0
        ORDER BY m.id ASC
        """
    ).fetchall()
    return [(int(r[0]), r[1]) for r in rows]


def _backup(db_path: str) -> str:
    ts = time.strftime('%Y%m%dT%H%M%S')
    dest = f"{db_path}.before-bge-m3-{ts}.bak"
    shutil.copy2(db_path, dest)
    # Sidecar files used by SQLite WAL — copy if present.
    for suffix in ('-wal', '-shm'):
        side = db_path + suffix
        if os.path.exists(side):
            shutil.copy2(side, dest + suffix)
    return dest


def _load_model(model_name: str, backend: str):
    if backend == 'gguf':
        # Mirrors embed_daemon._load_gguf_backend.
        from llama_cpp import Llama  # type: ignore
        gguf_path = os.environ.get('B12_EMBED_GGUF_PATH', '').strip()
        if not gguf_path or not os.path.exists(gguf_path):
            raise FileNotFoundError(
                f'B12_EMBED_GGUF_PATH not set or missing: {gguf_path!r}')
        return Llama(model_path=gguf_path, embedding=True, n_ctx=8192, verbose=False)
    from sentence_transformers import SentenceTransformer  # type: ignore
    return SentenceTransformer(model_name, device='cpu')


def _encode(model, texts: Sequence[str], backend: str) -> list[list[float]]:
    import numpy as np
    if backend == 'gguf':
        out = []
        for t in texts:
            emb = model.create_embedding(t)
            v = np.asarray(emb['data'][0]['embedding'], dtype=np.float32)
            n = float(np.linalg.norm(v))
            if n > 0:
                v = v / n
            out.append(v.tolist())
        return out
    arr = model.encode(list(texts), normalize_embeddings=True,
                       convert_to_numpy=True)
    return [row.astype(np.float32).tolist() for row in arr]


def migrate(
    db_path: str,
    model_name: str = 'BAAI/bge-m3',
    backend: str = 'sentence-transformers',
    dry_run: bool = False,
    batch_size: int = 16,
) -> dict:
    """Run the destructive 384→1024 reindex.

    Returns a result dict with the backup path, row counts, and any errors.
    """
    if not os.path.exists(db_path):
        return {'ok': False, 'error': f'db_not_found: {db_path}'}

    conn = _open(db_path)
    try:
        old_dim = _probe_dim(conn)
        candidates = _candidate_memories(conn)
        total = len(candidates)

        result = {
            'ok': True,
            'db_path': db_path,
            'old_dim': old_dim,
            'planned_rows': total,
            'model_name': model_name,
            'backend': backend,
            'dry_run': dry_run,
        }

        if dry_run:
            return result

        # Real migration starts here. We still proceed when total == 0 so a
        # fresh / filtered-empty DB still gets its vec0 table rebuilt at the
        # new dim — otherwise subsequent inserts from the daemon hit the old
        # 384-dim schema and silently fail.
        backup_path = _backup(db_path)
        result['backup_path'] = backup_path

        model = _load_model(model_name, backend)
        # Probe new dim once.
        probe = _encode(model, ['dim_probe'], backend)
        new_dim = len(probe[0])
        result['new_dim'] = new_dim

        if old_dim is not None and old_dim == new_dim:
            # Idempotent path — same dim means a previous run already migrated.
            result['skipped'] = 'dim_match'
            return result

        # vec0 virtual tables hardcode their dim in CREATE and cannot be
        # ALTER-renamed (their aux _chunks/_rowids tables don't follow), so
        # we drop in place and rebuild. The on-disk .bak written above is
        # the rollback mechanism — `--rollback` restores it.
        cur = conn.cursor()
        try:
            cur.execute('DROP TABLE IF EXISTS memory_embeddings')
        except sqlite3.OperationalError:
            # Crashed previous run can leave orphan shadow tables that block
            # the DROP. Clean them up so the next CREATE succeeds.
            for shadow in ('memory_embeddings_chunks',
                           'memory_embeddings_rowids',
                           'memory_embeddings_vector_chunks00',
                           'memory_embeddings_info'):
                try:
                    cur.execute(f'DROP TABLE IF EXISTS {shadow}')
                except sqlite3.OperationalError:
                    pass
            cur.execute('DROP TABLE IF EXISTS memory_embeddings')
        cur.execute(
            f'CREATE VIRTUAL TABLE memory_embeddings USING vec0('
            f'content_embedding FLOAT[{new_dim}] distance_metric=cosine)'
        )
        conn.commit()

        inserted = 0
        for i in range(0, total, batch_size):
            chunk = candidates[i:i + batch_size]
            ids = [r[0] for r in chunk]
            contents = [r[1] for r in chunk]
            embs = _encode(model, contents, backend)
            for mem_id, vec in zip(ids, embs):
                packed = struct.pack(f'{new_dim}f', *vec)
                cur.execute(
                    'INSERT INTO memory_embeddings(rowid, content_embedding)'
                    ' VALUES (?, ?)',
                    (mem_id, packed),
                )
                inserted += 1
            conn.commit()

        result['encoded'] = inserted

        # Hard verify count matches plan — surface a clear error rather than
        # silently shipping a half-empty index. Backup is still on disk.
        if inserted != total:
            raise RuntimeError(
                f'row count mismatch: inserted={inserted} planned={total}'
                f' — restore from {backup_path} via --rollback')

        result['swapped'] = True
        return result
    finally:
        conn.close()


def rollback(backup_path: str, db_path: str | None = None) -> dict:
    """Restore the DB from a *.before-bge-m3-* backup."""
    if not os.path.exists(backup_path):
        return {'ok': False, 'error': f'backup_not_found: {backup_path}'}
    target = db_path or backup_path.split('.before-bge-m3-')[0]
    if not target:
        return {'ok': False, 'error': f'cannot infer db_path from {backup_path}'}
    # Move current to side, then restore.
    side = target + f'.rolled-back-{int(time.time())}'
    archived_sidecars = {}
    if os.path.exists(target):
        shutil.move(target, side)
    # Stash any live-DB sidecars to side.{suffix} and remove the originals so
    # the restored backup is not mixed with stale WAL state. Without this step
    # the post-rollback DB carries a `target-wal` that no longer matches
    # `target` and SQLite recovery silently corrupts the file.
    for suffix in ('-wal', '-shm'):
        tside = target + suffix
        if os.path.exists(tside):
            archived = side + suffix
            shutil.move(tside, archived)
            archived_sidecars[suffix] = archived
    shutil.copy2(backup_path, target)
    for suffix in ('-wal', '-shm'):
        sb = backup_path + suffix
        if os.path.exists(sb):
            shutil.copy2(sb, target + suffix)
    return {
        'ok': True,
        'restored_to': target,
        'pre_rollback_archived': side,
        'archived_sidecars': archived_sidecars,
    }


# ── Self-test ───────────────────────────────────────────────────
def _self_test() -> int:
    """Drive a synthetic 384→1024 migration on an in-memory DB."""
    import tempfile
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, 'test.db')
        # Build a tiny seed DB with the same schema as production.
        conn = _open(db_path)
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_hash TEXT UNIQUE NOT NULL,
                content TEXT NOT NULL,
                tags TEXT,
                memory_type TEXT,
                metadata TEXT,
                created_at REAL,
                updated_at REAL,
                deleted_at REAL DEFAULT NULL,
                valid_until TEXT DEFAULT NULL,
                strength REAL DEFAULT 1.0,
                last_accessed_at REAL DEFAULT NULL,
                difficulty REAL DEFAULT 5.0,
                due_date TEXT
            );
            CREATE VIRTUAL TABLE memory_embeddings USING vec0(
                content_embedding FLOAT[384] distance_metric=cosine
            );
            """
        )
        # Seed 4 rows: one with summary tag (should be skipped), one deleted,
        # one with no embedding, one normal.
        rows = [
            (1, 'h1', 'normal english memory about databases', 'proj:Test',
             'decision', '{}', 0, 0, None),
            (2, 'h2', 'turkish memory hakkında veritabanı tasarımı', 'proj:Test,session-summary',
             'session_summary', '{}', 0, 0, None),
            (3, 'h3', 'multilingual memory with embeddings', 'proj:Test',
             'fact', '{}', 0, 0, None),
            (4, 'h4', 'deleted memory — should not be encoded', 'proj:Test',
             'fact', '{}', 0, 0, 1.0),
        ]
        for r in rows:
            cur.execute(
                """INSERT INTO memories
                (id, content_hash, content, tags, memory_type, metadata,
                 created_at, updated_at, deleted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                r,
            )
        # Old-dim embeddings for rows 1 and 3.
        for rid in (1, 3):
            packed = struct.pack(f'{384}f', *[0.01 * (rid + i) for i in range(384)])
            cur.execute(
                'INSERT INTO memory_embeddings(rowid, content_embedding) VALUES (?, ?)',
                (rid, packed),
            )
        conn.commit()
        conn.close()

        # Probe pre-migration state.
        c = _open(db_path)
        pre_dim = _probe_dim(c)
        cands = _candidate_memories(c)
        c.close()
        if pre_dim != 384:
            failures.append(f'pre_dim expected 384 got {pre_dim}')
        if {row[0] for row in cands} != {1, 3}:
            failures.append(f'candidates expected (1,3) got {[r[0] for r in cands]}')

        # Use a tiny stub model so we don't pull BGE-M3 weights in CI.
        class _Stub:
            def encode(self, texts, normalize_embeddings=True,
                       convert_to_numpy=False):
                import numpy as np
                vecs = []
                for t in texts:
                    # Deterministic hash-derived 1024-dim vector.
                    h = abs(hash(t)) % (2**31)
                    arr = np.asarray(
                        [(((h + i) * 1103515245) % 2**31) / 2**31
                         for i in range(1024)], dtype=np.float32)
                    if normalize_embeddings:
                        n = float(np.linalg.norm(arr))
                        if n > 0:
                            arr = arr / n
                    vecs.append(arr)
                if convert_to_numpy:
                    return np.asarray(vecs, dtype=np.float32)
                return vecs

        # Run the migration with the stub model injected.
        global _load_model
        original_loader = _load_model
        _load_model = lambda name, backend: _Stub()  # type: ignore
        try:
            res = migrate(db_path, model_name='stub',
                          backend='sentence-transformers', batch_size=2)
        finally:
            _load_model = original_loader  # type: ignore

        if not res.get('ok'):
            failures.append(f'migrate returned not ok: {res}')
        if res.get('new_dim') != 1024:
            failures.append(f'new_dim expected 1024 got {res.get("new_dim")}')
        if res.get('encoded') != len(cands):
            failures.append(
                f'encoded expected {len(cands)} got {res.get("encoded")}')

        # Post-migration verification.
        c = _open(db_path)
        post_dim = _probe_dim(c)
        post_count = c.execute(
            'SELECT COUNT(*) FROM memory_embeddings').fetchone()[0]
        c.close()
        if post_dim != 1024:
            failures.append(f'post_dim expected 1024 got {post_dim}')
        if post_count != len(cands):
            failures.append(
                f'post row count expected {len(cands)} got {post_count}')

        # Verify backup was written.
        backups = [n for n in os.listdir(tmp) if '.before-bge-m3-' in n]
        if not backups:
            failures.append('no backup file written')

        # Idempotency: second run on the migrated DB must short-circuit.
        _load_model = lambda name, backend: _Stub()  # type: ignore
        try:
            second = migrate(db_path, model_name='stub',
                             backend='sentence-transformers')
        finally:
            _load_model = original_loader  # type: ignore
        if second.get('skipped') != 'dim_match':
            failures.append(
                f'second-run idempotency failed: {second}')

        # Empty-DB case (P1 from Codex review): zero candidate rows must
        # still rebuild the vec0 schema, else fresh DBs stay at the old dim.
        empty_db = os.path.join(tmp, 'empty.db')
        ec = _open(empty_db)
        ec.executescript("""
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_hash TEXT UNIQUE NOT NULL,
                content TEXT NOT NULL,
                tags TEXT, memory_type TEXT, metadata TEXT,
                created_at REAL, updated_at REAL, deleted_at REAL DEFAULT NULL,
                valid_until TEXT DEFAULT NULL,
                strength REAL DEFAULT 1.0,
                last_accessed_at REAL DEFAULT NULL,
                difficulty REAL DEFAULT 5.0,
                due_date TEXT
            );
            CREATE VIRTUAL TABLE memory_embeddings USING vec0(
                content_embedding FLOAT[384] distance_metric=cosine
            );
        """)
        ec.close()
        _load_model = lambda name, backend: _Stub()  # type: ignore
        try:
            empty_res = migrate(empty_db, model_name='stub',
                                backend='sentence-transformers')
        finally:
            _load_model = original_loader  # type: ignore
        if not empty_res.get('ok'):
            failures.append(f'empty migrate not ok: {empty_res}')
        if empty_res.get('new_dim') != 1024:
            failures.append(
                f'empty migrate new_dim={empty_res.get("new_dim")} (expected 1024)')
        # After migration on an empty DB, the schema should be 1024 even with
        # no rows to encode. Probe via direct schema introspection.
        ec2 = _open(empty_db)
        schema_row = ec2.execute(
            "SELECT sql FROM sqlite_master WHERE name='memory_embeddings'"
        ).fetchone()
        ec2.close()
        if not schema_row or 'FLOAT[1024]' not in schema_row[0]:
            failures.append(
                f'empty migrate did not rebuild schema: {schema_row}')

    if failures:
        print('SELF-TEST FAILED:')
        for f in failures:
            print(f'  - {f}')
        return 1
    print('SELF-TEST OK (5 cases: pre-state, candidate filter, migration, '
          'idempotency, empty-DB schema rebuild)')
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', default=_default_db_path(), help='path to sqlite_vec.db')
    p.add_argument('--model', default='BAAI/bge-m3',
                   help='sentence-transformers model id (or GGUF if backend=gguf)')
    p.add_argument('--backend', default=os.environ.get('B12_EMBED_BACKEND',
                                                       'sentence-transformers'),
                   choices=['sentence-transformers', 'gguf'])
    p.add_argument('--dry-run', action='store_true', help='report only, no DB writes')
    p.add_argument('--rollback', metavar='BACKUP_PATH', default=None,
                   help='restore the named backup file in place of the current DB')
    p.add_argument('--self-test', action='store_true', help='run self-test then exit')
    p.add_argument('--batch-size', type=int, default=16)
    args = p.parse_args(argv)

    if args.self_test:
        return _self_test()

    if args.rollback:
        res = rollback(args.rollback, args.db)
        print(res)
        return 0 if res.get('ok') else 1

    res = migrate(args.db, model_name=args.model, backend=args.backend,
                  dry_run=args.dry_run, batch_size=args.batch_size)
    print(res)
    return 0 if res.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
