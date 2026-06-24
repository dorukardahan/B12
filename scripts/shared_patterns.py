"""Shared patterns and utilities for B12 hooks and scripts.

DRY extraction — regexes, paths, and platform detection used across
hooks/memory-session-end.sh, hooks/memory-precompact.sh, and all scripts.

English + Turkish contextual patterns (v4 format).
"""
import hashlib
import json
import os
import re
import sqlite3
import sys


# ── Platform-aware database path ──────────────────────────────────

def get_db_path() -> str:
    """Return the B12 SQLite database path for the current platform.

    macOS:   ~/Library/Application Support/mcp-memory/sqlite_vec.db
    Linux:   ~/.local/share/mcp-memory/sqlite_vec.db
    Windows: ~/AppData/Local/mcp-memory/sqlite_vec.db (or WSL Linux path)
    """
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support",
                            "mcp-memory", "sqlite_vec.db")
    elif sys.platform == "win32":
        return os.path.join(home, "AppData", "Local",
                            "mcp-memory", "sqlite_vec.db")
    else:
        # Linux, WSL, and other Unix-like
        return os.path.join(home, ".local", "share",
                            "mcp-memory", "sqlite_vec.db")


DB_PATH = get_db_path()


def content_hash(text: str) -> str:
    """Canonical SHA-256 content hash for B12 memories.

    Normalizes with strip().lower() before hashing.
    ALL code that computes content hashes MUST use this function.
    """
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def exact_tag_predicate(column: str = "tags") -> str:
    normalized = f"replace(replace(COALESCE({column}, ''), ', ', ','), ' ,', ',')"
    return f"(',' || {normalized} || ',') LIKE ? ESCAPE '\\'"


def exact_tag_param(tag: str) -> str:
    return f"%,{escape_like(tag.strip())},%"


def try_load_sqlite_vec(conn) -> tuple[bool, str | None]:
    """Best-effort sqlite-vec loader for read-only diagnostics."""
    extension_enabled = False
    try:
        import sqlite_vec  # type: ignore

        conn.enable_load_extension(True)
        extension_enabled = True
        sqlite_vec.load(conn)
        return True, None
    except ImportError:
        return False, "sqlite_vec is not importable"
    except sqlite3.Error as exc:
        return False, f"sqlite_vec could not be loaded: {exc}"
    finally:
        if extension_enabled:
            try:
                conn.enable_load_extension(False)
            except sqlite3.Error:
                pass


def count_active_embeddings(conn) -> tuple[int | None, str | None]:
    """Count active memory embeddings, returning None when vec0 is unavailable."""
    sql = """
        SELECT COUNT(*)
        FROM memories m
        JOIN memory_embeddings e ON e.rowid = m.id
        WHERE m.deleted_at IS NULL
    """
    try:
        return int(conn.execute(sql).fetchone()[0]), None
    except sqlite3.OperationalError as first_error:
        exists = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE name = 'memory_embeddings' AND type IN ('table', 'virtual table')
            LIMIT 1
            """
        ).fetchone()
        if not exists:
            return 0, None

        loaded, load_error = try_load_sqlite_vec(conn)
        if loaded:
            try:
                return int(conn.execute(sql).fetchone()[0]), None
            except sqlite3.OperationalError as retry_error:
                return None, f"embedding coverage unavailable: {retry_error}"
        return None, f"embedding coverage unavailable: {load_error or first_error}"


def validate_metadata(value) -> str:
    """Ensure metadata is a valid JSON string. Never raises.

    Accepts: dict → json.dumps it
    Accepts: valid JSON string → passes through
    Accepts: None/empty → returns '{}'
    Accepts: legacy f-string ("type:x, importance:0.6") → parses and converts

    ALL code that writes to the metadata column MUST use this function.
    """
    if value is None:
        return "{}"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return "{}"
        # Already valid JSON?
        try:
            json.loads(s)
            return s
        except (json.JSONDecodeError, ValueError):
            pass
        # Legacy f-string format: "type:progress, importance:0.6, key:value"
        result = {}
        for part in s.split(","):
            part = part.strip()
            if ":" not in part:
                continue
            key, _, val = part.partition(":")
            key = key.strip()
            val = val.strip()
            if key == "importance":
                key = "importance_score"
            try:
                val = float(val)
                if val == int(val):
                    val = int(val)
            except (ValueError, TypeError):
                pass
            result[key] = val
        return json.dumps(result, ensure_ascii=False) if result else "{}"
    # Unknown type — serialize best-effort
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


DECISION_RE = re.compile(
    r'(?i)(?:'
    # English patterns
    r'(?:decided|chose|going with|selected|opted for|switched to|went with)\s+.{5,}'
    r'|(?:will use|using|let.?s use|we.?ll use)\s+\S+\s+(?:instead of|rather than|for|because)\s+'
    r'|(?:the (?:approach|solution|decision|plan) is to)\s+'
    r'|(?:switching from|replacing|migrating from)\s+\S+\s+(?:to|with)\s+'
    # Turkish patterns
    r'|(?:karar verdik?|seçtik?|tercih ettik?|bununla gid|bunu kullan)\s*.{5,}'
    r'|(?:yerine|değil de|bunun yerine)\s+\S+\s+.{3,}'
    r'|(?:planımız|yaklaşımımız|çözüm(?:ümüz)?)\s+.{5,}'
    r')'
)

ERROR_RE = re.compile(
    r'(?i)(?:'
    # English patterns
    r'(?:fixed|resolved|solved|workaround for)\s+.{5,}'
    r'|(?:the fix|the solution|root cause)\s*(?:is|was|:)\s+'
    r'|(?:error|bug|issue)\s+.{0,40}(?:was caused by|because|due to|fixed by)'
    r'|(?:had to|needed to)\s+.{3,40}(?:because|due to|since)\s+.{3,}(?:error|bug|fail|broke|crash)'
    # Turkish patterns
    r'|(?:düzelttik?|çözdük?|giderdik?|fix.?ledik?)\s+.{5,}'
    r'|(?:hata|bug|sorun)\s+.{0,40}(?:sebebi|nedeni|çözümü|düzeltmesi)'
    r'|(?:sorun şuydu|hata şuydu|sebebi şuydu)\s*(?::)?\s+'
    r')'
)

LEARNING_RE = re.compile(
    r'(?i)(?:'
    # English patterns
    r'(?:turns out|TIL|important to note|gotcha|pitfall|caveat|note:)\s*(?::|that|,)?\s+'
    r'|(?:learned|discovered|realized|found out)\s+that\s+'
    r'|(?:the (?:trick|key|insight|important thing) (?:is|was))\s+'
    r'|(?:remember|important):\s+'
    r'|(?:pro.?tip|heads.?up|watch out|be careful|don.?t forget)\s*(?::|,)\s+'
    # Turkish patterns
    r'|(?:meğer|meğerse|anlaşılan)\s+.{5,}'
    r'|(?:öğrendik?|fark ettik?|keşfettik?)\s+.{3,}'
    r'|(?:dikkat|önemli|unutma)(?::|\s).{5,}'
    r')'
)

PREFERENCE_RE = re.compile(
    r'(?i)(?:'
    # English patterns
    r'(?:user\s+(?:prefers?|wants?|asked for|(?:does ?\x27?n.?t|never)\s+(?:want|like|use)))'
    r'|(?:always use|never use|convention is|style preference|workflow:)'
    r'|\[user\]\s+'
    # Turkish patterns
    r'|(?:kullanıcı\s+(?:tercih|istiyor|istemiyor|istemez))'
    r'|(?:her zaman|hiçbir zaman|asla|daima)\s+(?:kullan|yap|kullanma|yapma)'
    r')'
)

# ── Extended patterns (v4.1) ─────────────────────────────────

TOOL_PREF_RE = re.compile(
    r'(?i)(?:'
    # English patterns
    r'(?:always use|prefer\s+\S+\s+over)\s+.{5,}'
    r'|(?:works?\s+better\s+than)\s+.{5,}'
    r'|(?:switched to|switching to)\s+\S+\s+(?:for|because)\s+.{5,}'
    r'|(?:don.?t use|avoid using|stop using)\s+\S+\s+(?:because|for|since)\s+.{3,}'
    r'|(?:prefer\s+\S+\s+for)\s+.{5,}'
    # Turkish patterns
    r'|(?:hep\s+\S+\s+kullan)\s*.{5,}'
    r'|(?:\S+.?[ıi]\s+tercih\s+et)\s*.{5,}'
    r'|(?:daha\s+iyi\s+çalışıyor)\s*.{3,}'
    r'|(?:\S+\s+kullanma\s+çünkü)\s+.{5,}'
    r')'
)

ARCH_RE = re.compile(
    r'(?i)(?:'
    # English patterns
    r'(?:the\s+architecture\s+is)\s+.{5,}'
    r'|(?:we\s+structured\s+it\s+as)\s+.{5,}'
    r'|(?:the\s+pattern\s+we\s+use\s+is)\s+.{5,}'
    r'|(?:built\s+on\s+top\s+of)\s+.{5,}'
    r'|(?:the\s+design\s+is)\s+.{5,}'
    r'|(?:using\s+\S+\s+(?:pattern|approach|architecture))\s+.{3,}'
    r'|(?:the\s+(?:system|service|module|component)\s+(?:is structured|is designed|follows))\s+.{5,}'
    # Turkish patterns
    r'|(?:mimari(?:si|miz)?\s+(?:şöyle|böyle|olarak))\s*.{5,}'
    r'|(?:yapı(?:sı|mız)?\s+(?:şöyle|böyle|olarak))\s*.{5,}'
    r'|(?:tasarım(?:ı|ımız)?\s+(?:şöyle|böyle|olarak))\s*.{5,}'
    r'|(?:bunun\s+üzerine\s+kurduk)\s*.{5,}'
    r'|(?:yaklaşım\s+olarak)\s+.{5,}'
    r')'
)

WORKFLOW_RE = re.compile(
    r'(?i)(?:'
    # English patterns
    r'(?:the\s+workflow\s+is)\s*(?::)?\s+.{5,}'
    r'|(?:the\s+process\s+is)\s*(?::)?\s+.{5,}'
    r'|(?:first\s+\S+\s+then)\s+.{5,}'
    r'|(?:deploy\s+with)\s+.{5,}'
    r'|(?:run\s+\S+\s+before)\s+.{5,}'
    r'|(?:the\s+pipeline\s+is)\s*(?::)?\s+.{5,}'
    r'|(?:the\s+(?:build|release|test|ci)\s+(?:process|pipeline|flow)\s+(?:is|goes|works))\s+.{5,}'
    r'|(?:step\s+\d+\s*(?::|is|,))\s+.{5,}'
    # Turkish patterns
    r'|(?:iş\s*akışı(?:mız)?\s*(?::|şöyle|böyle))\s*.{5,}'
    r'|(?:süreç\s*(?::|şöyle|böyle))\s*.{5,}'
    r'|(?:önce\s+\S+\s+sonra)\s+.{5,}'
    r'|(?:deploy\s+için)\s+.{5,}'
    r'|(?:sırasıyla)\s+.{5,}'
    r')'
)

FILE_CONV_RE = re.compile(
    r'(?i)(?:'
    # English patterns
    r'(?:files?\s+go\s+in)\s+.{5,}'
    r'|(?:naming\s+convention\s+(?:is|for))\s+.{5,}'
    r'|(?:put\s+\S+\s+in\s+(?:the\s+)?\S+\s+directory)\s*.{3,}'
    r'|(?:file\s+structure\s+(?:is|looks))\s+.{5,}'
    r'|(?:organized\s+as)\s+.{5,}'
    r'|(?:(?:directory|folder)\s+(?:structure|layout|convention)\s+(?:is|for))\s+.{5,}'
    # Turkish patterns
    r'|(?:dosyalar\s+\S+.?[ea]\s+konur)\s*.{3,}'
    r'|(?:isimlendirme\s+kuralı)\s*.{5,}'
    r'|(?:dosya\s+yapısı)\s*.{5,}'
    r'|(?:düzen\s+olarak)\s+.{5,}'
    r')'
)

# ── v12 patterns ─────────────────────────────────────────────

CORRECTION_RE = re.compile(
    r'(?i)(?:'
    r'(?:not\s+.{3,30}(?:,\s*|\s+but\s+)(?:it.?s|actually)\s+.{3,30})'       # not X, actually Y
    r'|(?:(?:wrong|incorrect)\s+.{0,20}(?:should be|is actually)\s+.{3,30})'  # X wrong, should be Y
    r'|(?:changed?\s+(?:from|my)\s+.{3,30}\s+to\s+.{3,30})'                  # changed from X to Y
    r'|(?:(?:yanlış|hatalı)\s+.{3,30}(?:aslında|artık|olarak)\s+.{3,30})'     # Turkish
    r'|(?:(?:değil)\s+.{3,30}(?:artık|şimdi)\s+.{3,30})'                      # X değil, Y
    r')'
)

INFRA_RE = re.compile(
    r'(?i)(?:'
    r'(?:(?:server|host|ip|vps|ssh)\s+.{0,30}(?:\d{1,3}\.){3}\d{1,3})'   # IP with context
    r'|(?:ssh\s+[-\w]+@[\w.-]+)'                                           # SSH connections
    r'|(?:(?:version|sürüm)\s+.{0,10}v?\d+\.\d+)'                         # Version strings
    r'|(?:port\s+\d{2,5})'                                                 # Port numbers
    r')'
)

CONTENT_RE = re.compile(
    r'(?i)(?:'
    r'(?:(?:blog|article)\s+.{0,30}(?:published|approved|rejected|hazır|onaylandı))'
    r'|(?:(?:editorial|content)\s+decision\s*:\s+.{5,})'
    r'|(?:(?:do not|never|asla)\s+(?:write|post|publish|mention)\s+.{5,})'
    r'|(?:(?:review|feedback)\s*:\s+.{5,})'
    r')'
)


# ═══════════════════════════════════════════════════════════════
# Layer 0 + Layer 1: Pre-regex filters (v12.2)
# These run BEFORE regex patterns to eliminate false positives
# and auto-classify content that has explicit type markers.
# ═══════════════════════════════════════════════════════════════

# ── Layer 0: Session summary filter ─────────────────────────
_SUMMARY_MARKERS = (
    '# Session Summary',
    '## Decisions Made',
    '## Errors & Fixes',
    '## Key Learnings',
    '## User Preferences',
    '## What Was Done',
    '## Sprint Handoff',
    '## User Requests',
    '## Files Modified',
)


def summary_filter(text: str) -> bool:
    """Return True if text is a session summary recitation (skip regex).

    Session summaries contain embedded decision/error keywords that
    trigger false positives. This filter detects them by structural
    markers (2+ matches = summary content).
    """
    if not text:
        return False
    return sum(1 for m in _SUMMARY_MARKERS if m in text) >= 2


# ── Signal extraction helpers (used by lightweight platform hooks) ──
# Mirror the SessionEnd extraction semantics (hooks/memory-session-end.sh):
# a 250-char context window starting 50 chars before each match, after the
# session-summary recitation filter. Returns deduped windows. Kept here so
# every platform's hooks share ONE extraction definition rather than copying
# the regex-application logic.

def _extract_windows(pattern, text, *, window=250, lookback=50, limit=20):
    """Return up to `limit` deduped context windows around `pattern` matches in
    `text`. Empty list when `text` is a session-summary recitation."""
    if not text or summary_filter(text):
        return []
    out = []
    seen = set()
    for m in pattern.finditer(text):
        start = max(0, m.start() - lookback)
        frag = text[start:start + window].strip()
        key = frag.lower()
        if frag and key not in seen:
            seen.add(key)
            out.append(frag)
            if len(out) >= limit:
                break
    return out


def extract_decisions(text):
    """Decision-shaped statements (EN + TR) — `decided`, `chose`, `karar verdik`…"""
    return _extract_windows(DECISION_RE, text)


def extract_gotchas(text):
    """Error/fix/gotcha statements (EN + TR) — `fixed`, `root cause`, `düzelttik`…"""
    return _extract_windows(ERROR_RE, text)


def extract_learnings(text):
    """Learnings/insights (EN + TR) — `TIL`, `turns out`, `meğer`, `öğrendik`…"""
    return _extract_windows(LEARNING_RE, text)


def extract_preferences(text):
    """User preferences (EN + TR) — `user prefers`, `always use`, `kullanıcı tercih`…"""
    return _extract_windows(PREFERENCE_RE, text)


# ── Layer 1: [Label] prefix auto-classification ─────────────
_PREFIX_RE = re.compile(r'^\[([^\]]{2,30})\]')

_PREFIX_MAP = {
    'decision': 'decision',
    'error fix': 'error_fix',
    'error': 'error_fix',
    'gotcha': 'learning',
    'learning': 'learning',
    'preference': 'preference',
    'progress': 'observation',
    'observation': 'observation',
    'architecture': 'knowledge',
    'pattern': 'knowledge',
    'reference': 'knowledge',
    'review': 'knowledge',
    'note': 'knowledge',
    'handoff': 'session_summary',
    'audit': 'knowledge',
    'test': 'knowledge',
}


def classify_by_prefix(content: str):
    """Auto-classify memory by [Label] prefix tag.

    Returns {"type": str, "confidence": 1.0} if prefix found,
    or None if no recognized prefix.

    Prefix tags like [Decision], [Error Fix], [Learning] are
    deterministic type markers — no regex needed, ~94% precision.
    """
    if not content:
        return None
    m = _PREFIX_RE.match(content.strip())
    if not m:
        return None
    tag = m.group(1).strip().lower()
    for key, typ in _PREFIX_MAP.items():
        if key in tag:
            return {"type": typ, "confidence": 1.0}
    return None


# ── Fragment detection (write-time + NLI pre-filter) ─────────
# Literal stub set: short utterances we never want to store, store-merge,
# or run NLI against. Turkish + English. Case-insensitive matching.
_FRAGMENT_STUBS = {
    'ok.', 'okay.', 'evet.', 'tamam.', 'yes.', 'no.', 'hayır.', 'şu.',
    'shot.', 'cool.', 'sure.', 'fine.', 'done.',
}


def is_fragment(content: str) -> bool:
    """Return True for short/incomplete utterances we shouldn't process.

    Used by both the write-time gate (PR4) and the NLI surface filter (PR2)
    to skip candidates that would produce noisy results. Rules:

      - Literal stub set ({shot., ok., evet., tamam., ...})
      - Ends with `:` or `...` or unbalanced quote
      - Starts lowercase + no recognized `[Label]` prefix
      - <50 chars AND no recognized `[Label]` prefix

    Turkish-aware: uses an explicit "is uppercase letter" test rather than
    `str.islower()` so `ş`, `ç`, `ı`, `ö`, `ü`, `ğ` are not mis-classified
    (Python treats these as lowercase regardless of the local convention).
    """
    if not content:
        return True
    stripped = content.strip()
    if not stripped:
        return True

    # Codex review PR #57 round 5 P2: use casefold() for Turkish-safe
    # case-insensitive match. `.lower()` doesn't handle `İ → i` / `I → ı`
    # so `EVET.` / `TAMAM.` (caps lock user input) wouldn't match.
    if stripped.casefold() in {s.casefold() for s in _FRAGMENT_STUBS}:
        return True
    if stripped.endswith((':', '...')):
        return True
    # Codex review PR #57 P1: only check double quotes here. Apostrophes
    # are routine in normal sentences ("It's working with PostgreSQL")
    # and treating them as unbalanced would mark a large class of valid
    # English input as fragments — silently disabling NLI contradiction
    # detection on most real content.
    if stripped.count('"') % 2 == 1:
        return True

    has_prefix = _PREFIX_RE.match(stripped) is not None
    if has_prefix:
        return False

    # Heuristic: first letter must be an uppercase ASCII letter OR Turkish
    # uppercase (İŞÇÖÜĞ). Anything else → fragment when no [Label] prefix.
    first = stripped[0]
    is_upper = first.isupper() or first in 'İŞÇÖÜĞ'
    if not is_upper:
        return True
    if len(stripped) < 50:
        return True
    return False


# ── v12.1 patterns — implicit decisions, reasons, blockers ───

IMPLICIT_DECISION_RE = re.compile(
    r'(?i)(?:'
    # English implicit decisions
    r'(?:let.?s\s+(?:go\s+with|use|try|pick|choose|stick with)\s+.{3,80})'
    r'|(?:going\s+to\s+use\s+.{3,80})'
    r'|(?:plan\s+is\s+to\s+.{3,80})'
    r'|(?:(?:I|we).?(?:ll|will)\s+(?:go with|use|try|pick)\s+.{3,80})'
    r'|(?:(?:better|best)\s+to\s+(?:use|go with|try)\s+.{3,80})'
    # Turkish implicit decisions
    r'|(?:(?:yapacağız|kullanalım|geçelim|deneyelim|seçelim)\s+.{3,80})'
    r'|(?:(?:bununla|bunu|şunu)\s+(?:gidelim|deneyelim|kullanalım)\s*.{0,80})'
    r'|(?:(?:en iyisi|daha iyi)\s+.{3,80})'
    r')'
)

REASON_RE = re.compile(
    r'(?i)(?:'
    # English reasoning
    r'(?:because\s+.{10,200})'
    r'|(?:since\s+.{10,200})'
    r'|(?:the\s+reason\s+(?:is|was|being)\s+.{10,200})'
    r'|(?:due\s+to\s+.{10,200})'
    r'|(?:this\s+is\s+(?:because|since|due to)\s+.{10,200})'
    # Turkish reasoning
    r'|(?:çünkü\s+.{10,200})'
    r'|(?:(?:nedeni|sebebi|sebebiyle)\s+.{10,200})'
    r'|(?:(?:bunun\s+nedeni|bunun\s+sebebi)\s+.{10,200})'
    r')'
)

BLOCKER_RE = re.compile(
    r'(?i)(?:'
    # English blockers
    r'(?:blocked\s+by\s+.{5,150})'
    r'|(?:waiting\s+for\s+.{5,150})'
    r'|(?:can.?t\s+proceed\s+.{5,150})'
    r'|(?:stuck\s+on\s+.{5,150})'
    r'|(?:(?:depends|dependent)\s+on\s+.{5,150})'
    r'|(?:need\s+to\s+(?:wait|resolve|fix)\s+.{5,150})'
    # Turkish blockers
    r'|(?:(?:bekliyor|takıldık|tıkandık)\s+.{5,150})'
    r'|(?:(?:buna\s+bağlı|bundan\s+önce)\s+.{5,150})'
    r'|(?:(?:çözmemiz|düzeltmemiz)\s+(?:lazım|gerek)\s*.{0,150})'
    r')'
)


# ── Process memory self-guard ─────────────────────────────────────
# Used by the long-lived daemons (embed_daemon, b12_mcp_daemon) so a runaway
# allocation logs and exits cleanly instead of starving the host — the failure
# mode behind the 2026-06 OOM machine panic. getrusage is a pure read, so this
# works everywhere (unlike RLIMIT_AS, which macOS refuses to let a process set).

def rss_bytes() -> int:
    """Peak resident set size of THIS process, in bytes.

    Built on getrusage(RUSAGE_SELF).ru_maxrss, normalized across platforms:
    macOS/BSD report the value in bytes, Linux in kibibytes. It is a
    high-water mark (never decreases), so a guard built on it trips on the
    PEAK — a deliberately conservative signal for a long-lived process.
    """
    import resource
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform == "darwin" else rss * 1024


def rss_exceeds(ceiling_mb: int) -> int:
    """Return current peak RSS in MB when it exceeds ceiling_mb, else 0.

    ceiling_mb <= 0 disables the check. Cheap enough to call on every served
    request or timer tick. Never raises — a measurement failure returns 0
    (fail-open: a guard that can't measure must not kill a healthy daemon).
    """
    if ceiling_mb <= 0:
        return 0
    try:
        mb = rss_bytes() // (1024 * 1024)
    except Exception:
        return 0
    return int(mb) if mb > ceiling_mb else 0
