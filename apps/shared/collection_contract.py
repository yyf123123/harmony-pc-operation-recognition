"""The explicit, application-neutral content contract for VMA collection.

The contract is deliberately metadata-only.  It describes what a new collector
must record without changing the feature layout of a frozen or existing data
set.  Each real HDC collector persists its contract alongside new sessions and
features, so later evaluators can reject incompatible populations safely.
"""

from __future__ import annotations

from dataclasses import dataclass

from features.feature_schema import (
    CROSS_APP_FEATURE_SCHEMA_VERSION,
    CROSS_APP_SIGNED_FEATURE_SCHEMA_VERSION,
    LEGACY_WPS_FEATURE_SCHEMA_VERSION,
)


COLLECTION_CONTRACT_SCHEMA_VERSION = "cross-app.vma.collection-contract.v1"
HDC_CAPTURE_PHASES = ("BASELINE_1", "BASELINE_2", "ACTION_WINDOW", "POST_0750", "POST_3000")
HDC_SESSION_CONTENT = (
    "target",
    "bundle",
    "process",
    "initial_pid",
    "operation_ids",
    "schedule_seed",
    "schedule",
    "control_operation_id",
    "control_trials",
    "capture_profile",
    "collection_contract",
)
HDC_TRIAL_CONTENT = (
    "session_id",
    "trial_id",
    "operation_id",
    "round",
    "schedule_position",
    "precondition_confirmation",
    "action_confirmation",
    "capture_profile",
    "timing",
    "validity_or_failure_reason",
)


@dataclass(frozen=True)
class CollectionContract:
    """Stable description of one application's collection boundary."""

    app_id: str
    collection_mode: str
    feature_schema_versions: tuple[str, ...]
    capture_profile_family: str
    capture_phases: tuple[str, ...]
    session_content: tuple[str, ...]
    trial_content: tuple[str, ...]
    requires_observable_precondition: bool
    requires_observable_action_confirmation: bool
    requires_balanced_schedule: bool
    requires_settled_noop_control: bool
    retains_invalid_trials: bool
    excludes_mock_from_formal_evaluation: bool
    frozen_baseline: bool = False
    notes: str = ""

    def to_metadata(self) -> dict[str, object]:
        return {
            "schema_version": COLLECTION_CONTRACT_SCHEMA_VERSION,
            "app_id": self.app_id,
            "collection_mode": self.collection_mode,
            "feature_schema_versions": list(self.feature_schema_versions),
            "capture_profile_family": self.capture_profile_family,
            "capture_phases": list(self.capture_phases),
            "session_content": list(self.session_content),
            "trial_content": list(self.trial_content),
            "requires_observable_precondition": self.requires_observable_precondition,
            "requires_observable_action_confirmation": self.requires_observable_action_confirmation,
            "requires_balanced_schedule": self.requires_balanced_schedule,
            "requires_settled_noop_control": self.requires_settled_noop_control,
            "retains_invalid_trials": self.retains_invalid_trials,
            "excludes_mock_from_formal_evaluation": self.excludes_mock_from_formal_evaluation,
            "frozen_baseline": self.frozen_baseline,
            "notes": self.notes,
        }


def cross_app_hdc_contract(app_id: str) -> CollectionContract:
    """Return the common real-device contract used by expandable HDC apps."""
    if not app_id:
        raise ValueError("app_id is required for a collection contract")
    return CollectionContract(
        app_id=app_id,
        collection_mode="real_hdc",
        feature_schema_versions=(CROSS_APP_FEATURE_SCHEMA_VERSION, CROSS_APP_SIGNED_FEATURE_SCHEMA_VERSION),
        capture_profile_family="cross_app.reference_windows",
        capture_phases=HDC_CAPTURE_PHASES,
        session_content=HDC_SESSION_CONTENT,
        trial_content=HDC_TRIAL_CONTENT,
        requires_observable_precondition=True,
        requires_observable_action_confirmation=True,
        requires_balanced_schedule=True,
        requires_settled_noop_control=True,
        retains_invalid_trials=True,
        excludes_mock_from_formal_evaluation=True,
        notes="Only device-confirmed HDC trials are eligible for formal evaluation; mock and invalid trials remain retained but excluded.",
    )


def frozen_wps_contract() -> CollectionContract:
    """Describe WPS without implying that its frozen legacy baseline is HDC data."""
    return CollectionContract(
        app_id="wps",
        collection_mode="legacy_frozen",
        feature_schema_versions=(LEGACY_WPS_FEATURE_SCHEMA_VERSION,),
        capture_profile_family="wps.legacy.frozen.v1",
        capture_phases=(),
        session_content=("baseline_manifest", "operation_catalog", "raw_hashes"),
        trial_content=("legacy_trial_id", "operation_label", "sample_id"),
        requires_observable_precondition=False,
        requires_observable_action_confirmation=False,
        requires_balanced_schedule=False,
        requires_settled_noop_control=False,
        retains_invalid_trials=False,
        excludes_mock_from_formal_evaluation=True,
        frozen_baseline=True,
        notes="Compatibility facade only: do not mutate or relabel the frozen WPS raw data, feature schema, or baseline evaluation.",
    )
