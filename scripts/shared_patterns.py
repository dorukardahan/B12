"""Shared patterns and utilities for B12 hooks and scripts.

DRY extraction — regexes, paths, and platform detection used across
hooks/memory-session-end.sh, hooks/memory-precompact.sh, and all scripts.

English + Turkish contextual patterns (v4 format).
"""
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
