"""Append-only SQLite-backed store for `ExperimentRecord`s."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .schema import ExperimentRecord, Mutation, RenderMode, Verdict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    record_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    wave_id INTEGER NOT NULL,
    parent_id TEXT,
    mutation TEXT,
    model_name TEXT NOT NULL,
    latent_params TEXT NOT NULL,
    realized_dims TEXT NOT NULL,
    render_mode TEXT NOT NULL,
    renderer_model TEXT,
    task_seed INTEGER NOT NULL,
    rendering_seed INTEGER NOT NULL,
    trial_seed INTEGER NOT NULL,
    prompt_text TEXT,
    response_text TEXT,
    verdict TEXT NOT NULL,
    verifier_detail TEXT,
    tokens_in INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    latency_ms REAL NOT NULL,
    reasoning_text TEXT,
    finish_reason TEXT,
    created_at TEXT NOT NULL
);
"""

# Columns added after the original schema. Each entry is (name, ddl_type);
# applied via idempotent ALTER TABLE so pre-existing dbs (which lack them)
# are migrated in place without touching existing row data.
_MIGRATION_COLUMNS = [
    ("reasoning_text", "TEXT"),
    ("finish_reason", "TEXT"),
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_experiments_model_name ON experiments(model_name);",
    "CREATE INDEX IF NOT EXISTS idx_experiments_run_id ON experiments(run_id);",
    "CREATE INDEX IF NOT EXISTS idx_experiments_parent_id ON experiments(parent_id);",
    "CREATE INDEX IF NOT EXISTS idx_experiments_verdict ON experiments(verdict);",
]


def _record_to_row(record: ExperimentRecord) -> tuple:
    return (
        record.record_id,
        record.run_id,
        record.wave_id,
        record.parent_id,
        json.dumps(record.mutation.to_json_dict()) if record.mutation else None,
        record.model_name,
        json.dumps(record.latent_params),
        json.dumps(record.realized_dims),
        record.render_mode.name,
        record.renderer_model,
        record.task_seed,
        record.rendering_seed,
        record.trial_seed,
        record.prompt_text,
        record.response_text,
        record.verdict.name,
        record.verifier_detail,
        record.tokens_in,
        record.tokens_out,
        record.cost_usd,
        record.latency_ms,
        record.reasoning_text,
        record.finish_reason,
        record.created_at,
    )


def _row_to_record(row: sqlite3.Row) -> ExperimentRecord:
    mutation = (
        Mutation.from_json_dict(json.loads(row["mutation"]))
        if row["mutation"] is not None
        else None
    )
    return ExperimentRecord(
        record_id=row["record_id"],
        run_id=row["run_id"],
        wave_id=row["wave_id"],
        parent_id=row["parent_id"],
        mutation=mutation,
        model_name=row["model_name"],
        latent_params=json.loads(row["latent_params"]),
        realized_dims=json.loads(row["realized_dims"]),
        render_mode=RenderMode[row["render_mode"]],
        renderer_model=row["renderer_model"],
        task_seed=row["task_seed"],
        rendering_seed=row["rendering_seed"],
        trial_seed=row["trial_seed"],
        prompt_text=row["prompt_text"],
        response_text=row["response_text"],
        verdict=Verdict[row["verdict"]],
        verifier_detail=row["verifier_detail"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        cost_usd=row["cost_usd"],
        latency_ms=row["latency_ms"],
        reasoning_text=_row_get(row, "reasoning_text"),
        finish_reason=_row_get(row, "finish_reason"),
        created_at=row["created_at"],
    )


def _row_get(row: sqlite3.Row, key: str):
    """sqlite3.Row access that tolerates rows from a pre-migration schema
    that lack the column entirely (older cursors on a db opened before the
    ALTER TABLE ran within this connection would still have it, but this
    guards defensively for any row shape without the key)."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


_INSERT_SQL = """
INSERT INTO experiments (
    record_id, run_id, wave_id, parent_id, mutation, model_name,
    latent_params, realized_dims, render_mode, renderer_model,
    task_seed, rendering_seed, trial_seed, prompt_text, response_text,
    verdict, verifier_detail, tokens_in, tokens_out, cost_usd, latency_ms,
    reasoning_text, finish_reason, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class ResultsStore:
    """Append-only SQLite store of `ExperimentRecord`s.

    Design guarantee: records are immutable once written. This class
    intentionally exposes no `update` or `delete` method; every write goes
    through plain `INSERT` (never `INSERT OR REPLACE`), so a duplicate
    `record_id` raises `sqlite3.IntegrityError` rather than silently
    overwriting history. Analysis and lineage tracing can therefore trust
    that a record read today will read identically tomorrow.
    """

    def __init__(self, path: str | Path, read_only: bool = False) -> None:
        # check_same_thread=False + an internal write lock make appends safe
        # from worker threads: the parallel campaign scheduler (see
        # `cartographer/schedule/scheduler.py`, SchedulerConfig.parallelism)
        # calls PipelineEvaluator -- and therefore `append` -- from a
        # ThreadPoolExecutor. All writes are serialized under `_write_lock`;
        # sqlite itself then only ever sees one writer on this connection.
        #
        # `read_only=True` opens the file through sqlite's `mode=ro` URI so
        # NO byte of the target db is ever written -- not the CREATE TABLE,
        # not the ALTER TABLE migration, not the index DDL. This is the
        # opener every analysis surface uses against the LIVE, gitignored
        # `results/campaign.sqlite`, which a paid campaign may be appending
        # to concurrently: a reader must never take a write lock on it or
        # mutate its schema. A read-only store is append-incapable by
        # construction (`append`/`append_many` raise), so the immutability
        # guarantee above is enforced, not merely honored.
        self._read_only = read_only
        if read_only:
            uri = f"file:{Path(path)}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._write_lock = threading.Lock()
            self._conn.row_factory = sqlite3.Row
            # No DDL: the db is opened purely for reading. A db that predates
            # the reasoning_text/finish_reason migration still reads fine --
            # `_row_get` already tolerates a missing column.
            return
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._write_lock = threading.Lock()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._migrate()
        for stmt in _INDEXES:
            self._conn.execute(stmt)
        self._conn.commit()

    @classmethod
    def open_read_only(cls, path: str | Path) -> "ResultsStore":
        """Open an existing db for reading only -- see `__init__(read_only=True)`.

        Use this everywhere an analysis/report surface touches the live
        `results/campaign.sqlite`: it guarantees the reader can neither
        write rows nor alter the schema of a file a campaign may be
        actively appending to.
        """
        return cls(path, read_only=True)

    def _migrate(self) -> None:
        """Idempotently add columns introduced after the original schema to
        an existing `experiments` table. Safe to call on a brand-new db
        (columns already present from `_SCHEMA`) or an old one missing them."""
        existing = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(experiments)")
        }
        for name, ddl_type in _MIGRATION_COLUMNS:
            if name not in existing:
                self._conn.execute(f"ALTER TABLE experiments ADD COLUMN {name} {ddl_type}")
        self._conn.commit()

    def append(self, record: ExperimentRecord) -> None:
        if self._read_only:
            raise RuntimeError("ResultsStore opened read-only: append is disabled")
        with self._write_lock, self._conn:
            self._conn.execute(_INSERT_SQL, _record_to_row(record))

    def append_many(self, records: list[ExperimentRecord]) -> None:
        if self._read_only:
            raise RuntimeError("ResultsStore opened read-only: append is disabled")
        with self._write_lock, self._conn:
            self._conn.executemany(_INSERT_SQL, (_record_to_row(r) for r in records))

    def by_run(self, run_id: str) -> list[ExperimentRecord]:
        cur = self._conn.execute(
            "SELECT * FROM experiments WHERE run_id = ? ORDER BY created_at, record_id",
            (run_id,),
        )
        return [_row_to_record(row) for row in cur.fetchall()]

    def by_model(
        self, model_name: str, run_id: str | None = None
    ) -> list[ExperimentRecord]:
        if run_id is None:
            cur = self._conn.execute(
                "SELECT * FROM experiments WHERE model_name = ? ORDER BY created_at, record_id",
                (model_name,),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM experiments WHERE model_name = ? AND run_id = ? "
                "ORDER BY created_at, record_id",
                (model_name, run_id),
            )
        return [_row_to_record(row) for row in cur.fetchall()]

    def in_dim_region(
        self, bounds: dict[str, tuple[float, float]], run_id: str | None = None
    ) -> list[ExperimentRecord]:
        if run_id is None:
            cur = self._conn.execute("SELECT * FROM experiments")
        else:
            cur = self._conn.execute(
                "SELECT * FROM experiments WHERE run_id = ?", (run_id,)
            )
        records = [_row_to_record(row) for row in cur.fetchall()]
        out = []
        for record in records:
            ok = True
            for dim, (lo, hi) in bounds.items():
                value = record.realized_dims.get(dim)
                if value is None or not (lo <= value <= hi):
                    ok = False
                    break
            if ok:
                out.append(record)
        return out

    def lineage(self, record_id: str) -> list[ExperimentRecord]:
        chain: list[ExperimentRecord] = []
        current_id: str | None = record_id
        while current_id is not None:
            cur = self._conn.execute(
                "SELECT * FROM experiments WHERE record_id = ?", (current_id,)
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"no record with record_id={current_id!r}")
            record = _row_to_record(row)
            chain.append(record)
            current_id = record.parent_id
        chain.reverse()
        return chain

    def cost_summary(self, run_id: str | None = None) -> dict:
        if run_id is None:
            cur = self._conn.execute(
                "SELECT verdict, cost_usd, tokens_in, tokens_out FROM experiments"
            )
        else:
            cur = self._conn.execute(
                "SELECT verdict, cost_usd, tokens_in, tokens_out FROM experiments "
                "WHERE run_id = ?",
                (run_id,),
            )
        total_cost_usd = 0.0
        total_tokens_in = 0
        total_tokens_out = 0
        n_records = 0
        n_by_verdict: dict[str, int] = {}
        for row in cur.fetchall():
            total_cost_usd += row["cost_usd"]
            total_tokens_in += row["tokens_in"]
            total_tokens_out += row["tokens_out"]
            n_records += 1
            n_by_verdict[row["verdict"]] = n_by_verdict.get(row["verdict"], 0) + 1
        return {
            "total_cost_usd": total_cost_usd,
            "total_tokens_in": total_tokens_in,
            "total_tokens_out": total_tokens_out,
            "n_records": n_records,
            "n_by_verdict": n_by_verdict,
        }

    def export_jsonl(self, path: str | Path, run_id: str | None = None) -> None:
        records = self.by_run(run_id) if run_id is not None else self._all()
        with open(path, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record.to_json_dict()))
                f.write("\n")

    def _all(self) -> list[ExperimentRecord]:
        cur = self._conn.execute("SELECT * FROM experiments ORDER BY created_at, record_id")
        return [_row_to_record(row) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
