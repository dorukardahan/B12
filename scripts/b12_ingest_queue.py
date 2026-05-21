#!/usr/bin/env python3
"""
B12 durable JSONL ingest queue — crash-safe append + ack-pointer drain.

A write to ~/.B12/queue/ingest-YYYYMMDD.jsonl always survives process
death, hook timeout, or daemon crash. A consumer drains the queue by
reading records past the per-day ACK pointer and writing a new offset
to the sidecar `.ack` file atomically (write-temp + rename).

Architecture
============

Producer (hook / merge_or_insert wrapper):

    queue = IngestQueue()
    queue.enqueue({
        "content": "Decision: pnpm 10",
        "content_hash": "abc...",
        "memory_type": "decision",
        "tags": "proj:B12,decision",
        "metadata": {...},
        "enqueued_at": "2026-05-18T01:20:00Z",
        "source": "hooks/memory-session-end.sh",
    })   # append, fsync, return — never blocks on DB

Consumer (background worker — to be wired in a follow-up PR):

    for record in queue.drain():
        try:
            merge_or_insert(conn, **record_to_kwargs(record))
            queue.ack(record)
        except SQLITE_BUSY:
            break  # retry on next worker run

Crash recovery: on process restart, `drain()` opens the file, seeks to
`read_ack_offset()`, and resumes from there. Records past the ack
pointer that already landed in the DB are caught by the
`UNIQUE(content_hash)` constraint on `merge_or_insert` — they become
no-op duplicates rather than double-writes.

Atomic ACK writes
=================

`_write_ack_atomic(offset)` writes `offset\\n` to `<file>.ack.tmp`,
fsyncs the temp file, then `os.replace()`s into place. On macOS / Linux
this is atomic at the filesystem level: any reader sees either the old
ACK or the new one, never a half-written one.

Scope of this PR
================

This PR ships the queue data structures + CLI worker + self-tests.
Wiring the queue into the actual hook write paths (PreCompact,
SessionEnd, LLM extractor) is **deferred to a follow-up**. The
follow-up will:
- Add a `b12_enqueue_or_write` helper that producers call instead of
  `merge_or_insert` directly.
- Move the embed daemon's socket call onto a queue-drain worker.
- Run a launchd-managed sweep job that archives queue files older
  than 7 days.

Attribution
===========

Pattern ported from AytuncYildizli/mahobrain (see PR body).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


# ── Layout ──────────────────────────────────────────────────────────


def _b12_base() -> Path:
    return Path(os.environ.get("B12_DATA_DIR") or (Path.home() / ".B12"))


def _queue_dir() -> Path:
    d = _b12_base() / "queue"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ingest_file_for(day: str | None = None) -> Path:
    """Return the JSONL file path for the given UTC day (YYYYMMDD)."""
    day = day or datetime.now(timezone.utc).strftime("%Y%m%d")
    return _queue_dir() / f"ingest-{day}.jsonl"


def _ack_file_for(ingest_path: Path) -> Path:
    return ingest_path.with_suffix(".jsonl.ack")


# ── Atomic ACK writer ──────────────────────────────────────────────


def _write_ack_atomic(ack_path: Path, offset: int) -> None:
    """Write `offset\\n` to `ack_path` atomically via tmp + replace.

    Critical for crash safety: a half-written ack file would either
    skip records (bad — silent data loss) or duplicate records (fine
    — content_hash dedup catches them). Atomic replace gives us
    "either old or new", never partial.
    """
    if offset < 0:
        raise ValueError(f"ack offset must be >= 0; got {offset}")
    ack_path.parent.mkdir(parents=True, exist_ok=True)
    # tempfile.NamedTemporaryFile in the same dir guarantees the rename
    # is on the same filesystem.
    fd, tmp_path = tempfile.mkstemp(
        prefix=ack_path.name + ".",
        suffix=".tmp",
        dir=str(ack_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{offset}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, ack_path)
    except Exception:
        # Best-effort cleanup of the tmp file on any failure path.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_ack_offset(ack_path: Path) -> int:
    if not ack_path.is_file():
        return 0
    try:
        text = ack_path.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    if not text:
        return 0
    try:
        offset = int(text.split()[0])
    except (ValueError, IndexError):
        return 0
    return max(0, offset)


# ── Record dataclass ───────────────────────────────────────────────


@dataclass(frozen=True)
class IngestRecord:
    """One queue entry, paired with its byte offset for ACK tracking."""

    payload: dict
    start_offset: int  # byte offset where the JSON line begins
    end_offset: int    # byte offset right after the line's trailing \n

    @property
    def content_hash(self) -> str | None:
        v = self.payload.get("content_hash")
        return v if isinstance(v, str) else None


# ── Queue ──────────────────────────────────────────────────────────


class IngestQueue:
    """Append-only JSONL queue with a sidecar ACK pointer.

    Cheap to instantiate. Methods are independent — `enqueue()` can be
    called concurrently with `drain()` on a different process; the
    consumer just won't see the newly-appended bytes until it
    re-opens the file (each `drain()` call opens fresh).
    """

    def __init__(self, *, day: str | None = None, ingest_path: Path | None = None) -> None:
        self.ingest_path = ingest_path or _ingest_file_for(day)
        self.ack_path = _ack_file_for(self.ingest_path)

    # — Producer side —

    def enqueue(self, payload: dict) -> int:
        """Append one JSON line. Returns the new file size after fsync.

        Adds `enqueued_at` if missing. The caller is responsible for
        including everything `merge_or_insert` needs (`content`,
        `content_hash`, `memory_type`, `tags`, `metadata`).

        Torn-write recovery: if a prior crash left a partial JSON line
        without a trailing newline at EOF, this enqueue inserts a
        `\\n` separator before its own write so the new valid record
        lands on its own line. The torn fragment (which is
        unrecoverable on its own anyway) gets isolated where drain()
        can skip it without swallowing the new valid record beside
        it. Codex review on PR #21 flagged this exact crash-recovery
        hole.
        """
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        payload = dict(payload)  # shallow copy; never mutate caller's
        payload.setdefault(
            "enqueued_at",
            datetime.now(timezone.utc).isoformat(),
        )
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.ingest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ingest_path, "a+b") as fb:
            fb.seek(0, os.SEEK_END)
            size = fb.tell()
            if size > 0:
                fb.seek(size - 1)
                if fb.read(1) != b"\n":
                    fb.write(b"\n")
            fb.write(line.encode("utf-8"))
            fb.flush()
            os.fsync(fb.fileno())
        return self.ingest_path.stat().st_size

    # — Consumer side —

    def read_ack_offset(self) -> int:
        return _read_ack_offset(self.ack_path)

    def drain(self) -> Iterator[IngestRecord]:
        """Yield un-acked records past the current ACK pointer.

        Each record carries its `start_offset` / `end_offset` so the
        consumer can ACK whichever batch boundary it likes. A consumer
        that processes records one-at-a-time should call `ack(record)`
        after each successful merge_or_insert; a batch consumer can
        call `ack(records[-1])` once per batch.

        ACK offset is clamped against the ingest file's current size.
        If the stored offset exceeds the file size (e.g., the ingest
        file was rotated/archived to a smaller one, or the .ack file
        is stale/corrupted), we treat it as a fresh start at offset 0
        instead of seeking past EOF and silently losing all records.
        Codex review on PR #21 flagged this exact stale-ack hole.
        """
        if not self.ingest_path.is_file():
            return
        stored_ack = self.read_ack_offset()
        file_size = self.ingest_path.stat().st_size
        if stored_ack > file_size:
            # Stale or corrupt ACK pointer — file likely rotated.
            # Persist the reset so ack(record) can advance again.
            _write_ack_atomic(self.ack_path, 0)
            start = 0
        else:
            start = stored_ack
        with open(self.ingest_path, "rb") as fb:
            fb.seek(start)
            while True:
                pos = fb.tell()
                raw = fb.readline()
                if not raw:
                    return
                end = fb.tell()
                # Skip malformed lines (orphan bytes from a crash mid-write
                # would land here). The hash uniqueness in merge_or_insert
                # makes this safe.
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(obj, dict):
                    continue
                yield IngestRecord(payload=obj, start_offset=pos, end_offset=end)

    def ack(self, record: IngestRecord) -> None:
        """Advance the ACK pointer past this record. Idempotent."""
        current = self.read_ack_offset()
        if record.end_offset > current:
            _write_ack_atomic(self.ack_path, record.end_offset)

    def stats(self) -> dict:
        """Return a brief diagnostic snapshot.

        - bytes_total: size of the JSONL file (0 if absent).
        - bytes_acked: current ACK pointer, clamped to bytes_total
          so a stale/corrupt .ack file can't show "acked > total".
        - bytes_pending: total - acked (clamped to 0).
        """
        size = self.ingest_path.stat().st_size if self.ingest_path.is_file() else 0
        acked = min(self.read_ack_offset(), size)
        pending = max(0, size - acked)
        return {
            "ingest_path": str(self.ingest_path),
            "ack_path": str(self.ack_path),
            "bytes_total": size,
            "bytes_acked": acked,
            "bytes_pending": pending,
        }


# ── CLI ────────────────────────────────────────────────────────────


def _cli_stats(_args) -> int:
    q = IngestQueue()
    s = q.stats()
    sys.stdout.write(json.dumps(s, indent=2) + "\n")
    return 0


def _cli_drain(args) -> int:
    """Print enqueued records past the ACK pointer.

    Does NOT auto-write to the DB — this CLI is a diagnostic /
    follow-up-PR integration point. The real consumer worker will
    live next to embed_daemon.py and call merge_or_insert.
    """
    q = IngestQueue()
    count = 0
    for record in q.drain():
        sys.stdout.write(json.dumps(record.payload, ensure_ascii=False) + "\n")
        count += 1
        if args.ack:
            q.ack(record)
    sys.stderr.write(f"drained {count} record(s); acked={args.ack}\n")
    return 0


def _cli_enqueue(args) -> int:
    """Enqueue a payload read from stdin (one JSON object)."""
    raw = sys.stdin.read().strip()
    if not raw:
        sys.stderr.write("error: stdin empty\n")
        return 2
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        sys.stderr.write(f"error: malformed JSON ({e})\n")
        return 2
    if not isinstance(payload, dict):
        sys.stderr.write("error: payload must be a JSON object\n")
        return 2
    q = IngestQueue()
    new_size = q.enqueue(payload)
    sys.stderr.write(f"enqueued; queue now {new_size} bytes\n")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="b12_ingest_queue",
        description="B12 durable JSONL ingest queue (mahobrain port).",
    )
    sub = p.add_subparsers(dest="cmd", required=False)

    sub.add_parser("stats", help="Print queue size + ack pointer as JSON.")

    p_drain = sub.add_parser("drain", help="Print records past the ack pointer.")
    p_drain.add_argument("--ack", action="store_true",
                         help="Advance ack pointer after each printed record.")

    sub.add_parser("enqueue", help="Read one JSON object from stdin and append it.")
    sub.add_parser("self-test", help="Run the embedded test suite.")

    args = p.parse_args(argv)

    if args.cmd == "stats":
        return _cli_stats(args)
    if args.cmd == "drain":
        return _cli_drain(args)
    if args.cmd == "enqueue":
        return _cli_enqueue(args)
    if args.cmd == "self-test":
        return _self_test()
    p.print_help()
    return 0


# ── Self-test (filesystem only; no DB, no daemon) ──────────────────


def _self_test() -> int:
    import shutil
    import tempfile as _tmp

    failures: list[str] = []

    def expect(cond: bool, label: str) -> None:
        marker = "OK  " if cond else "FAIL"
        print(f"  [{marker}] {label}")
        if not cond:
            failures.append(label)

    tmpdir = Path(_tmp.mkdtemp(prefix="b12-ingest-queue-test-"))
    ingest = tmpdir / "ingest-20260518.jsonl"
    q = IngestQueue(ingest_path=ingest)

    try:
        # 1. enqueue → file exists, size grows
        size1 = q.enqueue({"content": "first", "content_hash": "h1", "memory_type": "decision"})
        expect(ingest.is_file() and size1 > 0, "1. enqueue_writes_to_file")

        # 2. enqueued_at auto-populated
        with open(ingest, "r", encoding="utf-8") as f:
            record = json.loads(f.readline())
        expect("enqueued_at" in record, "2. enqueue_auto_populates_timestamp")

        # 3. ACK starts at 0
        expect(q.read_ack_offset() == 0, "3. initial_ack_offset_zero")

        # 4. drain yields the un-acked record
        records = list(q.drain())
        expect(len(records) == 1 and records[0].payload["content"] == "first",
               "4. drain_yields_unacked_record")

        # 5. ack advances the pointer
        q.ack(records[0])
        expect(q.read_ack_offset() == records[0].end_offset, "5. ack_advances_pointer")

        # 6. After ack, drain is empty
        expect(list(q.drain()) == [], "6. drain_empty_after_ack")

        # 7. enqueue more, drain only un-acked
        q.enqueue({"content": "second", "content_hash": "h2", "memory_type": "fact"})
        q.enqueue({"content": "third", "content_hash": "h3", "memory_type": "fact"})
        records2 = list(q.drain())
        expect(len(records2) == 2 and records2[0].payload["content"] == "second"
               and records2[1].payload["content"] == "third",
               "7. drain_skips_acked_records")

        # 8. Crash recovery: drop ACK file, recreate queue object, drain
        # should yield ALL records (no ack survived).
        q.ack(records2[-1])  # ack to end
        os.unlink(q.ack_path)
        q2 = IngestQueue(ingest_path=ingest)
        records3 = list(q2.drain())
        expect(len(records3) == 3, "8. crash_recovery_drain_all_after_ack_lost")

        # 9. Atomic ACK write: simulate writer dying mid-replace by
        # checking that there's no `.tmp` debris after a successful write.
        _write_ack_atomic(q.ack_path, records2[-1].end_offset)
        leftovers = list(tmpdir.glob("*.tmp"))
        expect(leftovers == [], "9. ack_write_cleans_tmp_files")

        # 10. Malformed record handling: append a half-line, ensure drain skips it
        with open(ingest, "a", encoding="utf-8") as f:
            f.write("{this is not json\n")
            f.flush()
        q3 = IngestQueue(ingest_path=ingest)
        os.unlink(q3.ack_path)
        records4 = list(q3.drain())
        # Should still get the original 3 valid records, skipping the malformed one
        expect(len(records4) == 3, "10. drain_skips_malformed_lines")

        # 11. Non-dict payload rejected at enqueue
        try:
            q.enqueue("not a dict")  # type: ignore[arg-type]
            rejected = False
        except TypeError:
            rejected = True
        expect(rejected, "11. enqueue_rejects_non_dict")

        # 12. Stats reports pending bytes correctly (ack still gone from
        # test 10's unlink; q3 has no ack pointer)
        s = q3.stats()
        expect(s["bytes_pending"] == s["bytes_total"] and s["bytes_acked"] == 0,
               "12. stats_pending_equals_total_when_no_ack")

        # 13. Ack is idempotent: acking the same record twice doesn't regress
        all_records = list(q3.drain())
        last = all_records[-1]
        q3.ack(last)
        first_offset = q3.read_ack_offset()
        q3.ack(last)
        expect(q3.read_ack_offset() == first_offset, "13. ack_idempotent")

        # 14. Re-ack with an earlier record does NOT regress the pointer
        earlier = all_records[0]
        q3.ack(earlier)
        expect(q3.read_ack_offset() == first_offset, "14. ack_never_regresses")

        # 15. Torn prior write: file ends without a newline → next
        # enqueue inserts a \n separator so the new valid record is on
        # its own line. drain() yields the new record, skipping the
        # torn fragment.
        ingest15 = tmpdir / "ingest-torn-test.jsonl"
        with open(ingest15, "wb") as f:
            f.write(b'{"content":"torn-fragment","ha')  # no newline, no closing brace
        q15 = IngestQueue(ingest_path=ingest15)
        q15.enqueue({"content": "valid after torn", "content_hash": "torn-h"})
        records15 = list(q15.drain())
        expect(
            len(records15) == 1 and records15[0].payload["content"] == "valid after torn",
            "15. torn_prior_write_does_not_swallow_new_record",
        )

        # 16. Stale ACK offset past EOF → treat as fresh start.
        ingest16 = tmpdir / "ingest-stale-ack-test.jsonl"
        q16 = IngestQueue(ingest_path=ingest16)
        q16.enqueue({"content": "first", "content_hash": "s1"})
        q16.enqueue({"content": "second", "content_hash": "s2"})
        # Forge a stale ACK pointer that's WAY past the current file size.
        _write_ack_atomic(q16.ack_path, 999999)
        records16 = list(q16.drain())
        expect(
            len(records16) == 2,
            "16. stale_ack_past_eof_treated_as_fresh_start",
        )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    total = 16
    print()
    if failures:
        print(f"FAILED: {len(failures)} / {total} cases  →  {failures}")
        return 1
    print(f"PASSED: {total} / {total} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
