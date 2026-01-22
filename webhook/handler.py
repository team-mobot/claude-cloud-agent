"""
Webhook Handler for Claude Cloud Agent
Handles GitHub and JIRA events for issue labeling, comments, and PR closure.
"""

import json
import hmac
import hashlib
import os
import base64
import requests
from datetime import datetime

import boto3
from github import Github, GithubIntegration

# AWS clients
secrets = boto3.client('secretsmanager')
dynamodb = boto3.resource('dynamodb')
ecs = boto3.client('ecs')

sessions_table = dynamodb.Table(os.environ.get('SESSION_TABLE', 'claude-cloud-agent-sessions'))


def handler(event, context):
    """Main webhook handler - routes GitHub and JIRA events."""

    headers = {k.lower(): v for k, v in event.get('headers', {}).items()}

    # Detect source and route accordingly
    if 'x-atlassian-webhook-identifier' in headers:
        return handle_jira_webhook(event)
    elif 'x-github-event' in headers:
        return handle_github_webhook(event)
    else:
        return {'statusCode': 400, 'body': 'Unknown webhook source'}


# =============================================================================
# GitHub Handlers
# =============================================================================

def handle_github_webhook(event):
    """Handle GitHub webhook events."""

    if not verify_github_signature(event):
        return {'statusCode': 401, 'body': 'Invalid signature'}

    body = json.loads(event['body'])
    headers = {k.lower(): v for k, v in event.get('headers', {}).items()}
    event_type = headers.get('x-github-event', '').lower()

    trigger_label = os.environ.get('TRIGGER_LABEL', 'claude-dev')

    if event_type == 'issues' and body['action'] == 'labeled':
        if body['label']['name'] == trigger_label:
            return handle_github_new_issue(body['issue'], body['repository'])

    elif event_type == 'issue_comment' and body['action'] == 'created':
        return handle_github_comment(body)

    elif event_type == 'pull_request' and body['action'] == 'closed':
        return handle_pr_closed(body['pull_request'])

    return {'statusCode': 200, 'body': 'OK'}


def verify_github_signature(event) -> bool:
    """Verify GitHub webhook signature."""

    headers = {k.lower(): v for k, v in event.get('headers', {}).items()}
    signature = headers.get('x-hub-signature-256', '')

    if not signature:
        return False

    secret_arn = os.environ.get('GITHUB_SECRET_ARN')
    if not secret_arn:
        return False

    try:
        secret = secrets.get_secret_value(SecretId=secret_arn)
        creds = json.loads(secret['SecretString'])
        webhook_secret = creds.get('webhook_secret', '')

        body = event.get('body', '')

        if event.get('isBase64Encoded'):
            body = base64.b64decode(body).decode('utf-8')

        expected = 'sha256=' + hmac.new(
            webhook_secret.encode(),
            body.encode(),
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(signature, expected)
    except Exception as e:
        print(f"Error in verify_github_signature: {e}")
        return False


def get_github_client():
    """Get authenticated GitHub client."""

    secret_arn = os.environ.get('GITHUB_SECRET_ARN')
    secret = secrets.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])

    integration = GithubIntegration(
        creds['app_id'],
        creds['private_key']
    )
    installation = integration.get_installations()[0]
    return installation.get_github_for_installation()


def handle_github_new_issue(issue: dict, repo: dict):
    """Start new dev session from GitHub issue."""

    gh = get_github_client()
    repository = gh.get_repo(repo['full_name'])
    issue_number = issue['number']
    session_id = f"github-issue-{repo['full_name'].replace('/', '-')}-{issue_number}"
    branch_name = f"claude-dev/issue-{issue_number}"

    # 1. Create branch
    base = repository.get_branch('main')
    repository.create_git_ref(
        ref=f"refs/heads/{branch_name}",
        sha=base.commit.sha
    )

    # 2. Create initial commit with session info
    session_content = f"""# Claude Cloud Agent Session

**Issue:** #{issue_number}
**Title:** {issue['title']}
**Started:** {datetime.utcnow().isoformat()}Z

## Original Request

{issue['body'] or issue['title']}

---
*This file tracks the Claude Cloud Agent session. Progress updates will be added as comments on the PR.*
"""
    repository.create_file(
        path=".claude-dev/session.md",
        message=f"Start Claude Cloud Agent session for issue #{issue_number}",
        content=session_content,
        branch=branch_name
    )

    # 3. Create draft PR
    pr = repository.create_pull(
        title=f"[Claude Dev] {issue['title']}",
        body=format_github_pr_description(issue),
        head=branch_name,
        base='main',
        draft=True
    )

    # 4. Store session
    sessions_table.put_item(Item={
        'session_id': session_id,
        'source': 'github',
        'pr_number': pr.number,
        'branch': branch_name,
        'repo': repo['full_name'],
        'status': 'starting',
        'created_at': datetime.utcnow().isoformat()
    })

    # 5. Close issue with link to PR
    issue_obj = repository.get_issue(issue_number)
    issue_obj.create_comment(
        f"**Development session started!**\n\n"
        f"All work will be tracked in PR #{pr.number}.\n\n"
        f"Please provide feedback and interact there."
    )
    issue_obj.edit(state='closed')

    # 6. Start agent task
    task_info = start_agent_task(
        session_id=session_id,
        pr_number=pr.number,
        branch=branch_name,
        prompt=issue['body'] or issue['title'],
        repo=repo['full_name'],
        source='github'
    )

    # 7. Post UAT info to PR
    if task_info:
        pr.create_issue_comment(
            f"**UAT Access**\n\n"
            f"Connect to the running container:\n"
            f"```bash\n{task_info['uat_command']}\n```\n\n"
            f"*Requires AWS CLI and Session Manager plugin*"
        )

    return {'statusCode': 200, 'body': f'Started session {session_id}'}


def handle_github_comment(body: dict):
    """Handle user comment on PR - continue session."""

    if not body.get('issue', {}).get('pull_request'):
        return {'statusCode': 200, 'body': 'Not a PR comment'}

    pr_number = body['issue']['number']
    comment = body['comment']
    comment_text = comment.get('body', '')

    # Skip bot's own comments - check username and content signatures
    is_bot_comment = (
        comment['user']['login'].endswith('[bot]') or
        '**Claude Cloud Agent' in comment_text or
        '💬 **Claude:**' in comment_text or
        '📖 **Reading file:**' in comment_text or
        '💻 **Running:**' in comment_text or
        '✅ **Result:**' in comment_text or
        '❌ **Error:**' in comment_text or
        '**UAT Access**' in comment_text
    )

    if is_bot_comment:
        return {'statusCode': 200, 'body': 'Bot comment, ignoring'}

    # Find session
    response = sessions_table.query(
        IndexName='pr-index',
        KeyConditionExpression='pr_number = :pr',
        ExpressionAttributeValues={':pr': pr_number}
    )

    if not response['Items']:
        return {'statusCode': 200, 'body': 'No session found'}

    session = response['Items'][0]

    # Skip if session is already running (prevent duplicate tasks)
    if session.get('status') == 'running':
        print(f"Session {session['session_id']} already running, skipping")
        return {'statusCode': 200, 'body': 'Session already running'}

    # Resume agent with user's comment as new instruction
    start_agent_task(
        session_id=session['session_id'],
        pr_number=pr_number,
        branch=session['branch'],
        prompt=comment['body'],
        repo=body['repository']['full_name'],
        source=session.get('source', 'github'),
        jira_issue_key=session.get('jira_issue_key'),
        jira_site=session.get('jira_site'),
        resume=True
    )

    return {'statusCode': 200, 'body': 'Session resumed'}


def handle_pr_closed(pr: dict):
    """Clean up when PR is merged or closed."""

    pr_number = pr['number']

    # Find session
    response = sessions_table.query(
        IndexName='pr-index',
        KeyConditionExpression='pr_number = :pr',
        ExpressionAttributeValues={':pr': pr_number}
    )

    if not response['Items']:
        return {'statusCode': 200, 'body': 'No session found'}

    session = response['Items'][0]

    # Stop agent task if running
    if session.get('agent_task_arn'):
        try:
            ecs.stop_task(
                cluster=os.environ.get('ECS_CLUSTER', 'claude-cloud-agent'),
                task=session['agent_task_arn'],
                reason='PR closed'
            )
        except Exception as e:
            print(f"Error stopping task: {e}")

    # Update session status
    status = 'completed' if pr.get('merged') else 'closed'
    sessions_table.update_item(
        Key={'session_id': session['session_id']},
        UpdateExpression='SET #status = :status, completed_at = :time',
        ExpressionAttributeNames={'#status': 'status'},
        ExpressionAttributeValues={
            ':status': status,
            ':time': datetime.utcnow().isoformat()
        }
    )

    # Post final comment to GitHub PR
    gh = get_github_client()
    repo = gh.get_repo(pr['base']['repo']['full_name'])
    pr_obj = repo.get_pull(pr_number)

    status_text = "merged" if pr.get('merged') else "closed"
    pr_obj.create_issue_comment(
        f"**Session Complete**\n\n"
        f"PR {status_text}. Agent session ended.\n\n"
        f"Session ID: `{session['session_id']}`"
    )

    # If JIRA-sourced, post completion to JIRA
    if session.get('source') == 'jira' and session.get('jira_issue_key'):
        try:
            jira_creds = get_jira_credentials()
            post_jira_comment(
                jira_creds,
                session['jira_issue_key'],
                f"**Claude Cloud Agent Session Complete**\n\n"
                f"PR {status_text}. See PR for full details.\n\n"
                f"Session ID: `{session['session_id']}`"
            )
        except Exception as e:
            print(f"Error posting JIRA completion comment: {e}")

    return {'statusCode': 200, 'body': 'Session cleaned up'}


def format_github_pr_description(issue: dict, jira_issue_key: str = None) -> str:
    """Format PR description from GitHub issue."""

    jira_link = ""
    if jira_issue_key:
        jira_link = f"\n**JIRA:** {jira_issue_key}\n"

    return f"""## Claude Cloud Agent Session

This PR was automatically created from issue #{issue['number']}.
{jira_link}
### Original Request

{issue['body'] or issue['title']}

---

**Session ID:** `github-issue-{issue['number']}`

All development work is logged below as comments.
"""


# =============================================================================
# JIRA Handlers
# =============================================================================

def handle_jira_webhook(event):
    """Handle JIRA webhook events."""

    if not verify_jira_signature(event):
        return {'statusCode': 401, 'body': 'Invalid JIRA signature'}

    body = json.loads(event['body'])
    webhook_event = body.get('webhookEvent', '')

    print(f"JIRA webhook event: {webhook_event}")

    trigger_label = os.environ.get('TRIGGER_LABEL', 'claude-dev')

    # Issue updated - check for label addition
    if webhook_event == 'jira:issue_updated':
        changelog = body.get('changelog', {})
        for item in changelog.get('items', []):
            if item.get('field') == 'labels':
                # Check if trigger label was added
                from_labels = set((item.get('fromString') or '').split())
                to_labels = set((item.get('toString') or '').split())
                added_labels = to_labels - from_labels

                if trigger_label in added_labels:
                    return handle_jira_labeled(body['issue'], body)

    # Issue comment created
    elif webhook_event == 'comment_created':
        return handle_jira_comment(body)

    return {'statusCode': 200, 'body': 'OK'}


def verify_jira_signature(event) -> bool:
    """Verify JIRA webhook signature (if configured)."""

    # JIRA Cloud webhooks don't have built-in HMAC signatures like GitHub
    # Authentication is typically done via:
    # 1. IP allowlisting (Atlassian IPs)
    # 2. Shared secret in custom header
    # 3. JWT for Atlassian Connect apps

    # For now, check for custom shared secret header if configured
    secret_arn = os.environ.get('JIRA_SECRET_ARN')
    if not secret_arn:
        print("Warning: No JIRA_SECRET_ARN configured, skipping signature verification")
        return True

    try:
        secret = secrets.get_secret_value(SecretId=secret_arn)
        creds = json.loads(secret['SecretString'])
        webhook_secret = creds.get('webhook_secret')

        if not webhook_secret:
            print("Warning: No webhook_secret in JIRA credentials, skipping verification")
            return True

        headers = {k.lower(): v for k, v in event.get('headers', {}).items()}

        # Check for custom auth header
        provided_secret = headers.get('x-webhook-secret', '')
        if provided_secret and hmac.compare_digest(provided_secret, webhook_secret):
            return True

        # If no header but secret configured, log warning but allow (for dev)
        print("Warning: JIRA webhook secret configured but not provided in request")
        return True

    except Exception as e:
        print(f"Error in verify_jira_signature: {e}")
        return False


def get_jira_credentials() -> dict:
    """Get JIRA credentials from Secrets Manager."""

    secret_arn = os.environ.get('JIRA_SECRET_ARN')
    if not secret_arn:
        raise ValueError("JIRA_SECRET_ARN not configured")

    secret = secrets.get_secret_value(SecretId=secret_arn)
    return json.loads(secret['SecretString'])


def get_jira_issue(creds: dict, issue_key: str) -> dict:
    """Fetch JIRA issue details."""

    site = creds['site']
    email = creds['email']
    api_token = creds['api_token']

    response = requests.get(
        f"https://{site}/rest/api/3/issue/{issue_key}",
        auth=(email, api_token),
        headers={'Accept': 'application/json'}
    )
    response.raise_for_status()
    return response.json()


def post_jira_comment(creds: dict, issue_key: str, body_text: str):
    """Post a comment to a JIRA issue."""

    site = creds['site']
    email = creds['email']
    api_token = creds['api_token']

    # JIRA Cloud API v3 uses Atlassian Document Format (ADF)
    adf_body = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": body_text}
                    ]
                }
            ]
        }
    }

    response = requests.post(
        f"https://{site}/rest/api/3/issue/{issue_key}/comment",
        auth=(email, api_token),
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        },
        json=adf_body
    )
    response.raise_for_status()
    return response.json()


def handle_jira_labeled(issue: dict, webhook_data: dict):
    """Start new dev session from JIRA issue."""

    issue_key = issue['key']
    fields = issue.get('fields', {})

    # Get JIRA credentials
    jira_creds = get_jira_credentials()
    jira_site = jira_creds['site']

    # Get target repo from custom field
    repo_field_id = jira_creds.get('repo_custom_field_id', 'customfield_10001')
    target_repo = fields.get(repo_field_id)

    if not target_repo:
        print(f"No target repo specified in custom field {repo_field_id}")
        post_jira_comment(
            jira_creds,
            issue_key,
            f"**Claude Cloud Agent Error**\n\n"
            f"No GitHub repository specified. Please set the repository custom field and try again."
        )
        return {'statusCode': 400, 'body': 'No target repo specified'}

    # Clean up repo name (might be full URL or owner/repo format)
    if 'github.com/' in target_repo:
        target_repo = target_repo.split('github.com/')[-1].rstrip('/')
    target_repo = target_repo.strip()

    print(f"JIRA issue {issue_key} targeting repo: {target_repo}")

    # Get GitHub client and validate repo access
    gh = get_github_client()
    try:
        repository = gh.get_repo(target_repo)
    except Exception as e:
        print(f"Cannot access repo {target_repo}: {e}")
        post_jira_comment(
            jira_creds,
            issue_key,
            f"**Claude Cloud Agent Error**\n\n"
            f"Cannot access GitHub repository `{target_repo}`. "
            f"Please verify the repository name and permissions."
        )
        return {'statusCode': 400, 'body': f'Cannot access repo: {target_repo}'}

    # Create session
    session_id = f"jira-{issue_key}"
    branch_name = f"claude-dev/{issue_key}"
    issue_summary = fields.get('summary', issue_key)
    issue_description = fields.get('description', issue_summary)

    # Convert ADF description to plain text if needed
    if isinstance(issue_description, dict):
        issue_description = extract_text_from_adf(issue_description)

    # 1. Create branch
    try:
        base = repository.get_branch('main')
    except:
        base = repository.get_branch(repository.default_branch)

    repository.create_git_ref(
        ref=f"refs/heads/{branch_name}",
        sha=base.commit.sha
    )

    # 2. Create initial commit with session info
    session_content = f"""# Claude Cloud Agent Session

**JIRA Issue:** [{issue_key}](https://{jira_site}/browse/{issue_key})
**Title:** {issue_summary}
**Started:** {datetime.utcnow().isoformat()}Z

## Original Request

{issue_description}

---
*This file tracks the Claude Cloud Agent session. Progress updates will be added as comments on both the PR and JIRA issue.*
"""
    repository.create_file(
        path=".claude-dev/session.md",
        message=f"Start Claude Cloud Agent session for {issue_key}",
        content=session_content,
        branch=branch_name
    )

    # 3. Create draft PR
    pr = repository.create_pull(
        title=f"[Claude Dev] {issue_key}: {issue_summary}",
        body=format_jira_pr_description(issue_key, issue_summary, issue_description, jira_site),
        head=branch_name,
        base='main',
        draft=True
    )

    # 4. Store session
    sessions_table.put_item(Item={
        'session_id': session_id,
        'source': 'jira',
        'jira_issue_key': issue_key,
        'jira_site': jira_site,
        'pr_number': pr.number,
        'branch': branch_name,
        'repo': target_repo,
        'status': 'starting',
        'created_at': datetime.utcnow().isoformat()
    })

    # 5. Start agent task
    task_info = start_agent_task(
        session_id=session_id,
        pr_number=pr.number,
        branch=branch_name,
        prompt=f"JIRA Issue {issue_key}: {issue_summary}\n\n{issue_description}",
        repo=target_repo,
        source='jira',
        jira_issue_key=issue_key,
        jira_site=jira_site
    )

    # 6. Post comment to JIRA with PR link and UAT info
    uat_info = ""
    if task_info:
        uat_info = (
            f"\n\n**UAT Access**\n"
            f"Connect to the running container:\n"
            f"```\n{task_info['uat_command']}\n```"
        )

    post_jira_comment(
        jira_creds,
        issue_key,
        f"**Claude Cloud Agent Started**\n\n"
        f"Development session started! Working in GitHub:\n\n"
        f"- Repository: {target_repo}\n"
        f"- Branch: {branch_name}\n"
        f"- Pull Request: https://github.com/{target_repo}/pull/{pr.number}\n\n"
        f"Progress updates will be posted here and on the PR."
        f"{uat_info}"
    )

    # Also post UAT info to GitHub PR
    if task_info:
        pr.create_issue_comment(
            f"**UAT Access**\n\n"
            f"Connect to the running container:\n"
            f"```bash\n{task_info['uat_command']}\n```\n\n"
            f"*Requires AWS CLI and Session Manager plugin*"
        )

    return {'statusCode': 200, 'body': f'Started session {session_id}'}


def handle_jira_comment(webhook_data: dict):
    """Handle user comment on JIRA issue - continue session if exists."""

    comment = webhook_data.get('comment', {})
    issue = webhook_data.get('issue', {})
    issue_key = issue.get('key')

    if not issue_key:
        return {'statusCode': 200, 'body': 'No issue key found'}

    # Skip bot's own comments (check for automation user or specific text)
    author = comment.get('author', {})

    # Extract text from ADF body for bot detection
    comment_body_raw = comment.get('body', '')
    if isinstance(comment_body_raw, dict):
        comment_text = extract_text_from_adf(comment_body_raw)
    else:
        comment_text = str(comment_body_raw)

    # Skip if posted by app or contains bot signatures
    is_bot_comment = (
        author.get('accountType') == 'app' or
        'Claude Cloud Agent' in comment_text or
        '🤖' in comment_text or  # Bot emoji used in start message
        '💬 **Claude:**' in comment_text or  # Claude response format
        comment_text.startswith('Claude Cloud Agent')
    )

    if is_bot_comment:
        print(f"Skipping bot comment on {issue_key}")
        return {'statusCode': 200, 'body': 'Bot comment, ignoring'}

    # Find session by JIRA issue key
    response = sessions_table.query(
        IndexName='jira-issue-index',
        KeyConditionExpression='jira_issue_key = :key',
        ExpressionAttributeValues={':key': issue_key}
    )

    if not response['Items']:
        return {'statusCode': 200, 'body': 'No session found for this JIRA issue'}

    session = response['Items'][0]

    # Skip if session is already running (prevent duplicate tasks)
    if session.get('status') == 'running':
        print(f"Session {session['session_id']} already running, skipping")
        return {'statusCode': 200, 'body': 'Session already running'}

    # Extract comment text
    comment_body = comment.get('body', '')
    if isinstance(comment_body, dict):
        comment_body = extract_text_from_adf(comment_body)

    # Resume agent with user's comment as new instruction
    start_agent_task(
        session_id=session['session_id'],
        pr_number=session['pr_number'],
        branch=session['branch'],
        prompt=comment_body,
        repo=session['repo'],
        source='jira',
        jira_issue_key=issue_key,
        jira_site=session.get('jira_site'),
        resume=True
    )

    return {'statusCode': 200, 'body': 'Session resumed from JIRA comment'}


def format_jira_pr_description(issue_key: str, summary: str, description: str, jira_site: str) -> str:
    """Format PR description from JIRA issue."""

    return f"""## Claude Cloud Agent Session

This PR was automatically created from JIRA issue [{issue_key}](https://{jira_site}/browse/{issue_key}).

### Original Request

**{summary}**

{description}

---

**Session ID:** `jira-{issue_key}`
**JIRA Link:** https://{jira_site}/browse/{issue_key}

All development work is logged below as comments.
"""


def extract_text_from_adf(adf: dict) -> str:
    """Extract plain text from Atlassian Document Format."""

    if not isinstance(adf, dict):
        return str(adf)

    text_parts = []

    def extract_recursive(node):
        if isinstance(node, dict):
            if node.get('type') == 'text':
                text_parts.append(node.get('text', ''))
            for child in node.get('content', []):
                extract_recursive(child)
        elif isinstance(node, list):
            for item in node:
                extract_recursive(item)

    extract_recursive(adf)
    return ' '.join(text_parts)


# =============================================================================
# Shared Functions
# =============================================================================

def start_agent_task(session_id: str, pr_number: int, branch: str,
                     prompt: str, repo: str, source: str = 'github',
                     jira_issue_key: str = None, jira_site: str = None,
                     resume: bool = False):
    """Start agent ECS task to run Claude Code."""

    cluster = os.environ.get('ECS_CLUSTER', 'claude-cloud-agent')
    task_definition = os.environ.get('AGENT_TASK_DEFINITION', 'claude-cloud-agent')
    subnets = os.environ.get('SUBNETS', '').split(',')
    security_groups = os.environ.get('SECURITY_GROUPS', '').split(',')

    # Escape prompt for shell
    safe_prompt = prompt.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')

    # Build environment variables
    env_vars = [
        {'name': 'SESSION_ID', 'value': session_id},
        {'name': 'PR_NUMBER', 'value': str(pr_number)},
        {'name': 'BRANCH', 'value': branch},
        {'name': 'REPO', 'value': repo},
        {'name': 'PROMPT', 'value': safe_prompt},
        {'name': 'RESUME', 'value': str(resume).lower()},
        {'name': 'SOURCE', 'value': source},
    ]

    # Add JIRA-specific env vars if applicable
    if source == 'jira':
        if jira_issue_key:
            env_vars.append({'name': 'JIRA_ISSUE_KEY', 'value': jira_issue_key})
        if jira_site:
            env_vars.append({'name': 'JIRA_SITE', 'value': jira_site})
        jira_secret_arn = os.environ.get('JIRA_SECRET_ARN')
        if jira_secret_arn:
            env_vars.append({'name': 'JIRA_SECRET_ARN', 'value': jira_secret_arn})

    try:
        response = ecs.run_task(
            cluster=cluster,
            taskDefinition=task_definition,
            launchType='FARGATE',
            enableExecuteCommand=True,  # Enable ECS Exec for UAT access
            networkConfiguration={
                'awsvpcConfiguration': {
                    'subnets': [s.strip() for s in subnets if s.strip()],
                    'securityGroups': [s.strip() for s in security_groups if s.strip()],
                    'assignPublicIp': 'ENABLED'
                }
            },
            overrides={
                'containerOverrides': [{
                    'name': os.environ.get('CONTAINER_NAME', 'claude-cloud-agent'),
                    'environment': env_vars
                }]
            },
            tags=[
                {'key': 'session_id', 'value': session_id},
                {'key': 'pr_number', 'value': str(pr_number)},
                {'key': 'source', 'value': source}
            ]
        )

        if response.get('tasks'):
            task_arn = response['tasks'][0]['taskArn']
            task_id = task_arn.split('/')[-1]  # Extract task ID from ARN
            print(f"Started agent task: {task_arn}")

            # Build UAT connection command
            container_name = os.environ.get('CONTAINER_NAME', 'claude-cloud-agent')
            uat_command = (
                f"aws ecs execute-command --cluster {cluster} "
                f"--task {task_id} --container {container_name} "
                f"--interactive --command /bin/bash"
            )

            sessions_table.update_item(
                Key={'session_id': session_id},
                UpdateExpression='SET agent_task_arn = :arn, #status = :status, uat_command = :cmd',
                ExpressionAttributeNames={'#status': 'status'},
                ExpressionAttributeValues={
                    ':arn': task_arn,
                    ':status': 'running',
                    ':cmd': uat_command
                }
            )

            # Return task info for UAT link posting
            return {'task_arn': task_arn, 'task_id': task_id, 'uat_command': uat_command}
        else:
            print(f"Failed to start agent task: {response.get('failures', [])}")
            return None

    except Exception as e:
        print(f"Error starting agent task: {e}")
        return None
