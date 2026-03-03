#!/usr/bin/env python3
"""
B12 Memory Consolidation Script (v2 — HDBSCAN-based)

Uses the consolidation engine for semantic clustering, deduplication,
merging, and contradiction detection. Replaces the old Jaccard-based logic.

Usage:
    python3 memory-consolidate.py              # Dry-run report only
    python3 memory-consolidate.py --auto       # Apply consolidation changes
    python3 memory-consolidate.py --stats      # Quick stats only
    python3 memory-consolidate.py --index      # Generate cross-project index

The script works directly with the SQLite database.
"""

import sys
import os
import json
import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

# Configuration
STALE_DAYS = 90              # Memories older than this with no updates are flagged
MIN_CONTENT_LENGTH = 20      # Minimum content length — shorter is suspect
import sys as _sys
_home = os.path.expanduser("~")
if _sys.platform == "darwin":
    DB_PATH = os.path.join(_home, "Library", "Application Support", "mcp-memory", "sqlite_vec.db")
elif _sys.platform == "win32":
    DB_PATH = os.path.join(_home, "AppData", "Local", "mcp-memory", "sqlite_vec.db")
else:
    DB_PATH = os.path.join(_home, ".local", "share", "mcp-memory", "sqlite_vec.db")

# Import consolidation engine — when deployed, scripts/ is at ~/.B12/hooks/scripts/
_hook_dir = os.path.dirname(os.path.abspath(__file__))
_scripts_dir = os.path.join(_hook_dir, "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
# Also try the source repo scripts/ directory
_repo_scripts_dir = os.path.join(os.path.dirname(_hook_dir), "scripts")
if _repo_scripts_dir not in sys.path:
    sys.path.insert(0, _repo_scripts_dir)

try:
    from consolidation_engine import consolidate
    _HAS_ENGINE = True
except ImportError:
    _HAS_ENGINE = False


def get_all_memories(conn):
    """Fetch all active (non-deleted) memories."""
    cursor = conn.execute("""
        SELECT id, content_hash, content, tags, memory_type,
               metadata, created_at_iso, updated_at_iso
        FROM memories
        WHERE deleted_at IS NULL
        ORDER BY id
    """)
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def get_project_tag(mem):
    """Extract project name from proj: tag or metadata."""
    tags = [t.strip() for t in (mem.get('tags') or '').split(',') if t.strip()]
    for tag in tags:
        if tag.startswith('proj:'):
            return tag[5:]
    # Fallback to metadata
    try:
        meta = mem.get('metadata', '{}')
        if isinstance(meta, str):
            meta = json.loads(meta)
        return meta.get('project', '')
    except (json.JSONDecodeError, TypeError):
        return ''


def build_cross_project_index(memories):
    """Build topic->projects mapping using scope-aware tag namespaces."""
    project_topics = defaultdict(lambda: defaultdict(int))

    for mem in memories:
        tags = [t.strip() for t in (mem.get('tags') or '').split(',') if t.strip()]
        content = mem.get('content', '')

        projects = set()
        for tag in tags:
            if tag.startswith('proj:'):
                projects.add(tag[5:])

        if not projects:
            try:
                meta = mem.get('metadata', '{}')
                if isinstance(meta, str):
                    meta = json.loads(meta)
                proj = meta.get('project', '')
                if proj and proj != 'global':
                    projects.add(proj)
            except (json.JSONDecodeError, TypeError):
                pass

        if not projects:
            continue

        topics = set()
        for tag in tags:
            if not tag.startswith(('proj:', 'user:')):
                topics.add(tag)

        snippet = content[:300].lower()
        for keyword in ['docker', 'git', 'ssh', 'python', 'typescript', 'react',
                       'api', 'database', 'sqlite', 'mcp', 'hook', 'memory',
                       'deploy', 'test', 'auth', 'ci/cd', 'nginx', 'redis',
                       'plugin', 'config', 'migration', 'performance', 'security']:
            if keyword in snippet:
                topics.add(keyword)

        for project in projects:
            for topic in topics:
                project_topics[topic][project] += 1

    index = {}
    for topic, projects_map in sorted(project_topics.items()):
        if len(projects_map) > 0:
            index[topic] = dict(sorted(projects_map.items(), key=lambda x: -x[1]))

    return index


def generate_cross_project_index(memories):
    """Generate and save cross-project-index.json."""
    index = build_cross_project_index(memories)
    index_dir = os.path.join(os.environ.get('B12_DATA_DIR', os.path.expanduser('~/.B12')), 'memory-summaries')
    os.makedirs(index_dir, exist_ok=True)
    index_path = os.path.join(index_dir, "cross-project-index.json")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_memories": len(memories),
        "topic_count": len(index),
        "topics": index
    }

    with open(index_path, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"Cross-project index saved: {index_path}")
    print(f"  Topics indexed: {len(index)}")
    for topic, projects in list(index.items())[:15]:
        projects_str = ', '.join(f"{p}({c})" for p, c in projects.items())
        print(f"    {topic}: {projects_str}")


def print_stats(memories):
    """Quick stats output."""
    types = defaultdict(int)
    for m in memories:
        types[m.get('memory_type', 'unknown')] += 1

    total_chars = sum(len(m.get('content', '')) for m in memories)

    print(f"Memories: {len(memories)} | Types: {dict(types)} | Total chars: {total_chars:,}")

    if memories:
        oldest = min(m.get('created_at_iso', 'z') for m in memories)
        newest = max(m.get('created_at_iso', '') for m in memories)
        print(f"Oldest: {oldest[:10]} | Newest: {newest[:10]}")


def main():
    if not os.path.exists(DB_PATH):
        print(f"Database not found: {DB_PATH}")
        sys.exit(1)

    auto_mode = '--auto' in sys.argv
    stats_only = '--stats' in sys.argv
    index_only = '--index' in sys.argv

    conn = sqlite3.connect(DB_PATH)
    memories = get_all_memories(conn)

    if stats_only:
        print_stats(memories)
        conn.close()
        return

    if index_only:
        generate_cross_project_index(memories)
        conn.close()
        return

    conn.close()

    # Use the consolidation engine for clustering/dedup/merge
    if _HAS_ENGINE:
        dry_run = not auto_mode
        try:
            result = consolidate(db_path=DB_PATH, dry_run=dry_run)
        except Exception as e:
            print(f"Consolidation engine error: {e}")
            sys.exit(1)

        print("=" * 60)
        print("  B12 Memory Consolidation Report")
        if dry_run:
            print("  (DRY RUN — no changes made)")
        print("=" * 60)
        print()
        print(f"  Total active memories: {len(memories)}")
        print(f"  Memories processed:    {result.memories_processed}")
        print(f"  Clusters found:        {result.clusters_found}")
        print(f"  Deduplicated:          {result.memories_deduplicated}")
        print(f"  Merged:                {result.memories_merged}")
        print(f"  Contradictions flagged: {result.contradictions_flagged}")
        print()

        if result.dry_run_report:
            print("  Cluster details:")
            for entry in result.dry_run_report:
                action = entry['type'].upper()
                ids = entry['ids']
                sim = entry.get('similarity', 0)
                nli = entry.get('nli_score', '')
                nli_str = f" NLI:{nli}" if nli else ''
                print(f"    [{action}] #{ids[0]} <-> #{ids[1]}  "
                      f"(cosine: {sim:.3f}{nli_str})")
                for snippet in entry.get('snippets', []):
                    print(f"      {snippet}")
            print()

        print("=" * 60)
        if dry_run and (result.memories_deduplicated or result.memories_merged):
            print("  Run with --auto to apply these changes")
        elif not dry_run and (result.memories_deduplicated or result.memories_merged):
            print("  Changes applied to database.")
        else:
            print("  Database looks clean!")
        print("=" * 60)
    else:
        print("Error: consolidation_engine not available.")
        print("Ensure scripts/consolidation_engine.py is deployed to ~/.B12/hooks/scripts/")
        sys.exit(1)

    # Always regenerate cross-project index during full run
    conn = sqlite3.connect(DB_PATH)
    memories = get_all_memories(conn)
    conn.close()
    generate_cross_project_index(memories)


if __name__ == '__main__':
    main()
