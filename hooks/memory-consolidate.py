#!/usr/bin/env python3
"""
B12 Memory Consolidation Script (v1)

Analyzes the mcp-memory-service database for:
1. Near-duplicate memories (Jaccard word similarity)
2. Stale memories (old, low access)
3. Quality distribution
4. Cross-project topic index

Usage:
    python3 memory-consolidate.py              # Dry-run report only
    python3 memory-consolidate.py --auto       # Auto-merge duplicates & soft-delete stale
    python3 memory-consolidate.py --stats      # Quick stats only
    python3 memory-consolidate.py --index      # Generate cross-project index

The script works directly with the SQLite database.
"""

import sys
import os
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# Configuration
SIMILARITY_THRESHOLD = 0.65  # Jaccard similarity threshold for near-duplicates
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


def jaccard_similarity(text_a, text_b):
    """Word-level Jaccard similarity between two texts."""
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


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


def find_duplicates(memories):
    """Find near-duplicate pairs using Jaccard similarity.
    Only compares memories within the same project scope."""
    duplicates = []
    n = len(memories)
    for i in range(n):
        for j in range(i + 1, n):
            # Skip cross-project comparisons
            proj_a = get_project_tag(memories[i])
            proj_b = get_project_tag(memories[j])
            if proj_a and proj_b and proj_a != proj_b:
                continue

            sim = jaccard_similarity(memories[i]['content'], memories[j]['content'])
            if sim >= SIMILARITY_THRESHOLD:
                duplicates.append({
                    'mem_a': memories[i],
                    'mem_b': memories[j],
                    'similarity': sim
                })
    return duplicates


def find_stale(memories):
    """Find memories older than STALE_DAYS with no recent updates."""
    stale = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)
    for mem in memories:
        created = mem.get('created_at_iso', '')
        updated = mem.get('updated_at_iso', '')
        try:
            # Use updated_at if available, otherwise created_at
            date_str = updated or created
            if date_str:
                # Handle various ISO formats
                dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                if dt < cutoff:
                    stale.append({
                        'memory': mem,
                        'age_days': (datetime.now(timezone.utc) - dt).days
                    })
        except (ValueError, TypeError):
            continue
    return stale


def find_short(memories):
    """Find suspiciously short memories."""
    return [m for m in memories if len(m.get('content', '')) < MIN_CONTENT_LENGTH]


def merge_duplicates(conn, duplicates):
    """Merge duplicate pairs — keep newer, soft-delete older."""
    merged = 0
    for pair in duplicates:
        a = pair['mem_a']
        b = pair['mem_b']
        # Keep the one with more content (likely more detailed)
        if len(a['content']) >= len(b['content']):
            keep, remove = a, b
        else:
            keep, remove = b, a

        # Merge tags from removed into kept
        tags_keep = set((keep.get('tags') or '').split(','))
        tags_remove = set((remove.get('tags') or '').split(','))
        merged_tags = ','.join(sorted(t for t in tags_keep | tags_remove if t))

        # Update kept memory with merged tags
        conn.execute("""
            UPDATE memories
            SET tags = ?, updated_at = ?, updated_at_iso = ?
            WHERE id = ?
        """, (
            merged_tags,
            datetime.now(timezone.utc).timestamp(),
            datetime.now(timezone.utc).isoformat(),
            keep['id']
        ))

        # Soft-delete the duplicate
        conn.execute("""
            UPDATE memories
            SET deleted_at = ?
            WHERE id = ?
        """, (datetime.now(timezone.utc).timestamp(), remove['id']))

        merged += 1

    conn.commit()
    return merged


def soft_delete_stale(conn, stale_memories):
    """Soft-delete stale memories."""
    deleted = 0
    now = datetime.now(timezone.utc).timestamp()
    for item in stale_memories:
        mem = item['memory']
        conn.execute("""
            UPDATE memories SET deleted_at = ? WHERE id = ?
        """, (now, mem['id']))
        deleted += 1
    conn.commit()
    return deleted


def print_report(memories, duplicates, stale, short):
    """Print consolidation report."""
    print("=" * 60)
    print("  B12 Memory Consolidation Report")
    print("=" * 60)
    print()

    # Stats
    print(f"  Total active memories: {len(memories)}")
    types = defaultdict(int)
    for m in memories:
        types[m.get('memory_type', 'unknown')] += 1
    for t, count in sorted(types.items()):
        print(f"    {t}: {count}")
    print()

    # Tag distribution
    all_tags = defaultdict(int)
    for m in memories:
        for tag in (m.get('tags') or '').split(','):
            tag = tag.strip()
            if tag:
                all_tags[tag] += 1
    if all_tags:
        print(f"  Top tags:")
        for tag, count in sorted(all_tags.items(), key=lambda x: -x[1])[:10]:
            print(f"    {tag}: {count}")
        print()

    # Duplicates
    print(f"  Near-duplicates found: {len(duplicates)}")
    for pair in duplicates:
        sim = pair['similarity']
        a = pair['mem_a']
        b = pair['mem_b']
        print(f"    [{sim:.0%}] #{a['id']} vs #{b['id']}")
        print(f"      A: {a['content'][:80]}...")
        print(f"      B: {b['content'][:80]}...")
    print()

    # Stale
    print(f"  Stale memories (>{STALE_DAYS} days): {len(stale)}")
    for item in stale[:10]:
        m = item['memory']
        print(f"    #{m['id']} ({item['age_days']}d old): {m['content'][:60]}...")
    print()

    # Short
    print(f"  Suspiciously short (<{MIN_CONTENT_LENGTH} chars): {len(short)}")
    for m in short[:5]:
        print(f"    #{m['id']}: \"{m['content']}\"")
    print()

    print("=" * 60)
    if duplicates or stale:
        print("  Run with --auto to merge duplicates and remove stale entries")
    else:
        print("  Database looks clean!")
    print("=" * 60)


def build_cross_project_index(memories):
    """Build topic→projects mapping using scope-aware tag namespaces.

    Tag namespaces (v4):
      proj:<name>     — project-specific
      user:<setup>    — setup-specific (personal/work)
      user:universal  — applies everywhere
      user:pref       — user preferences
    """
    # topic → {project_name: count}
    project_topics = defaultdict(lambda: defaultdict(int))

    for mem in memories:
        tags = [t.strip() for t in (mem.get('tags') or '').split(',') if t.strip()]
        content = mem.get('content', '')

        # Extract project names from proj: tags
        projects = set()
        for tag in tags:
            if tag.startswith('proj:'):
                projects.add(tag[5:])  # strip "proj:" prefix

        # If no proj: tag, try metadata
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

        # Skip memories with no project association
        if not projects:
            continue

        # Extract topics: non-namespace tags + content keywords
        topics = set()
        for tag in tags:
            if not tag.startswith(('proj:', 'user:')):
                topics.add(tag)

        # Content-based topic extraction
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

    # Build the index
    index = {}
    for topic, projects_map in sorted(project_topics.items()):
        if len(projects_map) > 0:
            index[topic] = dict(sorted(projects_map.items(), key=lambda x: -x[1]))

    return index


def generate_cross_project_index(memories):
    """Generate and save cross-project-index.json."""
    index = build_cross_project_index(memories)
    index_dir = os.path.expanduser("~/.claude/memory-summaries")
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

    duplicates = find_duplicates(memories)
    stale = find_stale(memories)
    short = find_short(memories)

    print_report(memories, duplicates, stale, short)

    # Always regenerate cross-project index during full run
    generate_cross_project_index(memories)

    if auto_mode and (duplicates or stale):
        print()
        print("  AUTO MODE — Executing changes...")
        if duplicates:
            merged = merge_duplicates(conn, duplicates)
            print(f"  Merged {merged} duplicate pairs")
        if stale:
            deleted = soft_delete_stale(conn, stale)
            print(f"  Soft-deleted {deleted} stale memories")
        print("  Done!")

    conn.close()


if __name__ == '__main__':
    main()
