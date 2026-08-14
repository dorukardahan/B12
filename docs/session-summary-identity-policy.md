# Session-summary identity and retention policy

B12 treats `session_summary` identity as an explicit writer contract. A missing
`metadata.session_id` is not evidence that a row is corrupt, duplicated, or safe
to delete. MCP-only hosts can legitimately produce a useful project summary
without exposing a stable host session ID.

## Identity categories

Every active `session_summary` belongs to exactly one category:

- **`bound`** — `metadata.session_id` contains a non-empty, trimmed stable ID.
  Current lifecycle writers use `upsert_session_summary()` for this category.
- **`intentionally_unbound`** — the writer cannot provide a stable ID and records
  `session_identity: "unbound"` together with non-empty `producer` and `platform`
  metadata, with no structured recovery candidate. The MCP session tracker is the
  canonical current example.
- **`recoverable_legacy`** — no usable `metadata.session_id` exists, but exactly
  one candidate exists in an authoritative structured surface currently
  recognized by B12: `metadata.source_session` or an exact `session:<id>` tag.
  A 12-character legacy prefix is recoverable only when it does not collide with
  multiple full IDs already present in the database.
- **`ambiguous_legacy`** — no authoritative candidate exists, candidates conflict,
  a legacy prefix collides, metadata is malformed, or an explicit-unbound marker
  is incomplete or conflicts with structured recovery evidence. A complete marker
  plus one authoritative candidate is classified as recoverable; conflicting
  candidates remain ambiguous.

Free-form content, timestamps, project names, and content hashes are never
session identity. They can describe or group a row, but they cannot authorize an
identity backfill.

## Writer contract

A current writer that creates a `session_summary` must do one of the following:

1. supply a stable full `session_id` through `upsert_session_summary()`; or
2. set all three unbound fields:
   `session_identity: "unbound"`, `producer: <stable writer name>`, and
   `platform: <stable host class>`.

Writers must also include a project when one is known. Field absence is retained
only as a legacy state; new code must not use it to mean unbound.

## Read-only audit

Run the audit against a local database:

```bash
python3 scripts/b12_audit_session_summaries.py --db-path /path/to/sqlite_vec.db
python3 scripts/b12_audit_session_summaries.py --db-path /path/to/sqlite_vec.db --json
```

The command fingerprints and copies a stable DB+WAL+SHM image into a private local
temporary directory without opening or locking the source files, then opens only
that snapshot with `mode=ro` and `PRAGMA query_only=ON`. It fails closed if a
rollback journal is active, WAL commit-boundary state is unavailable, or the
source changes across five copy attempts. Preserving the WAL index's published
frame boundary prevents complete but not-yet-published WAL frames from appearing
in the audit. This keeps the source database, WAL, SHM, and journal bytes unchanged while still
including committed WAL rows. The audit reports category totals and, with
`--json`, one payload-free record for every active
unbound summary: row ID, category, report-local keyed labels for producer/platform/
project grouping, allowlisted tag-shape classes, age bucket, and recovery-source class.
The label key is randomly generated per report and never emitted, so low-entropy
values cannot be dictionary-matched or linked across reports. The audit never returns
raw dimension values, unnamespaced tag values, memory content, or candidate session
IDs, and it has no mutation flag.

## Retention

Retention is conservative because project context reads the newest active
summary by project tag, independently of `metadata.session_id`.

- **Bound rows:** write-time upsert owns current-session cardinality. Historical
  duplicate cleanup remains the separate dry-run-first dedupe workflow.
- **Intentionally unbound rows:** group only by non-empty `(project, producer)`.
  Preserve the newest five active summaries in every group. Rows beyond that
  floor become soft-delete candidates only after they are at least 90 days old.
- **Recoverable legacy rows:** identity recovery precedes retention. Do not apply
  the unbound retention rule after silently treating a candidate as a confirmed
  ID.
- **Ambiguous legacy rows:** hold rows that lack a reliable project or producer.
  Where both dimensions are reliable, the same newest-five/90-day rule may be
  proposed in a reviewed dry-run, but never as startup maintenance.

Retention uses soft deletion first. Hard deletion is deferred to the normal
post-tombstone garbage-collection window and is never part of this audit.

SessionEnd only writes or upserts summaries. It does not execute retention: a
rank-only lifecycle cap cannot prove row age, identity category, or backup
readiness and is therefore unsafe.

## Migration and rollback gates

There is no automatic migration or startup cleanup. A future identity backfill
or retention tool must satisfy all of these gates before it can mutate data:

1. create and verify a WAL-safe database backup;
2. emit a complete dry-run plan with row IDs, source field/tag, collision result,
   and before/after category counts;
3. reject conflicting candidates and colliding legacy prefixes;
4. run in one transaction and prove a second identical run is a no-op;
5. preserve content, hashes, FTS rows, vectors, graph edges, and timestamps except
   for the explicitly planned identity field or `deleted_at` tombstone;
6. compare project-context retrieval before and after, proving that each affected
   project still returns the same newest retained summary;
7. restore the backup and re-run integrity checks as the rollback test before the
   migration is considered shippable.

A dry-run count is planning evidence, not permission to mutate a user's database.
