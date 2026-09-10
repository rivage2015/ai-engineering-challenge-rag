# F05b audit runner output-save correction

The root reviewed the entire initial runner and approved its safety, worker selection, guards, single-child 30-second timeout and 1-MiB log limit. Before any execution, the root requested only that the before/after source-map JSON be printed to stdout and saved by the auditor with apply_patch, rather than written directly by the script.

- Retained, unexecuted original: f05b-audit-run.v1.py, SHA256 42c79d1c98a2c4b45fa6a7afb0d517587b5493890cdac5436b3874eff4085891.
- Approved executable revision: f05b-audit-run.v2.py, SHA256 6ef1598f64204a95caa8eb37a248cc75afc6978c43c1149f92445c33f8246800.
- Only the capture-output block changed: remove exclusive JSON file creation and print the full value. No guard, loader, test, timeout, log-cap, source validation or outcome criterion changed. This is a pre-execution auxiliary correction, not a formal product repair or a test failure.

The final executor-run.v2.py and its delegated runner have been read before audit execution. Each run will use a new audit run ID and retain unsuccessful attempts as well as successful results.
