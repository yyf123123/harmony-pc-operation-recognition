# Dataset schema

The phase uses SQLite metadata plus immutable raw files and replaceable derived
files.

```text
app_catalog -> operation_catalog
app_catalog -> session -> trial -> event_log
trial -> vma_snapshot -> vma_feature -> quality_log
trial -> timing_log
```

Raw snapshots and event/session JSON are written once under `data/raw/`.
Derived snapshots, features, and quality records live under `data/derived/`.
`feature_schema_version` is recorded in both the feature JSON and SQLite.

The `is_mock` and `source` columns are mandatory provenance gates. Evaluation
commands exclude `is_mock=1` by default; mock records are for pipeline testing,
not scientific results.
# Feature and validity invariants

`wps.vma.feature.v1` and `cross_app.vma.feature.v2` are frozen and are never
silently changed. v2 is the 1,024 FILE + 1,024 ANON Referenced-only hashed
layout. New Browser/Bilibili trials dual-write v2 and
`cross_app.vma.feature.v3_signed`: 8,192 dimensions spanning Referenced/RSS,
growth/reclamation, and FILE/ANON blocks with 1,024 stable-hash slots each.
No schema uses process name, PID, module path, VMA count, fixed ordering, or a
WPS-specific mapping rule. Evaluation selects exactly one schema and one
capture profile per run.

Only a trial that reaches `SAVE` through
`PREPARE -> PRECONDITION_CHECK -> BASELINE -> ACTION -> ACTION_CONFIRM ->
POST_ACTION -> QUALITY_CHECK` may have `quality_status=VALID`. Failed trials
are retained with an error code but are excluded from evaluation by default;
mock rows are also excluded.
