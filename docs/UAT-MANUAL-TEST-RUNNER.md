# UAT Manual Test Runner

The `claude-agent` image now supports a one-shot Playwright runner mode for PR UAT:

```bash
RUN_MODE=uat-playwright
```

In this mode `agent/entrypoint.sh` skips repository cloning and runs `agent/uat_runner.py`. The runner:

1. Downloads a JSON test plan from `TEST_PLAN_S3_URI` or reads `TEST_PLAN_JSON`.
2. Resolves auth from `MOBOT_AUTH_HEADER`, `MOBOT_AUTH_TOKEN`, or the API-key exchange endpoint.
3. Exposes `MOBOT_AUTH_HEADER` to Claude Code.
4. Runs Claude Code with Playwright Chromium available in the image.
5. Writes normalized results to `RESULTS_S3_URI`.
6. Uploads screenshots/logs from `/workspace/uat-runner/artifacts` to `ARTIFACTS_S3_URI`.

## Runtime Environment

Required container environment:

| Name | Purpose |
| --- | --- |
| `RUN_MODE` | Must be `uat-playwright`. |
| `SESSION_ID` | Unique UAT session id. |
| `TARGET_URL` | UAT app URL to test. |
| `TEST_PLAN_S3_URI` or `TEST_PLAN_JSON` | Manual test plan input. |
| `RESULTS_S3_URI` | S3 object where `results.json` is written. |
| `MOBOT_AUTH_HEADER`, `MOBOT_AUTH_TOKEN`, `MOBOT_API_KEY_SECRET_ID`, or `MOBOT_API_KEY` | Auth source for Mobot. |

Optional:

| Name | Purpose |
| --- | --- |
| `ARTIFACTS_S3_URI` | S3 prefix for logs/screenshots. |
| `MOBOT_BASE_URL` | Defaults to `https://app.teammobot.dev`. |
| `CLAUDE_MODEL` | Overrides the Claude Code model. The runner also exposes this as `ANTHROPIC_MODEL` and, for Opus models, `ANTHROPIC_DEFAULT_OPUS_MODEL`. The test_tickets workflow sets this to `global.anthropic.claude-opus-4-7`. |

Auth resolution order:

1. `MOBOT_AUTH_HEADER`
2. `MOBOT_AUTH_TOKEN` as `Token <value>`
3. `MOBOT_API_KEY_SECRET_ID` / `MOBOT_API_KEY` exchanged through `/api/login/api-key/session`

## Deploy

Build and push the updated agent image using the existing `claude-agent` image workflow:

```bash
cd agent
docker build --platform linux/amd64 -t claude-agent .
docker tag claude-agent:latest 678954237808.dkr.ecr.us-east-1.amazonaws.com/claude-agent:uat-runner
docker push 678954237808.dkr.ecr.us-east-1.amazonaws.com/claude-agent:uat-runner
```

After staging validation, promote to `:latest` using the repo's existing process if you want routine agent sessions to pick up the same image.

Register the task definition:

```bash
aws ecs register-task-definition \
  --cli-input-json file://agent/uat-runner-task-definition.json \
  --region us-east-1
```

The GitHub workflow in `team-mobot/test_tickets` expects the task family `claude-playwright-uat-runner` with container name `uat-runner`.

## IAM

The runner task role needs:

- `s3:GetObject` for the uploaded test plan.
- `s3:PutObject` for results and artifacts.
- `secretsmanager:GetSecretValue` for the Mobot API key secret.
- Bedrock permissions used by Claude Code.

The workflow OIDC role needs `ecs:RunTask`, `ecs:DescribeTasks`, `iam:PassRole` for this task definition, and access to the same S3 result prefix.

## Quiet UAT App Launches

`test-tickets-uat/entrypoint.sh` now honors:

```bash
SUPPRESS_GITHUB_COMMENTS=true
```

The UAT manual test workflow sets this so the PR only receives the final sticky results comment instead of the existing startup comment.
