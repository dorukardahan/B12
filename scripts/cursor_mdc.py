"""Cursor MDC globs Auto-Attached reader (Plan §B3).

Parses .cursor/rules/*.mdc from the project root. Each .mdc file is YAML
frontmatter between `---` lines + a markdown body. Returns rules whose
`globs:` match active files (fnmatch) or whose `alwaysApply: true`.

Stdlib-only — no PyYAML — handles the two frontmatter shapes Cursor
users actually write (block-list with `- item` and flow-list `[a,b]`).
"""
from __future__ import annotations

import fnmatch
import os
import re

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


def _parse_mdc(path: str) -> tuple[dict, str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            raw = fh.read()
    except OSError:
        return {}, ""
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw
    fm, body = m.group(1), m.group(2)
    front: dict = {}
    current_key: str | None = None
    for line in fm.splitlines():
        ln = line.rstrip()
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if ln.startswith((" ", "\t")):
            item = ln.strip()
            if item.startswith("- "):
                item = item[2:].strip().strip('"').strip("'")
                if current_key and isinstance(front.get(current_key), list):
                    front[current_key].append(item)
            continue
        if ":" in ln:
            k, _, v = ln.partition(":")
            k, v = k.strip(), v.strip()
            current_key = k
            if not v:
                front[k] = []
            elif v.startswith("[") and v.endswith("]"):
                front[k] = [s.strip().strip('"').strip("'")
                            for s in v[1:-1].split(",") if s.strip()]
            else:
                front[k] = v.strip('"').strip("'")
    return front, body.strip()


def attached_rules(project_root: str, active_files: list[str]) -> list[dict]:
    rules_dir = os.path.join(project_root, ".cursor", "rules")
    if not os.path.isdir(rules_dir):
        return []
    out: list[dict] = []
    try:
        for fname in sorted(os.listdir(rules_dir)):
            if not fname.endswith(".mdc"):
                continue
            fm, body = _parse_mdc(os.path.join(rules_dir, fname))
            always = str(fm.get("alwaysApply", "")).lower() == "true"
            globs = fm.get("globs") or []
            if isinstance(globs, str):
                globs = [globs]
            attached = always or (
                active_files and isinstance(globs, list) and globs and any(
                    fnmatch.fnmatch(af, g) or fnmatch.fnmatch(os.path.basename(af), g)
                    for af in active_files for g in globs
                )
            )
            if attached:
                out.append({
                    "name": os.path.splitext(fname)[0],
                    "description": str(fm.get("description", "")),
                    "body": body,
                })
    except OSError:
        return []
    return out


if __name__ == "__main__":
    import json, sys
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    lines_mode = "--lines" in sys.argv
    root = args[0] if args else "."
    files = args[1:] if len(args) > 1 else []
    rules = attached_rules(root, files)
    if lines_mode:
        for r in rules[:5]:
            name = r.get("name", "")
            desc = (r.get("description", "") or "")[:80]
            body = (r.get("body", "") or "")[:200].replace("\n", " ")
            print(f"- [{name}] {desc} :: {body}")
    else:
        print(json.dumps(rules, indent=2))
