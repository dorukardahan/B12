"""Shared patterns and utilities for B12 hooks and scripts.

DRY extraction — regexes, paths, and platform detection used across
hooks/memory-session-end.sh, hooks/memory-precompact.sh, and all scripts.

English + Turkish contextual patterns (v4 format).
"""
import hashlib
import json
import os
import re
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
