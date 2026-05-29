#!/usr/bin/env python3
"""B12 embedding-model drift self-heal.

Reads the live DB's vec0 embedding dimension, derives the canonical
embedding model from it, then reaffirms ``MCP_EMBEDDING_MODEL`` across
every *already-deployed* B12 config:

  - Claude Code: ``~/.claude.json`` and ``~/.claude*/.claude.json``
  - Codex CLI:   ``~/.codex/config.toml``

It also ensures the Codex ``[mcp_servers.B12.tools.memory_store]``
``approval_mode = "auto"`` block is present (the silent-store pattern).

Why this exists
---------------
The vec0 virtual table is fixed at ``FLOAT[N]`` at creation. If a setup's
``MCP_EMBEDDING_MODEL`` produces a different dimension, every embedding
``INSERT`` silently fails and that setup degrades to FTS-only recall. The
bge-m3 migration (v11.34) only rewrote the model in *one* Claude config,
so other deployed setups kept the old 384-dim MiniLM and drifted. This
script makes ``install.sh`` detect and repair that drift on every run.

Design
------
- stdlib only (json, sqlite3, re) — runs on system python3, no venv needed.
- Reads the DDL via ``SELECT sql FROM sqlite_master`` — the vec0 extension
  is NOT required to read the schema string.
- Only touches configs that already register B12 (never creates new
  registrations on platforms the user didn't opt into).
- Idempotent: rewrites a file only when a value actually changes.

Usage
-----
    python3 heal_embedding_model.py            # detect + fix
    python3 heal_embedding_model.py --check     # report only; exit 1 if drift
    python3 heal_embedding_model.py --quiet     # suppress "already aligned" lines
"""

import glob
import json
import os
import re
import sqlite3
import sys

# Embedding dimension → canonical model id. ONLY the current canonical
# model is mapped. We deliberately do NOT map legacy dimensions: B12's
# 384-dim history spans MULTIPLE models in different embedding spaces
# (all-MiniLM-L6-v2 AND paraphrase-multilingual-MiniLM-L12-v2), so guessing
# a model for a legacy dim could rewrite a config to the WRONG space and
# silently corrupt recall/reranking (same dim → inserts succeed, vectors
# don't match). A non-1024 DB should be migrated with
# scripts/migrate_embed_to_bge_m3.py, not auto-healed; an unmapped dim makes
# resolve_canonical() return None so this script warns and leaves configs
# untouched.
DIM_TO_MODEL = {
    1024: "BAAI/bge-m3",
}
# Fallback when no DB exists yet (fresh install) — matches install.sh default.
DEFAULT_MODEL = "BAAI/bge-m3"

GREEN, YELLOW, RED, NC = "\033[0;32m", "\033[1;33m", "\033[0;31m", "\033[0m"


def _info(msg):
    print(f"{GREEN}[OK]{NC} {msg}")


def _warn(msg):
    print(f"{YELLOW}[!]{NC} {msg}")


def db_path():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/mcp-memory/sqlite_vec.db")
    if os.path.isdir(os.path.expanduser("~/AppData")):
        return os.path.expanduser("~/AppData/Local/mcp-memory/sqlite_vec.db")
    return os.path.expanduser("~/.local/share/mcp-memory/sqlite_vec.db")


def detect_dim(path):
    """Return the vec0 FLOAT[N] dimension of memory_embeddings, or None."""
    if not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT sql FROM sqlite_master WHERE name='memory_embeddings'"
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    m = re.search(r"FLOAT\[(\d+)\]", row[0])
    return int(m.group(1)) if m else None


def resolve_canonical():
    """(model, dim, source) — source ∈ {db, default}. model None ⇒ skip."""
    dim = detect_dim(db_path())
    if dim is None:
        return DEFAULT_MODEL, None, "default"
    model = DIM_TO_MODEL.get(dim)
    return model, dim, "db"


# ── Claude Code JSON configs ─────────────────────────────────────────

def claude_config_paths():
    paths = []
    top = os.path.expanduser("~/.claude.json")
    if os.path.isfile(top):
        paths.append(top)
    for d in sorted(glob.glob(os.path.expanduser("~/.claude*"))):
        cfg = os.path.join(d, ".claude.json")
        if os.path.isfile(cfg) and cfg not in paths:
            paths.append(cfg)
    return paths


def heal_json(path, want, check):
    """Return ('ok'|'fixed'|'drift'|'skip', detail) for a Claude json config."""
    try:
        with open(path, "r") as f:
            cfg = json.load(f)
    except Exception as e:
        return ("skip", f"invalid/unreadable JSON ({e})")
    srv = (cfg.get("mcpServers") or {}).get("B12")
    if not isinstance(srv, dict):
        return ("skip", "no B12 entry")
    env = srv.get("env")
    if not isinstance(env, dict):
        env = {}
        srv["env"] = env
    cur = env.get("MCP_EMBEDDING_MODEL")
    if cur == want:
        return ("ok", want)
    if check:
        return ("drift", f"{cur!r} → should be {want!r}")
    env["MCP_EMBEDDING_MODEL"] = want
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    return ("fixed", f"{cur!r} → {want!r}")


# ── Codex TOML config ────────────────────────────────────────────────

def _toml_section_of(line):
    """Return table name for a '[table]' header line, else None ('[[..]]' ignored)."""
    s = line.strip()
    if s.startswith("[") and not s.startswith("[["):
        return s.split("]")[0].lstrip("[").strip()
    return None


def heal_codex(path, want, check):
    """Heal MCP_EMBEDDING_MODEL + ensure memory_store approval_mode in codex toml."""
    if not os.path.isfile(path):
        return ("skip", "no config.toml")
    with open(path, "r") as f:
        lines = f.readlines()

    has_b12 = any(_toml_section_of(ln) == "mcp_servers.B12" for ln in lines)
    if not has_b12:
        return ("skip", "no B12 entry")

    changes = []
    cur_section = None
    env_hdr_idx = None        # line index of the [..B12.env] header, if any
    model_idx = None          # line index of its MCP_EMBEDDING_MODEL line, if any
    model_cur = None          # current model value, if the key exists
    approval_hdr_idx = None   # line index of the [..memory_store] header, if any
    approval_idx = None       # line index of its approval_mode line, if any
    approval_cur = None       # current approval_mode value, if the key exists
    for i, ln in enumerate(lines):
        sec = _toml_section_of(ln)
        if sec is not None:
            cur_section = sec
            if sec == "mcp_servers.B12.env":
                env_hdr_idx = i
            elif sec == "mcp_servers.B12.tools.memory_store":
                approval_hdr_idx = i
            continue
        # Match TOML basic ("...") AND literal ('...') strings — the \1
        # backreference requires matching quotes. The trailing
        # (?:#.*)? allows a valid inline comment after the value
        # (e.g. `... = "BAAI/bge-m3"  # canonical`). Missing either form
        # would leave a real key undetected and we'd insert a duplicate,
        # breaking the config.
        if cur_section == "mcp_servers.B12.env":
            m = re.match(r"""\s*MCP_EMBEDDING_MODEL\s*=\s*(['"])(.*?)\1\s*(?:#.*)?$""", ln)
            if m:
                model_cur, model_idx = m.group(2), i
        elif cur_section == "mcp_servers.B12.tools.memory_store":
            m = re.match(r"""\s*approval_mode\s*=\s*(['"])(.*?)\1\s*(?:#.*)?$""", ln)
            if m:
                approval_cur, approval_idx = m.group(2), i

    # Both the model key and the approval key must end up EXACTLY right.
    # model_cur is None when the key is missing — treat that as drift too
    # (symmetric with heal_json), else a missing key on a non-1024 DB would
    # silently fall back to the server default and re-create the mismatch.
    # TOML forbids declaring a table twice, so we update existing
    # sections/keys in place and only ever append a header that is absent.
    model_needs = model_cur != want
    if model_needs:
        if model_idx is not None:
            changes.append(f"model {model_cur!r} → {want!r}")
        elif env_hdr_idx is not None:
            changes.append(f'add MCP_EMBEDDING_MODEL="{want}" (key missing)')
        else:
            changes.append(f'add [mcp_servers.B12.env] MCP_EMBEDDING_MODEL="{want}"')
    approval_needs = approval_cur != "auto"
    if approval_needs:
        if approval_idx is not None:
            changes.append(f'approval_mode {approval_cur!r} → "auto"')
        elif approval_hdr_idx is not None:
            changes.append('set approval_mode="auto" (key missing)')
        else:
            changes.append('add [mcp_servers.B12.tools.memory_store] approval_mode="auto"')

    if not changes:
        return ("ok", want)
    if check:
        return ("drift", "; ".join(changes))

    model_line = f'MCP_EMBEDDING_MODEL = "{want}"\n'
    approval_line = 'approval_mode = "auto"\n'

    # 1) In-place rewrites of existing keys (no index shift).
    if model_needs and model_idx is not None:
        lines[model_idx] = model_line
    if approval_needs and approval_idx is not None:
        lines[approval_idx] = approval_line

    # 2) Insert a key under an existing header. Apply DESCENDING by index so
    #    an earlier insert doesn't invalidate a later insert's index.
    inserts = []
    if model_needs and model_idx is None and env_hdr_idx is not None:
        inserts.append((env_hdr_idx + 1, model_line))
    if approval_needs and approval_idx is None and approval_hdr_idx is not None:
        inserts.append((approval_hdr_idx + 1, approval_line))
    for idx, text in sorted(inserts, reverse=True):
        lines.insert(idx, text)

    # 3) Append a whole section ONLY when its header is entirely absent.
    appended = []
    if model_needs and model_idx is None and env_hdr_idx is None:
        appended.append(f"\n[mcp_servers.B12.env]\n{model_line}")
    if approval_needs and approval_idx is None and approval_hdr_idx is None:
        appended.append(f"\n[mcp_servers.B12.tools.memory_store]\n{approval_line}")
    content = "".join(lines)
    if appended:
        content = content.rstrip("\n") + "\n" + "".join(appended)

    with open(path, "w") as f:
        f.write(content)
    return ("fixed", "; ".join(changes))


def main():
    check = "--check" in sys.argv or "--dry-run" in sys.argv
    quiet = "--quiet" in sys.argv

    model, dim, source = resolve_canonical()
    if model is None:
        _warn(
            f"DB embedding dim={dim} is not B12's canonical 1024-dim "
            f"(BAAI/bge-m3). Refusing to guess a {dim}-dim model — a legacy "
            f"DB should be migrated with scripts/migrate_embed_to_bge_m3.py. "
            f"Leaving configs untouched."
        )
        return 0

    where = f"DB vec0 dim={dim}" if source == "db" else "no DB yet (default)"
    if not quiet:
        _info(f"Canonical embedding model: {model}  ({where})")

    targets = [("json", p) for p in claude_config_paths()]
    targets.append(("codex", os.path.expanduser("~/.codex/config.toml")))

    drift_found = False
    fixed_any = False
    for kind, path in targets:
        status, detail = heal_json(path, model, check) if kind == "json" \
            else heal_codex(path, model, check)
        short = path.replace(os.path.expanduser("~"), "~")
        if status == "ok":
            if not quiet:
                _info(f"{short}: aligned ({detail})")
        elif status == "fixed":
            fixed_any = True
            _warn(f"{short}: DRIFT REPAIRED — {detail}")
        elif status == "drift":
            drift_found = True
            _warn(f"{short}: DRIFT — {detail}")
        # 'skip' (no B12 entry / unreadable) is silent unless verbose

    if check and drift_found:
        return 1
    if fixed_any:
        _warn("Embedding-model drift was repaired. Restart affected sessions to pick up the change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
