# UAT Manual Test Runner

The `claude-agent` image supports a one-shot Playwright runner mode for PR UAT:

```bash
RUN_MODE=uat-playwright
```

In this mode `agent/entrypoint.sh` skips repository cloning and runs `agent/uat_runner.py`. The runner:

1. Downloads a JSON test plan from `TEST_PLAN_S3_URI` or reads `TEST_PLAN_JSON`.
2. Resolves Mobot auth from `MOBOT_AUTH_HEADER`, `MOBOT_AUTH_TOKEN`, or the API-key exchange endpoint.
3. Exposes `MOBOT_AUTH_HEADER` to Pi.
4. Runs Pi against Azure OpenAI Responses with Playwright Chromium and Pi's `bash`, `read`, `write`, and `edit` tools enabled.
5. Writes normalized results to `RESULTS_S3_URI`.
6. Uploads Pi output, screenshots, and logs from `/workspace/uat-runner/artifacts` to `ARTIFACTS_S3_URI`.

## Runtime Environment

Required container environment:

| Name | Purpose |
| --- | --- |
| `RUN_MODE` | Must be `uat-playwright`. |
| `SESSION_ID` | Unique UAT session id. |
| `TARGET_URL` | UAT app URL to test. |
| `TEST_PLAN_S3_URI` or `TEST_PLAN_JSON` | Manual test plan input. |
| `RESULTS_S3_URI` | S3 object where `results.json` is written. |
| `UAT_RUNNER_AZURE_RESPONSES_ENDPOINT` | Dedicated Azure OpenAI Responses gateway/root endpoint for this runner. |
| `UAT_RUNNER_AZURE_RESPONSES_API_KEY` | API key for that dedicated UAT runner gateway. |
| `MOBOT_AUTH_HEADER`, `MOBOT_AUTH_TOKEN`, `MOBOT_API_KEY_SECRET_ID`, or `MOBOT_API_KEY` | Auth source for Mobot. |

Optional:

| Name | Purpose |
| --- | --- |
| `UAT_RUNNER_AZURE_RESPONSES_MODEL` | Azure deployment/model name; defaults to `gpt-5.6-terra`. |
| `ARTIFACTS_S3_URI` | S3 prefix for Pi output and browser evidence. |
| `MOBOT_BASE_URL` | Defaults to `https://app.teammobot.dev`. |
| `RUNNER_PROMPT` or `RUNNER_PROMPT_S3_URI` | Prompt template. The existing template variables and `results.json` contract are preserved. |

The runner maps only the `UAT_RUNNER_AZURE_RESPONSES_*` values to Pi's Azure provider variables (`AZURE_OPENAI_BASE_URL` and `AZURE_OPENAI_API_KEY`). It deliberately removes inherited Claude/Bedrock model settings from the Pi process. Do not substitute generic application OpenAI credentials for this contract.

Auth resolution order:

1. `MOBOT_AUTH_HEADER`
2. `MOBOT_AUTH_TOKEN` as `Token <value>`
3. `MOBOT_API_KEY_SECRET_ID` / `MOBOT_API_KEY` exchanged through `/api/login/api-key/session`

If `MOBOT_AUTH_TOKEN` is an expired JWT and API-key auth is configured, the runner exchanges the API key for a fresh token before starting Pi. Otherwise it publishes blocked results with a clear auth error.

## Post-merge deployment

This repository does not provision Azure gateway credentials or deploy images. Before enabling this revision:

1. Configure a **dedicated UAT-runner Azure OpenAI Responses gateway/deployment** for `gpt-5.6-terra`, and make its endpoint and key available to the UAT workflow/task launcher as `UAT_RUNNER_AZURE_RESPONSES_ENDPOINT` and `UAT_RUNNER_AZURE_RESPONSES_API_KEY`.
2. Build and push the updated image under the runner tag:

   ```bash
   cd agent
   docker build --platform linux/amd64 -t claude-agent .
   docker tag claude-agent:latest 678954237808.dkr.ecr.us-east-1.amazonaws.com/claude-agent:uat-runner
   docker push 678954237808.dkr.ecr.us-east-1.amazonaws.com/claude-agent:uat-runner
   ```

3. Register a new revision from `agent/uat-runner-task-definition.json`, then update the UAT workflow/task launcher to use it and inject the two dedicated Azure values. The task-definition source supplies the model default but intentionally does not contain a gateway endpoint or secret.

   ```bash
   aws ecs register-task-definition \
     --cli-input-json file://agent/uat-runner-task-definition.json \
     --region us-east-1
   ```

The GitHub workflow in `team-mobot/test_tickets` expects task family `claude-playwright-uat-runner` and container name `uat-runner`.

## IAM

The runner task role needs:

- `s3:GetObject` for the uploaded test plan.
- `s3:PutObject` for results and artifacts.
- `secretsmanager:GetSecretValue` for the Mobot API key secret.
- Network egress to the dedicated Azure Responses gateway.

The workflow OIDC role needs `ecs:RunTask`, `ecs:DescribeTasks`, `iam:PassRole` for this task definition, and access to the same S3 result prefix.

## Quiet UAT App Launches

`test-tickets-uat/entrypoint.sh` honors:

```bash
SUPPRESS_GITHUB_COMMENTS=true
```

The UAT manual test workflow sets this so the PR only receives the final sticky results comment instead of the existing startup comment.
