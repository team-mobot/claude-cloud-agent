# JIRA Integration Learnings

**Status:** Fully implemented and verified working (2026-02-03)

Notes from implementation - reference for future maintenance.

## Critical Discovery: Two Task Definition Families

There are TWO task definition families that were confused:

| Family | Status | Container Name | Has Secrets |
|--------|--------|----------------|-------------|
| `claude-cloud-agent` | Broken/incomplete | `claude-cloud-agent` | No |
| `claude-agent` | Working | `claude-agent` | Yes |

**The Lambda was using `claude-cloud-agent:3` but should use `claude-agent:X`.**

The working `claude-agent` task definition includes:
- `GITHUB_APP_ID` secret
- `GITHUB_APP_PRIVATE_KEY` secret
- `GITHUB_TOKEN` secret
- `GIT_USER_NAME` and `GIT_USER_EMAIL`
- JIRA credentials

## Container Push (Fixed)

~~The container's `push_changes()` method exists in `claude_runner.py` but is **never called** in `main.py`.~~

**Fixed:** The container now properly commits and pushes changes. Claude Code handles git operations internally, and the agent tracks commits from the Claude Code session.

## JIRA Webhook Details

### Signature Verification
```python
import hashlib
import hmac

def verify_jira_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
```

Header: `x-hub-signature` contains `sha256=<hex_digest>`

### Webhook Identification
Presence of `x-atlassian-webhook-identifier` header indicates JIRA webhook.

### API Authentication
JIRA Cloud uses Basic Auth:
- Username: email address
- Password: API token (not password)

```python
import base64
auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()
headers = {"Authorization": f"Basic {auth}"}
```

### Atlassian Document Format (ADF)
Issue descriptions and comments use ADF (rich JSON format), not plain text.

Extract plain text:
```python
def extract_text_from_adf(adf: dict) -> str:
    if not adf or adf.get("type") != "doc":
        return ""

    texts = []
    def walk(node):
        if node.get("type") == "text":
            texts.append(node.get("text", ""))
        for child in node.get("content", []):
            walk(child)

    walk(adf)
    return " ".join(texts)
```

## Secrets Manager Structure

JIRA secret (`claude-cloud-agent/jira`):
```json
{
  "webhook_secret": "shared secret for HMAC",
  "base_url": "https://company.atlassian.net",
  "email": "service-account@company.com",
  "api_token": "ATATT3x...",
  "project_mapping": {
    "PROJ": "owner/repo",
    "AGNTS": "team-mobot/test_tickets"
  }
}
```

## IAM Permissions Needed

### Lambda Role
- `iam:PassRole` for `claude-agent-*` task roles (not just `claude-cloud-agent-*`)

### ECS Execution Role
- `secretsmanager:GetSecretValue` for `claude-dev/*` secrets

### Agent Task Role (for completion summaries)
- `secretsmanager:GetSecretValue` for `claude-cloud-agent/jira` secret
- Added via Terraform `aws_iam_role_policy.agent_task_jira_secret` in `iam.tf`

## DynamoDB Schema Extensions

Add fields to session table:
- `source`: "github" or "jira"
- `jira_issue_key`: e.g., "AGNTS-118"
- `jira_project_key`: e.g., "AGNTS"

Add GSI `jira-issue-index`:
- Partition key: `jira_issue_key`
- Sort key: `created_at`

## Implementation Order (Recommended)

1. **Fix container first** - Add `push_changes()` call and explicit git commit
2. **Verify GitHub flow works** - Test with GitHub issue trigger
3. **Then add JIRA** - It's just a different trigger source

## Trigger Conditions

- **Label trigger**: `issue_updated` event where `claude-dev` label was added
- **Comment trigger**: `comment_created` event where body contains `@claude`

## Error Handling

Post errors back to JIRA as comments so user knows what went wrong:
```python
jira_client.add_comment(
    issue_key,
    f"❌ Failed to start Claude session: {error_message}"
)
```

## Completion Summary (Implemented 2026-02-03)

When the agent completes initial implementation, it posts a summary back to JIRA:

```python
# agent/jira_reporter.py
await jira.post_completion_summary(
    success=result["success"],
    summary=result.get("summary", ""),
    commits=result.get("commits", []),
    error=result.get("error")
)
```

The summary includes:
- ✅/⚠️ Status indicator
- Link to GitHub PR
- Summary of changes
- Recent commits
- Error message (if failed)
- Prompt to review PR

**Environment variables needed:**
- `JIRA_ISSUE_KEY` - Set by Lambda for JIRA-triggered sessions
- `JIRA_SECRET_ARN` - ARN for credentials secret

**Files:**
- `agent/jira_reporter.py` - JiraReporter class
- `agent/main.py` - Calls `post_completion_summary()` after initial prompt
