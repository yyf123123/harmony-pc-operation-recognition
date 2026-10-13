# External feature window manifest

This is a format example, not a generated dataset or measured result. Paths are supplied by the user and resolved relative to the manifest. Snapshot JSON has the existing `VmaSnapshot` fields, including `regions`, `app_id`, `phase`, and source provenance. Do not use mock windows for reported accuracy.

```json
{
  "sample_id": "YOUR_TRIAL_ID",
  "app_id": "bilibili",
  "operation_id": "bilibili.video.open_fixed_video",
  "baselines": ["baseline_one.json", "baseline_two.json"],
  "action": "action.json",
  "post_action": "post_short.json",
  "additional_post_actions": ["post_long.json"],
  "metadata": {"capture_profile": {"identity": "YOUR_CAPTURE_PROFILE_IDENTITY"}}
}
```
