"""Replay recognized events through versioned next-operation/page adapters."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.wps.adapter import WpsAdapter
from collector.linux.history import OperationHistory, event_from_native_runtime
from collector.linux.next_operation import MarkovArtifact, predict_or_not_ready
from collector.linux.page_mapping import PageMapping, candidates_or_unknown


def replay_events(events: list[dict], *, model=None, mapping=None, app_version: str = "",
                  observed_file_identities: list[dict] | None = None, page_size_bytes: int = 4096,
                  clock_domain: str = "", allowed_methods: tuple[str, ...] = ("native_class_centroid_cosine",),
                  allow_fixture: bool = False) -> list[dict]:
    """One replay file contains one session/application instance; changes need a new run.

    Input is recognition output only. Predicted candidates are never appended to
    history. No truth labels, automation requests or page labels are consulted.
    """
    if not events:
        raise ValueError("no recognition events")
    events = [event_from_native_runtime(event) if event.get("schema") == "harmony.ops.recognized.v1" else event
              for event in events]
    history = OperationHistory()
    history.reset(events[0]["session_id"], events[0]["app_instance_id"])
    catalog = WpsAdapter().catalog
    operation_ids = {operation.operation_id for operation in catalog.operations}
    rows = []
    for event in events:
        if (event.get("schema_version") != "linux.operation-event.v1" or event.get("app_id") != "wps"
                or event.get("method") not in allowed_methods or event.get("operation_id") not in operation_ids):
            raise ValueError("unconfigured recognition method, schema, application or operation")
        if bool(event.get("is_mock")) and not allow_fixture:
            raise ValueError("mock events require --allow-fixture")
        if not history.append(event):
            continue
        request = history.prediction_request()
        prediction = predict_or_not_ready(model, request, allow_fixture=allow_fixture)
        pages = candidates_or_unknown(mapping, prediction, app_version=app_version,
            label_schema_version=catalog.schema_version,
            observed_file_identities=observed_file_identities or [], page_size_bytes=page_size_bytes,
            clock_domain=clock_domain, allow_fixture=allow_fixture)
        rows.append({"schema_version": "linux.followup-replay.v1", "after_event_id": event["event_id"],
                     "request": request, "prediction": prediction, "pages": pages,
                     "validation_scope": "offline_replay_only"})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-model")
    validate.add_argument("--model", type=Path, required=True)
    replay = commands.add_parser("replay-events")
    replay.add_argument("--events", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument("--model", type=Path)
    replay.add_argument("--mapping", type=Path)
    replay.add_argument("--file-identities", type=Path, help="JSON list of explicitly verified file SHA-256/size identities")
    replay.add_argument("--app-version", default="")
    replay.add_argument("--page-size", type=int, default=4096, help="actual target page size recorded with replay inputs")
    replay.add_argument("--clock-domain", default="")
    replay.add_argument("--allow-method", action="append", default=["native_class_centroid_cosine"])
    replay.add_argument("--allow-fixture", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "validate-model":
        model = MarkovArtifact.load(args.model)
        print(json.dumps({"status": "valid", "model_sha256": model.sha256,
                          "source_kind": model.payload["training_provenance"]["source_kind"]}))
        return 0
    if args.output.exists():
        raise FileExistsError("output must be a new file")
    events = [json.loads(line) for line in args.events.read_text(encoding="utf-8").splitlines() if line.strip()]
    model = MarkovArtifact.load(args.model) if args.model else None
    mapping = PageMapping.load(args.mapping) if args.mapping else None
    if mapping and (not args.app_version or not args.clock_domain):
        raise ValueError("mapping requires --app-version and --clock-domain")
    identities = json.loads(args.file_identities.read_text(encoding="utf-8")) if args.file_identities else []
    rows = replay_events(events, model=model, mapping=mapping, app_version=args.app_version,
                         observed_file_identities=identities, page_size_bytes=args.page_size,
                         clock_domain=args.clock_domain, allowed_methods=tuple(args.allow_method),
                         allow_fixture=args.allow_fixture)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps({"status": "replayed", "recognized_events": len(rows),
                      "scope": "offline_replay_only", "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise SystemExit(f"Linux follow-up: {error}")
