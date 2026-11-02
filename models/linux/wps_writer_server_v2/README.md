# Linux WPS server candidate v2

Model ID: `linux-wps-writer-server-20261101-v2`. Explicit opt-in candidate only; production_ready=false. Existing frozen models and the previous Linux profile are unchanged.

40 audited windows from three calibration application instances (two complete 18-class rounds plus four input controls), with at least two samples and two restarts per class. The formula is unchanged: 2048-D frozen WPS VMA features, L2 and cosine class centers. No rejection threshold, smoothing or parameter search.

Independent application-restart replay: 26/37 correct (70.27%); WRITE_TEXT 1/2. This is one server, controlled synthetic documents in real WPS 12.1.0.17900, with 15 keyboard operations and find/replace/table RPC. It does not establish reliable input recognition, free-user recognition or production readiness.

All required processes must still be collected and validated. The explicit Native v2 profile uses the root process's feature maps, matching the one-process training snapshots, and the before-clear_refs window clock. No failed child collection is ignored. A subsequent normal-GUI Native smoke produced three valid windows: WRITE_TEXT was misclassified as INSERT_PAGE_BREAK, SAVE_DOCUMENT and FORMAT_BOLD were correct. This does not establish general online accuracy. The export-time native_runtime_ready=false field is retained to preserve the model.json SHA; Native explicit loading is validated separately.

Private traces, documents, training manifests and individual predictions are not included. Aggregate evaluation and content hashes are provided for review.
