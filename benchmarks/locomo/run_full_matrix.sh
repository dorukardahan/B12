#!/usr/bin/env bash
# Run the LoCoMo 9-cell retrieval matrix (3 storage × 3 search modes) and
# aggregate the per-cell JSON outputs into a single normalized file.
#
# Output JSON layout (keys normalized to recall_at_N for downstream tooling):
#   {
#     "version": "<package.json version>",
#     "date":    "YYYY-MM-DD",
#     "embed_model": "BAAI/bge-m3",
#     "embed_dim":   1024,
#     "dataset":     "locomo10",
#     "results": {
#       "<storage>-<search>": {
#         "recall_at_1": float, "recall_at_3": float, "recall_at_5": float,
#         "token_f1": float, "mrr": float, ...
#       },
#       ...  // 9 cells total
#     }
#   }
#
# Usage:  bash benchmarks/locomo/run_full_matrix.sh [OUTPUT_FILE]
#         Default OUTPUT_FILE = benchmarks/locomo/results-v11.67.json
#
# Env knobs:
#   B12_BENCH_PYTHON  — python3 binary (default: $HOME/.local/b12-venv/bin/python3,
#                       fallback: python3 on PATH)
#   B12_BENCH_TOP_K   — comma-separated top-k values (default: 1,3,5)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-${ROOT}/benchmarks/locomo/results-v11.67.json}"
TOP_K="${B12_BENCH_TOP_K:-1,3,5}"

PY="${B12_BENCH_PYTHON:-${HOME}/.local/b12-venv/bin/python3}"
if [ ! -x "$PY" ]; then PY="$(command -v python3)"; fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/b12-bench.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

STORAGES=(observations summaries dialogues)
SEARCHES=(keyword hybrid vector)
TOP_K_ARGS=$(echo "$TOP_K" | tr ',' ' ')

echo "B12 LoCoMo 9-cell matrix"
echo "  python:   $PY"
echo "  top-k:    $TOP_K"
echo "  output:   $OUT"
echo

for storage in "${STORAGES[@]}"; do
  for search in "${SEARCHES[@]}"; do
    cell="${storage}-${search}"
    echo "──── ${cell} ────"
    # eval_b12.py prints progress text BEFORE the JSON block on stdout, so we
    # extract the top-level `{ … }` envelope with sed (lines from a sole `{`
    # up to the matching sole `}` at column 0) before json.load() sees it.
    # PIPESTATUS[0] surfaces eval_b12.py's exit code past the sed pipe.
    "$PY" "${ROOT}/benchmarks/locomo/eval_b12.py" \
      --mode "$storage" --search "$search" \
      --top-k $TOP_K_ARGS --output json \
      | sed -n '/^{$/,/^}$/p' > "${TMP}/${cell}.json"
    exit_code=${PIPESTATUS[0]}
    if [ "$exit_code" -ne 0 ]; then
      echo "FAIL: ${cell} eval_b12.py exited $exit_code" >&2
      exit 1
    fi
  done
done

# B12_BENCH_VERSION pins the recorded version label — useful when the bench
# runs against a tagged baseline (e.g. v11.67.0) while package.json already
# reflects a later release commit on main.
if [ -n "${B12_BENCH_VERSION:-}" ]; then
  VERSION="$B12_BENCH_VERSION"
else
  VERSION="$(${PY} -c "import json; print(json.load(open('${ROOT}/package.json'))['version'])" 2>/dev/null || echo "unknown")"
fi

# Aggregate + key-normalize (recall@N -> recall_at_N) for downstream tooling.
"$PY" - "$VERSION" "$OUT" "$TMP" "${STORAGES[@]/%/-keyword}" "${STORAGES[@]/%/-hybrid}" "${STORAGES[@]/%/-vector}" <<'PYEOF'
import json, os, sys, datetime
version, out_path, tmp_dir, *cells = sys.argv[1:]
merged = {}
for cell in cells:
    with open(os.path.join(tmp_dir, f"{cell}.json")) as f:
        payload = json.load(f).get("results", {})
    for cell_key, metrics in payload.items():
        norm = {}
        for k, v in metrics.items():
            if k.startswith("recall@"):
                norm["recall_at_" + k.split("@")[1]] = v
            elif k.startswith("ndcg@"):
                norm["ndcg_at_" + k.split("@")[1]] = v
            elif k.startswith("precision@"):
                norm["precision_at_" + k.split("@")[1]] = v
            else:
                norm[k] = v
        merged[cell_key] = norm

header = {
    "dataset":     "locomo10",
    "date":        str(datetime.date.today()),
    "embed_dim":   1024,
    "embed_model": "BAAI/bge-m3",
    "version":     version,
}
# Pretty top-level, compact per-cell (one cell per line) to keep diffs small.
lines = ["{"]
for k in sorted(header):
    lines.append(f"  {json.dumps(k)}: {json.dumps(header[k])},")
lines.append('  "results": {')
keys = sorted(merged.keys())
for i, ck in enumerate(keys):
    cm = json.dumps(merged[ck], sort_keys=True, separators=(", ", ": "))
    comma = "," if i < len(keys) - 1 else ""
    lines.append(f"    {json.dumps(ck)}: {cm}{comma}")
lines.extend(["  }", "}"])
with open(out_path, "w") as f:
    f.write("\n".join(lines) + "\n")
print(f"\n  Aggregated {len(merged)} cells -> {out_path}")
PYEOF

# Validate 9 cells with all four required metrics
"$PY" - "$OUT" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
cells = d["results"]
need = ("recall_at_1", "recall_at_3", "recall_at_5", "token_f1")
missing = [(k, m) for k, v in cells.items() for m in need if m not in v]
if len(cells) != 9 or missing:
    print(f"FAIL: {len(cells)} cells; missing metrics: {missing}", file=sys.stderr)
    sys.exit(1)
print(f"OK: 9 cells, all with {need}")
PYEOF
