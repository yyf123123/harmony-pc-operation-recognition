"""Finite external-automation batch orchestration; no new recognition algorithm.

The public CLI uses the frozen window profile and real LinuxProcfs. The injected
fixture runner exists for tests only and marks every result synthetic. A passed
preflight means the supplied semantic verifiers and complete capture passed;
it is not independent certification of the external verifier or model accuracy.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager, ExitStack
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import stat
import tempfile
import time
import platform

from apps.wps.adapter import WpsAdapter
from collector.linux.history import event_from_prediction
from collector.linux.procfs import CaptureError, BatchProcfs
from collector.linux.session import capture_sample, run_command, validate_plan
from scripts.linux import export_sample

try:
    import fcntl
except ImportError:
    fcntl = None

SCHEMA = "linux.wps.batch.v1"
OPS = tuple(op.operation_id for op in WpsAdapter().catalog.operations)
PROFILE = {"baseline_s": 5, "action_s": 15, "post_s": 5, "command_timeout_s": 15}
ROLES = {"prepare_independent", "begin_session", "precondition", "action", "verify", "focus"}
TOKENS = {"document_path", "document_id", "operation_id", "state_dir", "session_id", "trial_id"}
TOKEN = re.compile(r"\{([a-z_]+)\}")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def file_sha(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def atomic_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict) or config.get("schema_version") != SCHEMA:
        raise ValueError("unsupported batch schema")
    for key in ("batch_id", "app_version", "device_id"):
        if not isinstance(config.get(key), str) or not config[key].strip() or config[key].startswith("FILL_"):
            raise ValueError(f"{key} is required")
    pids = config.get("pids")
    if (not isinstance(pids, list) or not pids or any(type(pid) is not int or pid <= 0 for pid in pids)
            or len(pids) != len(set(pids))):
        raise ValueError("unique explicit positive PIDs are required")
    if type(config.get("root_pid")) is not int or config["root_pid"] not in pids:
        raise ValueError("explicit root_pid must be one of the selected PIDs")
    if type(config.get("seed", 0)) is not int or type(config.get("max_attempts", 3)) is not int or not 1 <= config.get("max_attempts", 3) <= 3:
        raise ValueError("integer seed and max_attempts in 1..3 required")
    documents = config.get("documents")
    if not isinstance(documents, list) or len(documents) != 5:
        raise ValueError("five document templates are required")
    for document in documents:
        if not isinstance(document, dict) or document.get("test_copy") is not True:
            raise ValueError("each document must be explicitly confirmed a test copy")
        for key in ("document_id", "family_id", "path"):
            if not isinstance(document.get(key), str) or not document[key].strip():
                raise ValueError("document ID, family and path are required")
    for key in ("document_id", "family_id", "path"):
        if len({d[key] for d in documents}) != 5:
            raise ValueError("document IDs, families and paths must be distinct")
    if Counter(d.get("split") for d in documents) != {"train": 3, "validation": 1, "test": 1}:
        raise ValueError("freeze three train, one validation, one test document families")
    commands = config.get("automation")
    if not isinstance(commands, dict) or set(commands) != ROLES:
        raise ValueError("six explicit automation argv roles are required")
    for role, argv in commands.items():
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in argv):
            raise ValueError(f"{role} must be an argv array, never a shell string")
        for arg in argv:
            if set(TOKEN.findall(arg)) - TOKENS:
                raise ValueError("unknown automation placeholder")
        if TOKEN.search(argv[0]):
            raise ValueError("automation executable cannot depend on a sample placeholder")
    workflows = config.get("workflows")
    if not isinstance(workflows, list) or len(workflows) != 6:
        raise ValueError("six explicit workflows are required")
    for workflow in workflows:
        if (not isinstance(workflow, dict) or not isinstance(workflow.get("workflow_id"), str)
                or not workflow["workflow_id"] or not isinstance(workflow.get("operations"), list)
                or len(workflow["operations"]) != 20 or any(op not in OPS for op in workflow["operations"])):
            raise ValueError("each workflow requires an ID and twenty catalog operations")
    if len({w["workflow_id"] for w in workflows}) != 6 or len({tuple(w["operations"]) for w in workflows}) != 6:
        raise ValueError("workflow IDs and action orders must differ")
    evidence = config.get("automation_files")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(p, str) or not p for p in evidence):
        raise ValueError("declare automation source files for hash binding")
    return config


def make_schedule(config: dict) -> list[dict]:
    validate_config(config)
    result = []
    def unit(stage, subset, doc, number, operations):
        key = [config["batch_id"], stage, subset, doc["document_id"], number, operations]
        result.append({"unit_id": digest(key)[:24], "stage": stage, "subset": subset,
                       "document_id": doc["document_id"], "family_id": doc["family_id"],
                       "split": "preflight" if stage == "preflight" else doc["split"],
                       "group_index": number, "operations": operations})
    # Pilot documents come only from train; evaluation documents do not calibrate adapters.
    train = [d for d in config["documents"] if d["split"] == "train"]
    for number in range(2):
        for op in OPS:
            unit("preflight", "independent", train[number % len(train)], number, [op])
    for document in config["documents"]:
        for number in range(10):
            order = list(OPS)
            random.Random(f"{config.get('seed', 0)}:{document['document_id']}:{number}").shuffle(order)
            for op in order:
                unit("formal", "independent", document, number, [op])
        for number, workflow in enumerate(config["workflows"]):
            unit("formal", "continuous", document, number, workflow["operations"])
    if len({item["unit_id"] for item in result}) != len(result):
        raise ValueError("duplicate batch unit identity")
    return result


def expanded(argv, context):
    return [TOKEN.sub(lambda match: str(context[match[1]]), arg) for arg in argv]


@contextmanager
def exclusive_run(root):
    if fcntl is None:
        raise RuntimeError("batch execution requires POSIX file locking")
    with (root / ".batch.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another runner owns this batch") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def diagnostic_guard(root, resume):
    try:
        yield
    except (Exception, KeyboardInterrupt) as error:
        path = root / "report.json"
        previous = {}
        if path.exists():
            previous = json.loads(path.read_text())
            shutil.copy2(path, root / f"report-before-failure-{time.monotonic_ns()}.json")
        atomic_json(path, {**previous, "schema_version": "linux.wps.batch-report.v1",
                         "status": "resume_validation_failed" if resume else "initialization_failed",
                         "error_type": type(error).__name__, "dataset_ready": False,
                         "formal_collection_enabled": False})
        raise


class RealCapture:
    """Thin data-preparation adapter. Classification remains in the Native CLI."""
    is_mock = False

    def __init__(self, config, repo):
        self.config, self.repo = config, repo
        build = Path(config["build_dir"]).expanduser().resolve()
        self.backend = BatchProcfs(config["pids"], build / "collector/linux/linux_vma_collect", root_pid=config["root_pid"])
        self.native = build / "native/operation_predict"
        self.feature = build / "native/operation_feature"
        for path in (self.native, self.feature):
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError("build and selftest Native targets before batch execution")
        self.inputs = {str(path): file_sha(path) for path in (self.backend.collector, self.native, self.feature)}
        for path in (Path(__file__), Path(__file__).with_name("session.py"), Path(__file__).with_name("procfs.py"),
                     Path(__file__).with_name("history.py"), repo / "scripts/linux.py",
                     repo / "features/wps.py", repo / "apps/wps/catalog.json"):
            self.inputs[str(path.resolve())] = file_sha(path)
        for path in config["automation_files"]:
            source = Path(path).expanduser().resolve(strict=True)
            self.inputs[str(source)] = file_sha(source)
        for argv in config["automation"].values():
            executable = shutil.which(argv[0])
            if not executable:
                raise ValueError("automation executable is unavailable")
            self.inputs[executable] = file_sha(Path(executable))
            for argument in argv[1:]:
                if not TOKEN.search(argument) and Path(argument).expanduser().is_file():
                    source = Path(argument).expanduser().resolve()
                    self.inputs[str(source)] = file_sha(source)
        for document in config["documents"]:
            source = Path(document["path"]).expanduser().resolve(strict=True)
            if not source.is_file() or str(source) != document["path"]:
                raise ValueError("test template paths must be absolute regular files")
            self.inputs[str(source)] = file_sha(source)
        self.binding = {"inputs": self.inputs,
                        "processes": [{"pid": item.pid, "start_ticks": item.start_ticks} for item in self.backend.identities],
                        "python": platform.python_version(),
                        "clock_domain_sha256": file_sha(Path("/proc/sys/kernel/random/boot_id")),
                        "page_size_bytes": os.sysconf("SC_PAGE_SIZE")}
        self.check()

    def check(self):
        self.backend.check()
        for path, expected in self.inputs.items():
            if file_sha(Path(path)) != expected:
                raise CaptureError("bound automation, template or Native input changed; start a new batch")

    @contextmanager
    def claim_targets(self, lock_root=None):
        directory = Path(lock_root or Path(tempfile.gettempdir())/f"harmony-ops-{os.getuid()}")
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CaptureError("process lock directory must be private and owned by the current user")
        with ExitStack() as locks:
            for identity in self.backend.identities:
                path = directory/f"{identity.pid}-{identity.start_ticks}.lock"
                fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
                handle = locks.enter_context(os.fdopen(fd, "r+"))
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise CaptureError("another batch owns a selected process; no clear_refs performed") from None
            self.check()
            yield

    def prepare(self, role, context):
        self.check()
        run_command(expanded(self.config["automation"][role], context), 30, role)
        self.backend.check()

    def capture(self, unit, context, output):
        self.check()
        plan = {"schema_version": "linux.wps.action-plan.v1", "operation_id": context["operation_id"], **PROFILE,
                **{role: expanded(self.config["automation"][role], context) for role in ("precondition", "action", "verify", "focus")}}
        validate_plan(plan)
        sample = capture_sample(plan=plan, backend=self.backend, output=output,
            session_id=context["session_id"], trial_id=context["trial_id"], app_version=self.config["app_version"],
            device_id=self.config["device_id"], collection_stage=unit["stage"],
            capture_context={**{key: unit[key] for key in ("unit_id", "document_id", "family_id", "split", "subset", "group_index")},
                             "sequence_index": context["sequence_index"], "sequence_kind": "scripted_controlled_sequence" if unit["subset"] == "continuous" else "independent_operation"})
        export = output / "export"
        export_sample(sample, export)
        # Construct the actual classifier input using the shared Native math.
        self._native([self.feature, "--schema", "wps.vma.feature.v1", "--sample-id", sample["sample_id"],
            "--input", export / "aggregated_features.csv", "--output", export / "native_vectors.csv"])
        with (export / "vma_vectors_l2.csv").open(newline="") as handle:
            reference = next(csv.DictReader(handle))
        with (export / "native_vectors.csv").open(newline="") as handle:
            native = next(csv.DictReader(handle))
        if max(abs(float(reference[f"v{i:04d}"]) - float(native[f"v{i:04d}"])) for i in range(2048)) > 1e-9:
            raise CaptureError("Native/Python feature parity failed")
        self._native([self.native, "--models", self.repo / "models", "--app", "wps", "--schema", "wps.vma.feature.v1",
            "--vectors", export / "native_vectors.csv", "--output", output / "prediction.csv"])
        with (output / "prediction.csv").open(newline="") as handle:
            predictions = list(csv.DictReader(handle))
        if len(predictions) != 1:
            raise CaptureError("Native predictor must return one observation")
        atomic_json(output / "recognition.json", event_from_prediction(sample, predictions[0]))
        return sample

    @staticmethod
    def _native(argv):
        try:
            result = subprocess.run([str(a) for a in argv], capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise CaptureError("Native data preparation failed or timed out") from None
        if result.returncode:
            raise CaptureError("Native data preparation failed")


def _relative_file(root, name):
    if not isinstance(name, str) or Path(name).is_absolute():
        raise ValueError("receipt path must be relative")
    path = (root / name).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("receipt escapes batch directory")
    return path


def verify_receipt(root, receipt, fixture_mode):
    hashes = receipt["files"]
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("empty receipt")
    for name, expected in hashes.items():
        if file_sha(_relative_file(root, name)) != expected:
            raise ValueError("saved sample evidence was modified; resume refused")
    samples = []
    for name in receipt["samples"]:
        if name not in hashes:
            raise ValueError("sample JSON is not hash-bound")
        sample = json.loads(_relative_file(root, name).read_text())
        if sample.get("status") != "success" or sample.get("is_mock") is not fixture_mode:
            raise ValueError("invalid or mixed synthetic sample")
        if (sample.get("feature_schema") != "wps.vma.feature.v1" or sample.get("collection_stage") != receipt["stage"]
                or sample.get("capture_context", {}).get("unit_id") != receipt["unit_id"]):
            raise ValueError("sample provenance differs from its scheduled unit")
        samples.append(sample)
    return samples


def run_batch(config, output: Path, *, resume=False, capture=None, repo=None, fixture_mode=False):
    """Serial bounded execution, resumable only with identical bound inputs/PIDs.

    Continuous sessions commit as a whole: a failed/interrupted prefix is retained
    as diagnostic evidence, never resumed mid-document or counted as a clean flow.
    """
    config = validate_config(config)
    schedule = make_schedule(config)
    root = Path(output).expanduser().resolve()
    if resume:
        if not root.is_dir():
            raise ValueError("resume requires an existing batch directory")
    else:
        root.mkdir(parents=True, exist_ok=False)
    with exclusive_run(root), diagnostic_guard(root, resume), ExitStack() as lifecycle:
        repo = Path(repo or Path(__file__).resolve().parents[2]).resolve()
        if capture is not None and not fixture_mode:
            raise ValueError("injected capture requires explicit fixture mode")
        engine = capture or RealCapture(config, repo)
        if bool(engine.is_mock) != fixture_mode:
            raise ValueError("mixed synthetic/real batch engine")
        if not fixture_mode:
            lifecycle.enter_context(engine.claim_targets())
        binding = {"schema": SCHEMA, "config_sha256": digest(config), "inputs": engine.binding, "fixture_mode": fixture_mode}
        if resume:
            if json.loads((root / "binding.json").read_text()) != binding:
                raise ValueError("batch configuration, input hashes or process identities changed")
            if json.loads((root / "config.json").read_text()) != config or json.loads((root / "schedule.json").read_text()) != schedule:
                raise ValueError("frozen configuration or schedule was modified")
        else:
            atomic_json(root / "binding.json", binding)
            atomic_json(root / "config.json", config)
            atomic_json(root / "schedule.json", schedule)
        planned = {u["unit_id"]: u for u in schedule}
        successes, attempts, terminal = {}, Counter(), set()
        journal = root / "journal.jsonl"
        def append(record):
            with journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                handle.flush(); os.fsync(handle.fileno())
        if journal.exists():
            data = journal.read_bytes()
            if data and not data.endswith(b"\n"):
                raise ValueError("truncated journal; retain evidence and start a new run")
            for line in data.splitlines():
                record = json.loads(line)
                uid = record["unit_id"]
                if uid not in planned:
                    raise ValueError("journal unit not present in frozen schedule")
                if record["event"] == "started":
                    if uid in successes or record["attempt"] != attempts[uid] + 1:
                        raise ValueError("inconsistent attempt journal")
                    if planned[uid]["stage"] == "formal" and sum(planned[key]["stage"] == "preflight" for key in successes) != 36:
                        raise ValueError("formal journal began without a complete preflight")
                    attempts[uid] += 1
                elif record["event"] in {"passed", "failed", "abandoned"}:
                    key = (uid, record["attempt"])
                    if record["attempt"] != attempts[uid] or key in terminal:
                        raise ValueError("inconsistent terminal journal")
                    terminal.add(key)
                    if record["event"] == "passed":
                        if record.get("stage") != planned[uid]["stage"]:
                            raise ValueError("receipt stage differs from frozen schedule")
                        samples = verify_receipt(root, record, fixture_mode)
                        if [sample["operation_id"] for sample in samples] != planned[uid]["operations"]:
                            raise ValueError("receipt operations differ from frozen schedule")
                        successes[uid] = record
                else:
                    raise ValueError("unknown journal event")
        for uid, count in attempts.items():
            if (uid, count) not in terminal:
                append({"event": "abandoned", "unit_id": uid, "attempt": count, "reason": "interrupted_attempt"})
        report = {"schema_version": "linux.wps.batch-report.v1", "status": "running", "is_mock": fixture_mode,
                  "validation_scope": "synthetic_fixture_only" if fixture_mode else "external_verifier_controlled_capture",
                  "preflight_passed": False, "formal_collection_enabled": False, "dataset_ready": False,
                  "target_windows": {"preflight": 36, "independent": 900, "continuous": 600},
                  "completed_units": len(successes), "failed_attempts": sum(attempts.values()) - len(successes)}
        def save_report():
            counts = Counter()
            labels = Counter()
            splits = Counter()
            for uid in successes:
                unit = planned[uid]
                counts["preflight" if unit["stage"] == "preflight" else unit["subset"]] += len(unit["operations"])
                if unit["stage"] == "formal":
                    splits[unit["split"]] += len(unit["operations"])
                    if unit["subset"] == "independent": labels.update(unit["operations"])
            report.update(valid_windows=dict(counts), independent_per_operation=dict(labels), formal_split_counts=dict(splits),
                          completed_units=len(successes), failed_attempts=sum(attempts.values())-len(successes),
                          preflight_passed=counts["preflight"] == 36,
                          formal_collection_enabled=counts["preflight"] == 36 and not fixture_mode
                          and report.get("disk_estimate", {}).get("passed", True))
            atomic_json(root / "report.json", report)
        save_report()
        documents = {d["document_id"]: d for d in config["documents"]}
        space_checked = False
        try:
            for unit in schedule:
                uid = unit["unit_id"]
                if uid in successes:
                    continue
                if unit["stage"] == "formal" and not report["preflight_passed"]:
                    raise CaptureError("full-class preflight is incomplete; formal capture refused")
                if unit["stage"] == "formal" and not space_checked:
                    sizes = [sum((root/name).stat().st_size for name in value["files"]) / len(value["samples"])
                             for key, value in successes.items() if planned[key]["stage"] == "preflight"]
                    remaining = 1500 - sum(len(planned[key]["operations"]) for key in successes if planned[key]["stage"] == "formal")
                    required = math.ceil(max(sizes) * remaining * 2)
                    free = shutil.disk_usage(root).free
                    report["disk_estimate"] = {"required_bytes": required, "free_bytes": free,
                                               "basis": "twice_max_pilot_bytes_per_window_times_remaining", "passed": free >= required}
                    save_report()
                    if free < required:
                        raise CaptureError("insufficient free space for estimated remaining batch; formal capture paused")
                    space_checked = True
                while uid not in successes:
                    if attempts[uid] >= config.get("max_attempts", 3):
                        raise CaptureError("attempt limit reached; retain failures and repair before a new run")
                    number = attempts[uid] + 1
                    attempt_dir = root / "attempts" / uid / f"try-{number:03d}"
                    attempt_dir.mkdir(parents=True, exist_ok=False)
                    attempts[uid] = number
                    append({"event": "started", "unit_id": uid, "attempt": number})
                    report["active_unit"] = uid
                    started = time.monotonic_ns()
                    document = documents[unit["document_id"]]
                    session_id = digest([config["batch_id"], uid, number])[:24] if unit["subset"] == "continuous" else digest([config["batch_id"], unit["stage"], unit["document_id"], unit["group_index"]])[:24]
                    context = {"document_path": document["path"], "document_id": document["document_id"],
                               "session_id": session_id, "trial_id": session_id,
                               "state_dir": str(attempt_dir / "automation-state"), "operation_id": unit["operations"][0]}
                    Path(context["state_dir"]).mkdir()
                    paths = []
                    try:
                        engine.prepare("begin_session" if unit["subset"] == "continuous" else "prepare_independent", context)
                        for index, operation in enumerate(unit["operations"]):
                            context.update(operation_id=operation, sequence_index=index)
                            report["active_sequence_index"] = index
                            save_report()
                            sample_dir = attempt_dir / f"sample-{index:03d}"
                            sample = engine.capture(unit, context, sample_dir)
                            if sample.get("status") != "success" or sample.get("is_mock") is not fixture_mode:
                                raise CaptureError("capture did not produce a valid matching-source sample")
                            paths.append(sample_dir / "derived/sample.json")
                        instances = {json.loads(p.read_text())["app_instance_id"] for p in paths}
                        if len(instances) != 1:
                            raise CaptureError("application instance changed inside a session")
                        # The driver's mutable working copies are not observation
                        # evidence. Bind only completed capture/Native artifacts;
                        # scripts, templates and arguments are separately bound.
                        files = {str(p.relative_to(root)): file_sha(p) for p in sorted(attempt_dir.rglob("*"))
                                 if p.is_file() and not p.is_relative_to(Path(context["state_dir"]))}
                        record = {"event": "passed", "unit_id": uid, "attempt": number, "stage": unit["stage"],
                                  "samples": [str(p.relative_to(root)) for p in paths], "files": files,
                                  "elapsed_ns": time.monotonic_ns()-started}
                        verify_receipt(root, record, fixture_mode)
                        if [json.loads(path.read_text())["operation_id"] for path in paths] != unit["operations"]:
                            raise CaptureError("capture operation does not match scheduled truth")
                        append(record)
                        successes[uid] = record
                    except (Exception, KeyboardInterrupt) as error:
                        append({"event": "failed", "unit_id": uid, "attempt": number,
                                "error_type": type(error).__name__, "reason": str(error) if isinstance(error, CaptureError) else "capture_or_output_failure"})
                        save_report()
                        # Pilot failures pause immediately. Formal failures may be
                        # retried with a fresh preparation and bounded attempts;
                        # continuous sessions restart in full, never at a suffix.
                        if (isinstance(error, CaptureError) and unit["stage"] == "formal"
                                and number < config.get("max_attempts", 3)):
                            continue
                        raise
                    save_report()
            report.update(status="fixture_passed" if fixture_mode else "passed", dataset_ready=not fixture_mode)
            report.pop("active_unit", None); report.pop("active_sequence_index", None)
            atomic_json(root / "accepted.json", {"schema_version": "linux.wps.batch-index.v1", "is_mock": fixture_mode,
                        "units": [successes[unit["unit_id"]] for unit in schedule]})
            save_report()
        except (Exception, KeyboardInterrupt) as error:
            report.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                          error_type=type(error).__name__, dataset_ready=False,
                          reason=str(error) if isinstance(error, CaptureError) else "inspect journal and preserved attempt")
            save_report()
        return report
