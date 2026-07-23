---
name: aws_cli
description: Builds and runs AWS CLI commands for write and modify operations (create, delete, update, put, deploy, tag, terminate) and for operations aws_boto3 rejects, such as s3 sync or cp, presigned URLs, and check_* or simulate_* methods. The command runs in a sandbox with the user's AWS credentials and is shown on an approval card before it executes, so route mutations here rather than through terminal_run. It must start with "aws ", has a 30s default timeout, and output is truncated at 10,000 characters. Command substitution ($() or backticks outside single quotes) is blocked; run a read via aws_boto3 first and substitute literal values, and wrap --query JMESPath filters in single quotes. When a mutating command is missing a required parameter, set mode to check_params to return a parameter checklist for the user instead of running. For ordinary reads (list, describe, get) use aws_boto3 instead; it is faster and needs no approval.
triggers: aws create, aws delete, aws update, aws modify, aws put, aws deploy, aws terminate, aws provision, aws start, aws stop, aws cli, s3 sync, s3 upload, s3 cp, presign, aws invoke, aws tag, cloudformation deploy
executor: aws_cli
input_schema:
  command:
    type: string
    required: true
    description: "Full AWS CLI command starting with 'aws ' (e.g. aws s3api create-bucket --bucket my-bucket --region eu-west-1). No $() or backtick substitution; single-quote any --query JMESPath. In check_params mode this field instead carries the plain-text parameter checklist to show the user."
  mode:
    type: string
    description: "Closed set: 'execute' or empty requests user approval and runs the command; 'check_params' skips approval and execution and returns the checklist text in `command` so it can be presented to the user."
---

# AWS CLI (write operations)

Executor-backed tool. This body is documentation for the Skills panel; the model
sees only the frontmatter description and input_schema. Behavior at runtime:

- With `mode` empty or `execute`: the command must start with `aws `. It is shown on
  an approval card, then run in a sandbox that can reach the user's AWS credentials
  (`~/.aws`), with a 30s default timeout. Combined stdout and stderr over 10,000
  characters is truncated.
- With `mode=check_params`: nothing runs and no approval is shown; the text in
  `command` is echoed back so the model can present a required-parameter checklist to
  the user before building the real command.
- After approval, command validation blocks: `$(...)` and backtick substitution
  (outside single quotes), redirecting output into key files (`.pem`, `.key`),
  `sed -i`, reads of sensitive paths, and very long command chains. Interactive
  subcommands (`aws configure`, `aws sso login`) cannot run in the sandbox; tell the
  user to run those in their own terminal.

Recommended flow for create, modify, and delete commands: confirm every required
parameter first (use `check_params` to surface a checklist), name exactly what a
destructive command will change, then run the single `aws ...` command so the
approval card shows the user precisely what executes.

For read-only queries (list, describe, get), use the `aws_boto3` skill instead; it
answers instantly without approval.
