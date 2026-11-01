# Linux WPS Writer pilot v1

18 class centroids × 2048 dimensions; L2 normalization and cosine argmax.
Fitted only on 54 independent train observations (doc1-3), three per class.
36 separate preflight and 100 continuous observations never fit parameters.
doc4/5 and continuous scores are exploratory; their outcomes were previously viewed.
No threshold, smoothing, all-data refit or online/cross-restart validation.

Load explicitly using classification.predict with --schema wps.vma.feature.v1.
Verify SHA256SUMS.json using scripts.linux_train verify before inference.
Native's frozen registry does not load this candidate; no service model was replaced.
