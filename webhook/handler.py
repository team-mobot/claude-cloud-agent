"""
GitHub Webhook Handler for Claude Cloud Agent
Handles issue labeling, PR comments, and PR closure events.
"""

import json
import hmac
import hashlib
import os
from datetime import datetime

import boto3
from github import Github, GithubIntegration

# AWS clients
secrets = boto3.client('secretsmanager')
dynamodb = boto3.resource('dynamodb')
ecs = boto3.client('ecs')

sessions_table = dynamodb.Table(os.environ.get('SESSION_TABLE', 'claude-cloud-agent-sessions'))


def handler(event, context):
    """Main webhook handler."""

    if not verify_signature(event):
        return {'statusCode': 401, 'body': 'Invalid signature'}

    body = json.loads(event['body'])
    event_type = event['headers'].get('x-github-event', '').lower()

    # Get the trigger label from environment (default: claude-dev)
    trigger_label = os.environ.get('TRIGGER_LABEL', 'claude-dev')

    if event_type == 'issues' and body['action'] == 'labeled':
        if body['label']['name'] == trigger_label:
            return handle_new_issue(body['issue'], body['repository'])

    elif event_type == 'issue_comment' and body['action'] == 'created':
        return handle_comment(body)

    elif event_type == 'pull_request' and body['action'] == 'closed':
        return handle_pr_closed(body['pull_request'])

    return {'statusCode': 200, 'body': 'OK'}


def verify_signature(event) -> bool:
    """Verify GitHub webhook signature."""

    headers = event.get('headers', {})
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
            import base64
            body = base64.b64decode(body).decode('utf-8')

        expected = 'sha256=' + hmac.new(
            webhook_secret.encode(),
            body.encode(),
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(signature, expected)
    except Exception as e:
        print(f"Error in verify_signature: {e}")
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


def handle_new_issue(issue: dict, repo: dict):
    """Start new dev session from issue."""

    gh = get_github_client()
    repository = gh.get_repo(repo['full_name'])
    issue_number = issue['number']
    session_id = f"issue-{issue_number}"
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
        body=format_pr_description(issue),
        head=branch_name,
        base='main',
        draft=True
    )

    # 4. Store session
    sessions_table.put_item(Item={
        'session_id': session_id,
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
    start_agent_task(
        session_id=session_id,
        pr_number=pr.number,
        branch=branch_name,
        prompt=issue['body'] or issue['title'],
        repo=repo['full_name']
    )

    return {'statusCode': 200, 'body': f'Started session {session_id}'}


def handle_comment(body: dict):
    """Handle user comment on PR - continue session."""

    if not body.get('issue', {}).get('pull_request'):
        return {'statusCode': 200, 'body': 'Not a PR comment'}

    pr_number = body['issue']['number']
    comment = body['comment']

    # Skip bot's own comments
    if comment['user']['login'].endswith('[bot]'):
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

    # Resume agent with user's comment as new instruction
    start_agent_task(
        session_id=session['session_id'],
        pr_number=pr_number,
        branch=session['branch'],
        prompt=comment['body'],
        repo=body['repository']['full_name'],
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
    sessions_table.update_item(
        Key={'session_id': session['session_id']},
        UpdateExpression='SET #status = :status, completed_at = :time',
        ExpressionAttributeNames={'#status': 'status'},
        ExpressionAttributeValues={
            ':status': 'completed' if pr.get('merged') else 'closed',
            ':time': datetime.utcnow().isoformat()
        }
    )

    # Post final comment
    gh = get_github_client()
    repo = gh.get_repo(pr['base']['repo']['full_name'])
    pr_obj = repo.get_pull(pr_number)

    status = "merged" if pr.get('merged') else "closed"
    pr_obj.create_issue_comment(
        f"**Session Complete**\n\n"
        f"PR {status}. Agent session ended.\n\n"
        f"Session ID: `{session['session_id']}`"
    )

    return {'statusCode': 200, 'body': 'Session cleaned up'}


def format_pr_description(issue: dict) -> str:
    """Format PR description from issue."""

    return f"""## Claude Cloud Agent Session

This PR was automatically created from issue #{issue['number']}.

### Original Request

{issue['body'] or issue['title']}

---

**Session ID:** `issue-{issue['number']}`

All development work is logged below as comments.
"""


def start_agent_task(session_id: str, pr_number: int, branch: str,
                     prompt: str, repo: str, resume: bool = False):
    """Start agent ECS task to run Claude Code."""

    cluster = os.environ.get('ECS_CLUSTER', 'claude-cloud-agent')
    task_definition = os.environ.get('AGENT_TASK_DEFINITION', 'claude-cloud-agent')
    subnets = os.environ.get('SUBNETS', '').split(',')
    security_groups = os.environ.get('SECURITY_GROUPS', '').split(',')

    # Escape prompt for shell
    safe_prompt = prompt.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')

    try:
        response = ecs.run_task(
            cluster=cluster,
            taskDefinition=task_definition,
            launchType='FARGATE',
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
                    'environment': [
                        {'name': 'SESSION_ID', 'value': session_id},
                        {'name': 'PR_NUMBER', 'value': str(pr_number)},
                        {'name': 'BRANCH', 'value': branch},
                        {'name': 'REPO', 'value': repo},
                        {'name': 'PROMPT', 'value': safe_prompt},
                        {'name': 'RESUME', 'value': str(resume).lower()},
                    ]
                }]
            },
            tags=[
                {'key': 'session_id', 'value': session_id},
                {'key': 'pr_number', 'value': str(pr_number)}
            ]
        )

        if response.get('tasks'):
            task_arn = response['tasks'][0]['taskArn']
            print(f"Started agent task: {task_arn}")

            sessions_table.update_item(
                Key={'session_id': session_id},
                UpdateExpression='SET agent_task_arn = :arn, #status = :status',
                ExpressionAttributeNames={'#status': 'status'},
                ExpressionAttributeValues={
                    ':arn': task_arn,
                    ':status': 'running'
                }
            )
        else:
            print(f"Failed to start agent task: {response.get('failures', [])}")

    except Exception as e:
        print(f"Error starting agent task: {e}")
