"""
Lambda webhook handler for GitHub events.

Routes GitHub webhook events to appropriate handlers:
- issues.labeled: Create branch/PR, start session
- issue_comment.created: Route to running container
- pull_request.closed: Stop session
"""

import hashlib
import hmac
import json
import logging
import os
from typing import Any

import boto3

from github_client import GitHubClient
from session_manager import SessionManager
from ecs_launcher import ECSLauncher

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
GITHUB_APP_SECRET_ARN = os.environ.get("GITHUB_APP_SECRET_ARN")
SESSIONS_TABLE = os.environ.get("SESSIONS_TABLE")
UAT_DOMAIN_SUFFIX = os.environ.get("UAT_DOMAIN_SUFFIX", "uat.teammobot.dev")
TEST_TICKETS_TASK_DEFINITION = os.environ.get("TEST_TICKETS_TASK_DEFINITION", "")
TEST_TICKETS_SECRET_ARN = os.environ.get("TEST_TICKETS_SECRET_ARN", "")
ALB_LISTENER_ARN = os.environ.get("ALB_LISTENER_ARN", "")

# Trigger label that starts a session
TRIGGER_LABEL = "claude-dev"

# test_tickets UAT configuration
TEST_TICKETS_TRIGGER_LABEL = "uat"
TEST_TICKETS_STAGING_LABEL = "uat-staging"  # Uses :staging image instead of :latest
TEST_TICKETS_CLAUDE_LABEL = "claude-dev"  # Auto-starts Claude to implement PR
TEST_TICKETS_REPO = "team-mobot/test_tickets"

# Lazy-loaded clients and secrets
_secrets_client = None
_github_app_secret = None


def get_secrets_client():
    """Get boto3 secrets manager client."""
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def get_github_app_secret() -> dict:
    """Retrieve GitHub App secret from Secrets Manager (cached)."""
    global _github_app_secret
    if _github_app_secret is None:
        client = get_secrets_client()
        response = client.get_secret_value(SecretId=GITHUB_APP_SECRET_ARN)
        _github_app_secret = json.loads(response["SecretString"])
    return _github_app_secret


def get_webhook_secret() -> str:
    """Get webhook secret from combined GitHub App secret."""
    return get_github_app_secret()["webhook_secret"]


def get_github_private_key() -> str:
    """Get private key from combined GitHub App secret."""
    return get_github_app_secret()["private_key"]


def get_github_app_id() -> str:
    """Get App ID from combined GitHub App secret."""
    return get_github_app_secret()["app_id"]


def verify_signature(payload: bytes, signature: str) -> bool:
    """
    Verify GitHub webhook signature.

    GitHub sends signature in format: sha256=<hex_digest>
    """
    if not signature or not signature.startswith("sha256="):
        logger.warning("Invalid signature format")
        return False

    secret = get_webhook_secret()
    expected_sig = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected_sig, signature)


def is_bot_comment(author: str, body: str) -> bool:
    """
    Detect bot comments to prevent infinite loops.

    Returns True if:
    - Author username ends with [bot]
    - Comment body contains our agent marker
    """
    if author.endswith("[bot]"):
        return True
    if "<!-- claude-agent -->" in body:
        return True
    return False


def lambda_handler(event: dict, context: Any) -> dict:
    """
    Main Lambda handler for GitHub webhooks.
    """
    logger.info(f"Received event: {json.dumps(event)[:500]}...")

    # Extract headers and body
    headers = event.get("headers", {})
    body = event.get("body", "")

    # Handle base64 encoding
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")

    # Verify webhook signature
    signature = headers.get("x-hub-signature-256", "")
    if not verify_signature(body.encode("utf-8"), signature):
        logger.error("Webhook signature verification failed")
        return {
            "statusCode": 401,
            "body": json.dumps({"error": "Invalid signature"})
        }

    # Parse event
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse webhook payload: {e}")
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "Invalid JSON"})
        }

    # Get event type
    event_type = headers.get("x-github-event", "")
    action = payload.get("action", "")

    logger.info(f"Processing {event_type}.{action}")

    # Initialize clients
    installation_id = payload.get("installation", {}).get("id")
    if not installation_id:
        logger.error("No installation ID in payload")
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "Missing installation ID"})
        }

    github = GitHubClient(
        app_id=get_github_app_id(),
        private_key=get_github_private_key(),
        installation_id=installation_id
    )
    sessions = SessionManager(table_name=SESSIONS_TABLE)
    ecs = ECSLauncher()

    # Route to appropriate handler
    try:
        if event_type == "issues" and action == "labeled":
            return handle_issue_labeled(payload, github, sessions, ecs)
        elif event_type == "issues" and action in ("unlabeled", "closed"):
            return handle_issue_unlabeled_or_closed(payload, github, sessions, ecs)
        elif event_type == "issue_comment" and action == "created":
            return handle_issue_comment(payload, github, sessions)
        elif event_type == "pull_request" and action == "labeled":
            return handle_pr_labeled(payload, github, sessions, ecs)
        elif event_type == "pull_request" and action in ("unlabeled", "closed"):
            return handle_pr_unlabeled_or_closed(payload, github, sessions, ecs)
        else:
            logger.info(f"Ignoring event: {event_type}.{action}")
            return {
                "statusCode": 200,
                "body": json.dumps({"message": "Event ignored"})
            }
    except Exception as e:
        logger.exception(f"Error handling {event_type}.{action}: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }


def handle_issue_labeled(
    payload: dict,
    github: GitHubClient,
    sessions: SessionManager,
    ecs: ECSLauncher
) -> dict:
    """
    Handle issue labeled event.

    When an issue is labeled with TRIGGER_LABEL:
    1. Create a feature branch
    2. Create a draft PR
    3. Create session in DynamoDB
    4. Launch ECS task

    When test_tickets repo is labeled with 'uat':
    1. Start UAT environment for the branch
    """
    issue = payload.get("issue", {})
    label = payload.get("label", {})
    repo = payload.get("repository", {})
    label_name = label.get("name", "")
    repo_full_name = repo.get("full_name", "")

    # Check for test_tickets UAT trigger (uat or uat-staging)
    if (
        repo_full_name == TEST_TICKETS_REPO
        and label_name in (TEST_TICKETS_TRIGGER_LABEL, TEST_TICKETS_STAGING_LABEL)
        and TEST_TICKETS_TASK_DEFINITION
    ):
        return handle_test_tickets_uat(payload, github, sessions, ecs)

    # Check if this is our trigger label
    if label_name != TRIGGER_LABEL:
        logger.info(f"Ignoring label: {label.get('name')}")
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "Not trigger label"})
        }

    issue_number = issue.get("number")
    issue_title = issue.get("title", "")
    issue_body = issue.get("body", "")
    repo_clone_url = repo.get("clone_url")
    default_branch = repo.get("default_branch", "main")

    logger.info(f"Starting session for issue #{issue_number} in {repo_full_name}")

    # Check for existing session
    existing = sessions.get_session_by_pr(repo_full_name, issue_number)
    if existing and existing.get("status") in ("STARTING", "RUNNING"):
        logger.info(f"Session already exists: {existing['session_id']}")
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Session already exists",
                "session_id": existing["session_id"]
            })
        }

    # Create branch name
    branch_name = f"claude/{issue_number}"

    # Create branch from default branch
    logger.info(f"Creating branch {branch_name} from {default_branch}")
    github.create_branch(repo_full_name, branch_name, default_branch)

    # Create initial commit (required for PR creation - branch must differ from base)
    import uuid
    session_id = str(uuid.uuid4())[:8]
    session_file_content = f"""# Claude Agent Session

Session ID: {session_id}
Issue: #{issue_number}
Repository: {repo_full_name}
Created: {__import__('datetime').datetime.utcnow().isoformat()}Z

This file tracks the Claude Cloud Agent session working on this branch.
"""
    logger.info(f"Creating initial commit on {branch_name}")
    github.create_or_update_file(
        repo_full_name,
        ".claude-session",
        f"chore: initialize Claude agent session for issue #{issue_number}",
        session_file_content,
        branch_name
    )

    # Create draft PR
    pr_title = f"[Claude] {issue_title}"
    pr_body = f"""## Automated Implementation

This PR was created by Claude Cloud Agent to implement issue #{issue_number}.

### Original Issue
{issue_body}

### UAT Preview
URL will be posted when the agent is ready.

---
*This PR is being worked on by an automated agent. Comment on this PR to provide feedback.*

<!-- claude-agent -->
"""

    logger.info(f"Creating draft PR: {pr_title}")
    pr = github.create_pull_request(
        repo_full_name,
        title=pr_title,
        body=pr_body,
        head=branch_name,
        base=default_branch,
        draft=True
    )
    pr_number = pr["number"]

    # Create session record (session_id was generated earlier for the initial commit)
    logger.info(f"Creating session {session_id}")
    sessions.create_session(
        session_id=session_id,
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        pr_number=pr_number,
        branch_name=branch_name
    )

    # Launch ECS task with GitHub token for cloning
    logger.info(f"Launching ECS task for session {session_id}")
    task_arn = ecs.launch_agent_task(
        session_id=session_id,
        repo_clone_url=repo_clone_url,
        branch_name=branch_name,
        issue_number=issue_number,
        pr_number=pr_number,
        initial_prompt=f"Issue #{issue_number}: {issue_title}\n\n{issue_body}",
        github_token=github.get_token(),
        installation_id=payload.get("installation", {}).get("id", 0),
        repo_full_name=repo_full_name
    )

    # Update session with task ARN
    sessions.update_session(session_id, task_arn=task_arn)

    logger.info(f"Session {session_id} started successfully")
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Session started",
            "session_id": session_id,
            "pr_number": pr_number,
            "task_arn": task_arn
        })
    }


def handle_issue_comment(
    payload: dict,
    github: GitHubClient,
    sessions: SessionManager
) -> dict:
    """
    Handle PR comment event.

    Routes comments to running containers via their prompt API.
    """
    comment = payload.get("comment", {})
    issue = payload.get("issue", {})
    repo = payload.get("repository", {})

    # Only handle PR comments (issues with pull_request key)
    if "pull_request" not in issue:
        logger.info("Comment is on issue, not PR - ignoring")
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "Not a PR comment"})
        }

    comment_author = comment.get("user", {}).get("login", "")
    comment_body = comment.get("body", "")
    pr_number = issue.get("number")
    repo_full_name = repo.get("full_name")

    # Check for bot comment
    if is_bot_comment(comment_author, comment_body):
        logger.info(f"Ignoring bot comment from {comment_author}")
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "Bot comment ignored"})
        }

    # Look up session
    session = sessions.get_session_by_pr(repo_full_name, pr_number)
    if not session:
        logger.info(f"No session found for PR #{pr_number}")
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "No active session"})
        }

    if session.get("status") != "RUNNING":
        logger.info(f"Session {session['session_id']} not running: {session.get('status')}")
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "Session not running"})
        }

    container_ip = session.get("container_ip")
    if not container_ip:
        logger.warning(f"Session {session['session_id']} has no container IP")
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "Container not ready"})
        }

    # Forward comment to container with GitHub context for posting responses
    import requests
    try:
        prompt_url = f"http://{container_ip}:8080/prompt"
        logger.info(f"Forwarding comment to {prompt_url}")

        # Parse owner/repo from full_name
        owner, repo_name = repo_full_name.split("/", 1)

        response = requests.post(
            prompt_url,
            json={
                "prompt": comment_body,
                "github": {
                    "owner": owner,
                    "repo": repo_name,
                    "prNumber": pr_number,
                    "token": github.get_token()
                }
            },
            timeout=30  # Prompt server should return quickly after accepting
        )
        response.raise_for_status()

        logger.info(f"Comment forwarded successfully")
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Comment forwarded",
                "session_id": session["session_id"]
            })
        }
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to forward comment: {e}")
        # Don't mark session as failed - just log the error
        # The container might still be processing or temporarily unavailable

        return {
            "statusCode": 200,
            "body": json.dumps({"message": f"Failed to forward: {e}"})
        }


def handle_pr_closed(
    payload: dict,
    github: GitHubClient,
    sessions: SessionManager,
    ecs: ECSLauncher
) -> dict:
    """
    Handle PR closed event.

    Stops the ECS task and marks session as completed.
    """
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})

    pr_number = pr.get("number")
    repo_full_name = repo.get("full_name")
    merged = pr.get("merged", False)

    # Look up session
    session = sessions.get_session_by_pr(repo_full_name, pr_number)
    if not session:
        logger.info(f"No session found for PR #{pr_number}")
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "No session to clean up"})
        }

    session_id = session["session_id"]
    task_arn = session.get("task_arn")

    logger.info(f"Closing session {session_id} (merged={merged})")

    # Stop ECS task if running
    if task_arn and session.get("status") in ("STARTING", "RUNNING"):
        try:
            ecs.stop_task(task_arn)
            logger.info(f"Stopped task {task_arn}")
        except Exception as e:
            logger.error(f"Failed to stop task: {e}")

    # Update session status
    status = "COMPLETED" if merged else "COMPLETED"
    sessions.update_session(session_id, status=status)

    logger.info(f"Session {session_id} marked as {status}")
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Session closed",
            "session_id": session_id,
            "merged": merged
        })
    }


def cleanup_uat_resources(session: dict) -> None:
    """
    Clean up ALB target group and listener rule for a UAT session.

    Args:
        session: Session dict from DynamoDB containing target_group_arn
    """
    target_group_arn = session.get("target_group_arn")
    session_id = session.get("session_id", "")

    if not target_group_arn:
        logger.info(f"No target group ARN for session {session_id}, skipping cleanup")
        return

    # Skip cleanup for shared target group
    if "test-tickets-uat-tg" in target_group_arn:
        logger.info(f"Session {session_id} uses shared target group, skipping cleanup")
        return

    elbv2 = boto3.client("elbv2")

    try:
        # Find and delete ALB listener rules pointing to this target group
        if ALB_LISTENER_ARN:
            rules = elbv2.describe_rules(ListenerArn=ALB_LISTENER_ARN)
            for rule in rules.get("Rules", []):
                if rule.get("IsDefault"):
                    continue
                actions = rule.get("Actions", [])
                for action in actions:
                    if action.get("TargetGroupArn") == target_group_arn:
                        rule_arn = rule.get("RuleArn")
                        logger.info(f"Deleting ALB rule {rule_arn}")
                        elbv2.delete_rule(RuleArn=rule_arn)
                        break

        # Delete target group
        logger.info(f"Deleting target group {target_group_arn}")
        elbv2.delete_target_group(TargetGroupArn=target_group_arn)
        logger.info(f"Cleaned up UAT resources for session {session_id}")

    except Exception as e:
        logger.error(f"Error cleaning up UAT resources for {session_id}: {e}")


def handle_issue_unlabeled_or_closed(
    payload: dict,
    github: GitHubClient,
    sessions: SessionManager,
    ecs: ECSLauncher
) -> dict:
    """
    Handle issue unlabeled or closed event.

    Stops UAT environment when 'uat' label is removed or issue is closed.
    """
    issue = payload.get("issue", {})
    repo = payload.get("repository", {})
    action = payload.get("action")
    label = payload.get("label", {})

    issue_number = issue.get("number")
    repo_full_name = repo.get("full_name")

    # Only handle test_tickets repo
    if repo_full_name != TEST_TICKETS_REPO:
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "Not test_tickets repo"})
        }

    # For unlabeled events, only handle 'uat' or 'uat-staging' label removal
    if action == "unlabeled" and label.get("name") not in (TEST_TICKETS_TRIGGER_LABEL, TEST_TICKETS_STAGING_LABEL):
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "Not uat or uat-staging label"})
        }

    # Look up session by issue number
    session = sessions.get_session_by_pr(repo_full_name, issue_number)
    if not session:
        logger.info(f"No UAT session found for issue #{issue_number}")
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "No session to clean up"})
        }

    session_id = session["session_id"]
    task_arn = session.get("task_arn")

    logger.info(f"Stopping UAT session {session_id} (action={action})")

    # Stop ECS task if running
    if task_arn and session.get("status") in ("STARTING", "RUNNING"):
        try:
            ecs.stop_task(task_arn, reason=f"Issue {action}")
            logger.info(f"Stopped task {task_arn}")
        except Exception as e:
            logger.error(f"Failed to stop task: {e}")

    # Clean up ALB resources
    cleanup_uat_resources(session)

    # Update session status
    sessions.update_session(session_id, status="STOPPED")

    # Post comment to issue
    try:
        issue_obj = github.get_issue(repo_full_name, issue_number)
        issue_obj.create_comment(
            f"<!-- claude-agent -->\n**UAT Environment Stopped**\n\n"
            f"The UAT environment for `{session_id}` has been shut down."
        )
    except Exception as e:
        logger.error(f"Failed to post comment: {e}")

    logger.info(f"UAT session {session_id} stopped")
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "UAT session stopped",
            "session_id": session_id
        })
    }


def handle_test_tickets_uat(
    payload: dict,
    github: GitHubClient,
    sessions: SessionManager,
    ecs: ECSLauncher
) -> dict:
    """
    Handle test_tickets UAT deployment.

    When an issue in test_tickets is labeled with 'uat' or 'uat-staging':
    1. Parse branch name from issue (or use issue number)
    2. Create unique session ID
    3. Launch test_tickets ECS task with PostgreSQL
    4. Post UAT URL to issue

    Uses :staging image for 'uat-staging' label, :latest for 'uat' label.
    """
    import uuid
    from datetime import datetime

    issue = payload.get("issue", {})
    repo = payload.get("repository", {})
    label = payload.get("label", {})
    label_name = label.get("name", "")

    issue_number = issue.get("number")
    issue_title = issue.get("title", "")
    issue_body = issue.get("body", "") or ""
    repo_full_name = repo.get("full_name")

    # Determine image tag based on label
    is_staging = label_name == TEST_TICKETS_STAGING_LABEL
    image_tag = "staging" if is_staging else "latest"

    logger.info(f"Starting test_tickets UAT for issue #{issue_number} in {repo_full_name} (image_tag={image_tag})")

    # Parse branch name from issue body or title
    # Look for patterns like "branch: feature/xyz" or use issue number
    branch_name = None
    for line in issue_body.split("\n"):
        line_lower = line.lower().strip()
        if line_lower.startswith("branch:"):
            branch_name = line.split(":", 1)[1].strip()
            break

    if not branch_name:
        # Default to issue number based branch
        branch_name = f"feature/issue-{issue_number}"

    # Create session ID (sanitize branch name for subdomain)
    safe_branch = branch_name.replace("/", "-").replace("_", "-").lower()
    session_id = f"tt-{safe_branch}"[:50]  # Limit length for subdomain

    # Check for existing session
    existing = sessions.get_session(session_id)
    if existing and existing.get("status") in ("STARTING", "RUNNING"):
        uat_url = f"https://{session_id}.{UAT_DOMAIN_SUFFIX}"
        logger.info(f"UAT session already exists: {session_id}")

        # Post reminder comment
        github.create_issue_comment(
            repo_full_name,
            issue_number,
            f"<!-- claude-agent -->\n**UAT Already Running**\n\n"
            f"URL: {uat_url}\n\n"
            f"Session: `{session_id}`"
        )

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "UAT session already exists",
                "session_id": session_id,
                "uat_url": uat_url
            })
        }

    # Create session record
    logger.info(f"Creating test_tickets UAT session {session_id}")
    sessions.create_session(
        session_id=session_id,
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        pr_number=issue_number,  # Use issue number as PR number for lookups
        branch_name=branch_name
    )

    # Launch test_tickets ECS task
    logger.info(f"Launching test_tickets ECS task for session {session_id} (image_tag={image_tag})")
    try:
        task_arn = ecs.launch_test_tickets_task(
            session_id=session_id,
            branch=branch_name,
            pr_number=issue_number,
            repo=repo_full_name,
            github_token=github.get_token(),
            image_tag=image_tag
        )
    except Exception as e:
        logger.error(f"Failed to launch test_tickets task: {e}")
        sessions.update_session(session_id, status="FAILED")

        github.create_issue_comment(
            repo_full_name,
            issue_number,
            f"<!-- claude-agent -->\n:x: **UAT Failed to Start**\n\n"
            f"Error: {str(e)}\n\n"
            f"Please check the configuration and try again."
        )

        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }

    # Update session with task ARN
    uat_url = f"https://{session_id}.{UAT_DOMAIN_SUFFIX}"
    sessions.update_session(
        session_id,
        task_arn=task_arn,
        uat_url=uat_url
    )

    # Comment will be posted by the container once fully started

    logger.info(f"test_tickets UAT session {session_id} started successfully")
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "UAT started",
            "session_id": session_id,
            "uat_url": uat_url,
            "task_arn": task_arn
        })
    }


def handle_pr_labeled(
    payload: dict,
    github: GitHubClient,
    sessions: SessionManager,
    ecs: ECSLauncher
) -> dict:
    """
    Handle PR labeled event.

    When a PR in test_tickets is labeled with 'uat' or 'claude-dev':
    1. Get the branch name from the PR
    2. Create unique session ID
    3. Launch test_tickets ECS task
    4. Post UAT URL to PR
    5. If 'claude-dev', queue initial prompt for auto-implementation
    """
    pr = payload.get("pull_request", {})
    label = payload.get("label", {})
    repo = payload.get("repository", {})

    label_name = label.get("name", "")
    repo_full_name = repo.get("full_name", "")

    # Only handle test_tickets repo
    if repo_full_name != TEST_TICKETS_REPO:
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "Not test_tickets repo"})
        }

    # Check for uat, uat-staging, or claude-dev label
    is_uat = label_name == TEST_TICKETS_TRIGGER_LABEL
    is_staging = label_name == TEST_TICKETS_STAGING_LABEL
    is_claude_dev = label_name == TEST_TICKETS_CLAUDE_LABEL

    if not (is_uat or is_staging or is_claude_dev):
        logger.info(f"Ignoring label: {label_name}")
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "Not uat, uat-staging, or claude-dev label"})
        }

    # Determine image tag: staging label uses :staging, others use :latest
    image_tag = "staging" if is_staging else "latest"

    if not TEST_TICKETS_TASK_DEFINITION:
        logger.error("TEST_TICKETS_TASK_DEFINITION not configured")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Task definition not configured"})
        }

    pr_number = pr.get("number")
    pr_title = pr.get("title", "")
    pr_body = pr.get("body", "") or ""
    branch_name = pr.get("head", {}).get("ref", "")

    if not branch_name:
        logger.error(f"No branch found for PR #{pr_number}")
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "No branch found"})
        }

    logger.info(f"Starting test_tickets UAT for PR #{pr_number} branch {branch_name} (claude_dev={is_claude_dev}, image_tag={image_tag})")

    # Create session ID (sanitize branch name for subdomain)
    safe_branch = branch_name.replace("/", "-").replace("_", "-").lower()
    session_id = f"tt-{safe_branch}"[:50]  # Limit length for subdomain

    # Check for existing session
    existing = sessions.get_session(session_id)
    if existing and existing.get("status") in ("STARTING", "RUNNING"):
        uat_url = f"https://{session_id}.{UAT_DOMAIN_SUFFIX}"
        logger.info(f"UAT session already exists: {session_id}")

        # Post reminder comment on PR
        github.create_issue_comment(
            repo_full_name,
            pr_number,
            f"<!-- claude-agent -->\n**UAT Already Running**\n\n"
            f"URL: {uat_url}\n\n"
            f"Session: `{session_id}`"
        )

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "UAT session already exists",
                "session_id": session_id,
                "uat_url": uat_url
            })
        }

    # Create session record
    logger.info(f"Creating test_tickets UAT session {session_id}")
    sessions.create_session(
        session_id=session_id,
        repo_full_name=repo_full_name,
        issue_number=pr_number,
        pr_number=pr_number,
        branch_name=branch_name
    )

    # If claude-dev, prepare initial prompt for auto-implementation
    initial_prompt = None
    if is_claude_dev:
        initial_prompt = f"""Implement this PR:

## {pr_title}

{pr_body}

Read the codebase, understand the requirements, and implement the changes.
Commit your work when complete and push to the remote branch."""
        logger.info(f"Queuing initial prompt for claude-dev: {initial_prompt[:100]}...")

    # Launch test_tickets ECS task
    logger.info(f"Launching test_tickets ECS task for session {session_id} (image_tag={image_tag})")
    try:
        task_arn = ecs.launch_test_tickets_task(
            session_id=session_id,
            branch=branch_name,
            pr_number=pr_number,
            repo=repo_full_name,
            github_token=github.get_token(),
            image_tag=image_tag
        )
    except Exception as e:
        logger.error(f"Failed to launch test_tickets task: {e}")
        sessions.update_session(session_id, status="FAILED")

        github.create_issue_comment(
            repo_full_name,
            pr_number,
            f"<!-- claude-agent -->\n:x: **UAT Failed to Start**\n\n"
            f"Error: {str(e)}\n\n"
            f"Please check the configuration and try again."
        )

        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }

    # Update session with task ARN and initial_prompt (if claude-dev)
    uat_url = f"https://{session_id}.{UAT_DOMAIN_SUFFIX}"
    sessions.update_session(
        session_id,
        task_arn=task_arn,
        uat_url=uat_url,
        initial_prompt=initial_prompt
    )

    # Comment will be posted by the container once fully started

    logger.info(f"test_tickets UAT session {session_id} started from PR (claude_dev={is_claude_dev}, image_tag={image_tag})")
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "UAT started",
            "session_id": session_id,
            "uat_url": uat_url,
            "task_arn": task_arn,
            "claude_dev": is_claude_dev,
            "image_tag": image_tag
        })
    }


def handle_pr_unlabeled_or_closed(
    payload: dict,
    github: GitHubClient,
    sessions: SessionManager,
    ecs: ECSLauncher
) -> dict:
    """
    Handle PR unlabeled or closed event.

    Stops UAT environment when 'uat' label is removed or PR is closed/merged.
    """
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    action = payload.get("action")
    label = payload.get("label", {})

    pr_number = pr.get("number")
    repo_full_name = repo.get("full_name")
    branch_name = pr.get("head", {}).get("ref", "")

    # For non-test_tickets repos, delegate to original PR closed handler
    if repo_full_name != TEST_TICKETS_REPO:
        if action == "closed":
            return handle_pr_closed(payload, github, sessions, ecs)
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "Not test_tickets repo"})
        }

    # For unlabeled events, only handle 'uat', 'uat-staging', or 'claude-dev' label removal
    if action == "unlabeled":
        removed_label = label.get("name")
        if removed_label not in (TEST_TICKETS_TRIGGER_LABEL, TEST_TICKETS_STAGING_LABEL, TEST_TICKETS_CLAUDE_LABEL):
            return {
                "statusCode": 200,
                "body": json.dumps({"message": "Not uat, uat-staging, or claude-dev label"})
            }

    # For closed events, check if the PR had uat, uat-staging, or claude-dev label
    if action == "closed":
        labels = pr.get("labels", [])
        label_names = [l.get("name") for l in labels]
        has_trigger_label = (
            TEST_TICKETS_TRIGGER_LABEL in label_names or
            TEST_TICKETS_STAGING_LABEL in label_names or
            TEST_TICKETS_CLAUDE_LABEL in label_names
        )
        if not has_trigger_label:
            return {
                "statusCode": 200,
                "body": json.dumps({"message": "PR did not have uat, uat-staging, or claude-dev label"})
            }

    # Derive session_id from branch name (same logic as handle_pr_labeled)
    if branch_name:
        safe_branch = branch_name.replace("/", "-").replace("_", "-").lower()
        session_id = f"tt-{safe_branch}"[:50]
    else:
        # Fallback: look up by PR number
        session = sessions.get_session_by_pr(repo_full_name, pr_number)
        if not session:
            logger.info(f"No UAT session found for PR #{pr_number}")
            return {
                "statusCode": 200,
                "body": json.dumps({"message": "No session to clean up"})
            }
        session_id = session["session_id"]

    # Look up session
    session = sessions.get_session(session_id)
    if not session:
        logger.info(f"No UAT session found: {session_id}")
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "No session to clean up"})
        }

    task_arn = session.get("task_arn")

    logger.info(f"Stopping UAT session {session_id} (action={action})")

    # Stop ECS task if running
    if task_arn and session.get("status") in ("STARTING", "RUNNING"):
        try:
            ecs.stop_task(task_arn, reason=f"PR {action}")
            logger.info(f"Stopped task {task_arn}")
        except Exception as e:
            logger.error(f"Failed to stop task: {e}")

    # Clean up ALB resources
    cleanup_uat_resources(session)

    # Update session status
    sessions.update_session(session_id, status="STOPPED")

    # Post comment to PR
    try:
        github.create_issue_comment(
            repo_full_name,
            pr_number,
            f"<!-- claude-agent -->\n**UAT Environment Stopped**\n\n"
            f"The UAT environment for `{session_id}` has been shut down."
        )
    except Exception as e:
        logger.error(f"Failed to post comment: {e}")

    logger.info(f"UAT session {session_id} stopped")
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "UAT session stopped",
            "session_id": session_id
        })
    }
