---
name: aws_boto3
description: Runs one read-only AWS API call via boto3 and returns the JSON response. Only client methods whose snake_case names start with list, get, describe, head, query, search, lookup, batch_get, scan, or select are allowed; anything else is rejected with an explanation. Use for every AWS read, including inventories, resource status, configuration, CloudWatch data, Bedrock model ids, and caller identity. Do not use for writes (create, update, delete, put, tag), for methods outside the allowlist such as check_*, simulate_*, or generate_presigned_url, or for high-level CLI features like s3 sync; route those to aws_cli, which asks the user for approval. Runs instantly with no approval prompt. Output has ResponseMetadata removed and is truncated at 100,000 characters. Region defaults to us-east-1.
triggers: aws, aws list, aws describe, s3 buckets, ec2 instances, bedrock models, model ids, iam roles, lambda functions, dynamodb tables, ecs clusters, eks, rds instances, cloudwatch alarms, cloudwatch logs, sns topics, sqs queues, vpcs, subnets, security groups, route53, sagemaker, cloudformation stacks, caller identity, aws account
executor: aws_boto3
input_schema:
  service:
    type: string
    required: true
    description: "AWS service name for boto3.client(), e.g. s3, ec2, bedrock, iam, lambda, dynamodb, ecs, rds, cloudwatch, logs, sns, sqs, sts."
  method:
    type: string
    required: true
    description: "Read-only boto3 client method in snake_case. Must start with list, get, describe, head, query, search, lookup, batch_get, scan, or select (e.g. list_buckets, describe_instances, list_foundation_models, get_caller_identity)."
  params:
    type: object
    description: "Keyword arguments for the method, as a JSON object with exact PascalCase boto3 parameter names. Example: {'Filters': [{'Name': 'instance-state-name', 'Values': ['running']}]} or {'Bucket': 'my-bucket', 'MaxKeys': 20}. Use MaxResults / NextToken to paginate."
  region:
    type: string
    description: "AWS region. Optional; defaults to us-east-1."
---

# AWS boto3 (read-only)

Executor-backed tool. This body is documentation for the Skills panel; the model
sees only the frontmatter description and input_schema. Behavior at runtime:

- Runs `boto3.client(service, region_name=region).method(**params)` and returns the
  result as indented JSON. No user approval is required (reads only).
- Only methods whose snake_case name starts with one of these prefixes run:
  `list`, `get`, `describe`, `head`, `query`, `search`, `lookup`, `batch_get`,
  `scan`, `select`. Anything else returns a "not a read-only operation" message that
  points to `aws_cli`.
- `ResponseMetadata` is stripped from the result. Output over 100,000 characters is
  truncated.
- Credentials come from the standard AWS chain (`~/.aws`, environment). Region
  defaults to `us-east-1`.

Common request to call mappings:

| Request | service | method |
|---------|---------|--------|
| list S3 buckets | s3 | list_buckets |
| show EC2 instances | ec2 | describe_instances |
| list Bedrock models | bedrock | list_foundation_models |
| list IAM roles | iam | list_roles |
| list Lambda functions | lambda | list_functions |
| list DynamoDB tables | dynamodb | list_tables |
| who am I / my account | sts | get_caller_identity |
| list ECS clusters | ecs | list_clusters |
| describe RDS instances | rds | describe_db_instances |
| list CloudWatch alarms | cloudwatch | describe_alarms |
| list SNS topics | sns | list_topics |
| list SQS queues | sqs | list_queues |
| list VPCs | ec2 | describe_vpcs |
| list security groups | ec2 | describe_security_groups |

For write, delete, or modify operations, and for read-shaped methods outside the
allowlist (`check_*`, `simulate_*`, `generate_presigned_url`) or CLI features like
`s3 sync`, use the `aws_cli` skill instead.
