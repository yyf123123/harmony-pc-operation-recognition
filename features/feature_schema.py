from dataclasses import dataclass


# v1 is frozen for exact WPS baseline reproduction.  New collection uses the
# same mathematical layout under an explicitly cross-app schema identifier.
LEGACY_WPS_FEATURE_SCHEMA_VERSION = "wps.vma.feature.v1"
CROSS_APP_FEATURE_SCHEMA_VERSION = "cross_app.vma.feature.v2"
CROSS_APP_SIGNED_FEATURE_SCHEMA_VERSION = "cross_app.vma.feature.v3_signed"
FEATURE_SCHEMA_VERSION = CROSS_APP_FEATURE_SCHEMA_VERSION
VECTOR_DIM = 2048
FILE_SLOTS = 1024
ANON_SLOTS = 1024
SIGNED_VECTOR_DIM = 8192
SIGNED_CHANNEL_COUNT = 4


@dataclass(frozen=True)
class FeatureSchema:
    version: str = FEATURE_SCHEMA_VERSION
    vector_dim: int = VECTOR_DIM
    file_slots: int = FILE_SLOTS
    anon_slots: int = ANON_SLOTS
    transform: str = "baseline_median;max(action_excess,post_excess);log1p;l2"
    channel_count: int = 1

    def validate(self) -> None:
        if (self.file_slots + self.anon_slots) * self.channel_count != self.vector_dim:
            raise ValueError("FILE and ANON slots across channels must sum to vector_dim")


def schema_for(version: str) -> FeatureSchema:
    """Return the immutable layout for a known feature schema version."""
    if version in {LEGACY_WPS_FEATURE_SCHEMA_VERSION, CROSS_APP_FEATURE_SCHEMA_VERSION}:
        return FeatureSchema(version=version)
    if version == CROSS_APP_SIGNED_FEATURE_SCHEMA_VERSION:
        return FeatureSchema(
            version=version,
            vector_dim=SIGNED_VECTOR_DIM,
            transform="baseline_median;referenced_and_rss;positive_and_negative;log1p;l2",
            channel_count=SIGNED_CHANNEL_COUNT,
        )
    raise ValueError(f"unsupported feature schema: {version}")
