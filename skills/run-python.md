---
name: run_python
description: Executes a self-contained Python 3 script in an OS-sandboxed subprocess and returns combined stdout and stderr. Every call is approval-gated; the user must approve before the code runs. Use for calculations, data analysis, text processing, parsing, and quick prototyping when no shell is needed. Limits are a 15 second timeout, no stdin, cwd /tmp, and no state between calls; secret paths like ~/.ssh are blocked while ~/.aws is allowed, and AWS write operations are blocked in-process (only read-shaped boto3 calls pass). Results appear only if printed; bare expressions are not echoed. Do not use for shell commands (use terminal_run), for files in a connected workspace (use ws_run_command or ws_read_file), or for read-only AWS queries (prefer aws_boto3, which needs no approval).
triggers: python, run python, calculate, compute, math, statistics, regex, script, csv
executor: run_python
input_schema:
  code:
    type: string
    required: true
    description: Complete Python 3 script. Print anything that should be returned; bare expressions are not echoed. No state persists between calls, stdin is unavailable, 15 second limit, network access is allowed.
---

Runs after user approval inside the OS sandbox (sandbox-exec on macOS): secret paths
such as ~/.ssh are denied, ~/.aws is allowed so boto3 can authenticate, and a botocore
guard blocks AWS write operations in-process (read-shaped calls like list/get/describe
pass). The script runs from /tmp with stdin closed and a 15 second hard timeout;
combined stdout and stderr come back, or "(no output)" when nothing was printed.
