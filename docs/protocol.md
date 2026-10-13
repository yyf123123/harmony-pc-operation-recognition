# Cross-app fixed-window protocol

Every trial follows:

```text
PREPARE -> PRECONDITION_CHECK -> BASELINE -> ACTION -> ACTION_CONFIRM -> POST_ACTION -> QUALITY_CHECK -> SAVE
```

`quality_status=VALID` is possible only after this entire path completes.
Failures retain their raw evidence and an INVALID database record; evaluation
excludes INVALID and mock rows by default.

## Reference-window capture profile

`cross_app.reference_windows.v1` consists of `BASELINE_1` and `BASELINE_2`
(750 ms each), an `ACTION_WINDOW` armed with `clear_refs` before the action,
then `POST_0750` and `POST_3000`. There is deliberately no `clear_refs`
between action completion/action confirmation and `ACTION_WINDOW` capture.
The capture profile, each window duration, action duration, HDC timing fields,
and randomized schedule position are stored in session/trial provenance.

## Feature schemas

`wps.vma.feature.v1` and `cross_app.vma.feature.v2` are frozen. v2 remains a
2,048-D Referenced-only FILE/ANON transform for reproducibility. New trials
dual-write v2 and `cross_app.vma.feature.v3_signed`; v3 is 8,192-D and has
independent 1,024-slot FILE/ANON blocks for Referenced/RSS × growth/reclamation.
Schema and capture-profile views are never mixed in an analysis. A trial can
have one immutable derived feature per schema, never a replacement row.

Timing fields are independent: `T_clear_refs`, `T_device_sampling`,
`T_hdc_transfer`, `T_parse`, `T_feature`, `T_clustering`, and `T_total`.
Mock timings have `source=mock` and `is_mock=true`.
