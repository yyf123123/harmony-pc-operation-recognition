"""SQLite metadata store. Raw files are never replaced by this class."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import QualityRecord, SessionRecord, TrialRecord

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS app_catalog (
  app_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  catalog_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS operation_catalog (
  operation_id TEXT PRIMARY KEY,
  app_id TEXT NOT NULL REFERENCES app_catalog(app_id),
  module TEXT NOT NULL,
  operation_family TEXT NOT NULL,
  display_name TEXT NOT NULL,
  catalog_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session (
  session_id TEXT PRIMARY KEY,
  app_id TEXT NOT NULL REFERENCES app_catalog(app_id),
  started_at TEXT NOT NULL,
  source TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS trial (
  trial_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES session(session_id),
  operation_id TEXT NOT NULL REFERENCES operation_catalog(operation_id),
  status TEXT NOT NULL,
  baseline_ts TEXT,
  action_start_ts TEXT,
  action_end_ts TEXT,
  post_ts TEXT,
  source TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0,
  state_json TEXT NOT NULL DEFAULT '{}',
  current_state TEXT NOT NULL DEFAULT 'SAVE',
  quality_status TEXT NOT NULL DEFAULT 'VALID',
  error_code TEXT,
  error_detail TEXT
);
CREATE TABLE IF NOT EXISTS event_log (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  trial_id TEXT NOT NULL REFERENCES trial(trial_id),
  phase TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  event_json TEXT NOT NULL,
  source TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS vma_snapshot (
  snapshot_id TEXT PRIMARY KEY,
  trial_id TEXT NOT NULL REFERENCES trial(trial_id),
  phase TEXT NOT NULL,
  snapshot_ts TEXT NOT NULL,
  raw_path TEXT,
  source TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS vma_feature (
  feature_id INTEGER PRIMARY KEY AUTOINCREMENT,
  trial_id TEXT NOT NULL REFERENCES trial(trial_id),
  feature_schema_version TEXT NOT NULL,
  derived_path TEXT NOT NULL,
  vector_dim INTEGER NOT NULL,
  source TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS quality_log (
  quality_id INTEGER PRIMARY KEY AUTOINCREMENT,
  trial_id TEXT NOT NULL REFERENCES trial(trial_id),
  valid INTEGER NOT NULL,
  signal_strength REAL,
  repeatability REAL,
  inter_class_margin REAL,
  valid_coverage REAL,
  reason TEXT,
  source TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0,
  quality_status TEXT NOT NULL DEFAULT 'VALID',
  error_code TEXT
);
CREATE TABLE IF NOT EXISTS eval_split (
  split_id INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id TEXT NOT NULL,
  eval_set TEXT NOT NULL,
  fold INTEGER NOT NULL,
  group_id TEXT NOT NULL,
  sample_count INTEGER NOT NULL,
  source TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS timing_log (
  timing_id INTEGER PRIMARY KEY AUTOINCREMENT,
  trial_id TEXT NOT NULL REFERENCES trial(trial_id),
  t_clear_refs REAL,
  t_device_sampling REAL,
  t_hdc_transfer REAL,
  t_parse REAL,
  t_feature REAL,
  t_clustering REAL,
  t_total REAL,
  source TEXT NOT NULL,
  is_mock INTEGER NOT NULL DEFAULT 0
);
"""


class DatabaseStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA_SQL)
        self._ensure_columns()
        self._ensure_feature_schema_uniqueness()
        self.connection.commit()

    def _ensure_columns(self) -> None:
        migrations = {
            "trial": {
                "current_state": "TEXT NOT NULL DEFAULT 'SAVE'",
                "quality_status": "TEXT NOT NULL DEFAULT 'VALID'",
                "error_code": "TEXT",
                "error_detail": "TEXT",
            },
            "quality_log": {
                "quality_status": "TEXT NOT NULL DEFAULT 'VALID'",
                "error_code": "TEXT",
            },
        }
        for table, columns in migrations.items():
            existing = {row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _ensure_feature_schema_uniqueness(self) -> None:
        """Prevent accidental replacement or duplicate evaluation of a schema."""
        duplicates = list(
            self.connection.execute(
                "SELECT trial_id, feature_schema_version, COUNT(*) AS copies "
                "FROM vma_feature GROUP BY trial_id, feature_schema_version HAVING COUNT(*) > 1"
            )
        )
        if duplicates:
            first = duplicates[0]
            raise RuntimeError(
                "vma_feature has duplicate schema rows; refusing to choose or overwrite one "
                f"(trial_id={first['trial_id']}, schema={first['feature_schema_version']})"
            )
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_vma_feature_trial_schema "
            "ON vma_feature(trial_id, feature_schema_version)"
        )

    def close(self) -> None:
        self.connection.close()

    def insert_catalog(self, catalog: Any) -> None:
        self.connection.execute(
            "INSERT INTO app_catalog(app_id,display_name,schema_version,catalog_json) VALUES(?,?,?,?) ON CONFLICT(app_id) DO UPDATE SET display_name=excluded.display_name,schema_version=excluded.schema_version,catalog_json=excluded.catalog_json",
            (catalog.app_id, catalog.display_name, catalog.schema_version, json.dumps({"app_id": catalog.app_id}, ensure_ascii=False)),
        )
        for operation in catalog.operations:
            self.connection.execute(
                "INSERT INTO operation_catalog(operation_id,app_id,module,operation_family,display_name,catalog_json) VALUES(?,?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET app_id=excluded.app_id,module=excluded.module,operation_family=excluded.operation_family,display_name=excluded.display_name,catalog_json=excluded.catalog_json",
                (operation.operation_id, operation.app_id, operation.module, operation.operation_family, operation.display_name, json.dumps(operation.to_mapping(), ensure_ascii=False)),
            )
        self.connection.commit()

    def insert_session(self, record: SessionRecord) -> None:
        self.connection.execute(
            "INSERT INTO session(session_id,app_id,started_at,source,is_mock,metadata_json) VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO NOTHING",
            (record.session_id, record.app_id, record.started_at, record.source, int(record.is_mock), json.dumps(dict(record.metadata), ensure_ascii=False)),
        )
        self.connection.commit()

    def insert_trial(self, record: TrialRecord) -> None:
        self.connection.execute(
            "INSERT INTO trial(trial_id,session_id,operation_id,status,baseline_ts,action_start_ts,action_end_ts,post_ts,source,is_mock,state_json,current_state,quality_status,error_code,error_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record.trial_id, record.session_id, record.operation_id, record.status, record.baseline_ts, record.action_start_ts, record.action_end_ts, record.post_ts, record.source, int(record.is_mock), json.dumps(dict(record.state), ensure_ascii=False), record.current_state, record.quality_status, record.error_code, record.error_detail),
        )
        self.connection.commit()

    def trial_exists(self, trial_id: str) -> bool:
        return self.connection.execute("SELECT 1 FROM trial WHERE trial_id=?", (trial_id,)).fetchone() is not None

    def mark_trial_failed(self, trial_id: str, *, error_code: str, error_detail: str, state: Mapping[str, Any]) -> None:
        self.connection.execute(
            "UPDATE trial SET status='failed',current_state='FAILED',quality_status='INVALID',error_code=?,error_detail=?,state_json=? WHERE trial_id=?",
            (error_code, error_detail, json.dumps(dict(state), ensure_ascii=False), trial_id),
        )
        self.connection.commit()

    def finalize_trial_success(
        self,
        trial_id: str,
        *,
        state: Mapping[str, Any],
        current_state: str = "SAVE",
        baseline_ts: str | None = None,
        action_start_ts: str | None = None,
        action_end_ts: str | None = None,
        post_ts: str | None = None,
    ) -> None:
        self.connection.execute(
            "UPDATE trial SET status='success',current_state=?,quality_status='VALID',error_code=NULL,error_detail=NULL,"
            "state_json=?,baseline_ts=COALESCE(?,baseline_ts),action_start_ts=COALESCE(?,action_start_ts),"
            "action_end_ts=COALESCE(?,action_end_ts),post_ts=COALESCE(?,post_ts) WHERE trial_id=?",
            (
                current_state,
                json.dumps(dict(state), ensure_ascii=False),
                baseline_ts,
                action_start_ts,
                action_end_ts,
                post_ts,
                trial_id,
            ),
        )
        self.connection.commit()

    def insert_event(self, trial_id: str, phase: str, event_ts: str, event: Mapping[str, Any], *, source: str, is_mock: bool) -> None:
        self.connection.execute(
            "INSERT INTO event_log(trial_id,phase,event_ts,event_json,source,is_mock) VALUES(?,?,?,?,?,?)",
            (trial_id, phase, event_ts, json.dumps(dict(event), ensure_ascii=False), source, int(is_mock)),
        )
        self.connection.commit()

    def insert_snapshot(self, snapshot_id: str, trial_id: str, phase: str, snapshot_ts: str, raw_path: str | None, *, source: str, is_mock: bool) -> None:
        self.connection.execute(
            "INSERT INTO vma_snapshot(snapshot_id,trial_id,phase,snapshot_ts,raw_path,source,is_mock) VALUES(?,?,?,?,?,?,?)",
            (snapshot_id, trial_id, phase, snapshot_ts, raw_path, source, int(is_mock)),
        )
        self.connection.commit()

    def insert_feature(self, trial_id: str, schema_version: str, derived_path: str, vector_dim: int, *, source: str, is_mock: bool) -> None:
        existing = self.connection.execute(
            "SELECT derived_path FROM vma_feature WHERE trial_id=? AND feature_schema_version=?",
            (trial_id, schema_version),
        ).fetchone()
        if existing is not None:
            raise ValueError(
                "feature schema is immutable for a trial; use a new trial or schema "
                f"(trial_id={trial_id}, schema={schema_version}, existing={existing['derived_path']})"
            )
        self.connection.execute(
            "INSERT INTO vma_feature(trial_id,feature_schema_version,derived_path,vector_dim,source,is_mock) VALUES(?,?,?,?,?,?)",
            (trial_id, schema_version, derived_path, vector_dim, source, int(is_mock)),
        )
        self.connection.commit()

    def insert_features(self, records: Iterable[tuple[str, str, str, int, str, bool]]) -> None:
        """Atomically register multiple immutable schema views of one trial."""
        values = list(records)
        keys = [(trial_id, schema_version) for trial_id, schema_version, *_ in values]
        if len(keys) != len(set(keys)):
            raise ValueError("one feature schema may be recorded only once per trial")
        for trial_id, schema_version, *_ in values:
            if self.connection.execute(
                "SELECT 1 FROM vma_feature WHERE trial_id=? AND feature_schema_version=?",
                (trial_id, schema_version),
            ).fetchone() is not None:
                raise ValueError(f"feature schema is immutable for a trial (trial_id={trial_id}, schema={schema_version})")
        try:
            with self.connection:
                self.connection.executemany(
                    "INSERT INTO vma_feature(trial_id,feature_schema_version,derived_path,vector_dim,source,is_mock) VALUES(?,?,?,?,?,?)",
                    [(trial_id, schema_version, path, vector_dim, source, int(is_mock)) for trial_id, schema_version, path, vector_dim, source, is_mock in values],
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("feature schema insert failed without partial registration") from error

    def insert_quality(self, record: QualityRecord) -> None:
        self.connection.execute(
            "INSERT INTO quality_log(trial_id,valid,signal_strength,repeatability,inter_class_margin,valid_coverage,reason,source,is_mock,quality_status,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (record.trial_id, int(record.valid), record.signal_strength, record.repeatability, record.inter_class_margin, record.valid_coverage, record.reason, record.source, int(record.is_mock), record.quality_status, record.error_code),
        )
        self.connection.commit()

    def insert_timing(self, trial_id: str, timing: Mapping[str, float | None], *, source: str, is_mock: bool) -> None:
        self.connection.execute(
            "INSERT INTO timing_log(trial_id,t_clear_refs,t_device_sampling,t_hdc_transfer,t_parse,t_feature,t_clustering,t_total,source,is_mock) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (trial_id, timing.get("T_clear_refs"), timing.get("T_device_sampling"), timing.get("T_hdc_transfer"), timing.get("T_parse"), timing.get("T_feature"), timing.get("T_clustering"), timing.get("T_total"), source, int(is_mock)),
        )
        self.connection.commit()

    def count(self, table: str, *, is_mock: bool | None = None) -> int:
        if table not in {"app_catalog", "operation_catalog", "session", "trial", "event_log", "vma_snapshot", "vma_feature", "quality_log", "eval_split", "timing_log"}:
            raise ValueError(table)
        query = f"SELECT COUNT(*) FROM {table}"
        params: tuple[Any, ...] = ()
        if is_mock is not None and table not in {"app_catalog", "operation_catalog"}:
            query += " WHERE is_mock = ?"
            params = (int(is_mock),)
        return int(self.connection.execute(query, params).fetchone()[0])

    def evaluation_rows(self, *, app_id: str | None = None, include_mock: bool = False) -> list[sqlite3.Row]:
        query = (
            "SELECT t.*, q.valid, q.quality_status AS quality_log_status "
            "FROM trial t JOIN quality_log q ON q.trial_id=t.trial_id "
            "WHERE t.status='success' AND t.quality_status='VALID' "
            "AND q.valid=1 AND q.quality_status='VALID'"
        )
        params: list[Any] = []
        if not include_mock:
            query += " AND t.is_mock=0 AND q.is_mock=0"
        if app_id is not None:
            query += " AND t.operation_id IN (SELECT operation_id FROM operation_catalog WHERE app_id=?)"
            params.append(app_id)
        return list(self.connection.execute(query, params).fetchall())
