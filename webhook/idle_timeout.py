"""
Idle timeout handler for UAT sessions.

Runs on a schedule to stop idle sessions and clean up resources.
"""

import logging
import os
import time
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configuration
SESSIONS_TABLE = os.environ.get("SESSIONS_TABLE")
IDLE_TIMEOUT_MINUTES = int(os.environ.get("IDLE_TIMEOUT_MINUTES", "60"))
ALB_LISTENER_ARN = os.environ.get("ALB_LISTENER_ARN", "")

# AWS clients
dynamodb = boto3.resource("dynamodb")
ecs = boto3.client("ecs")
elbv2 = boto3.client("elbv2")


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


def stop_session(session: dict) -> None:
    """
    Stop an idle session.

    Args:
        session: Session dict from DynamoDB
    """
    session_id = session.get("session_id")
    task_arn = session.get("task_arn")

    logger.info(f"Stopping idle session {session_id}")

    # Stop ECS task if running
    if task_arn:
        try:
            # Extract cluster from task ARN
            # Format: arn:aws:ecs:region:account:task/cluster/task-id
            cluster = task_arn.split("/")[1] if "/" in task_arn else "test-tickets-uat"
            ecs.stop_task(
                cluster=cluster,
                task=task_arn,
                reason="Idle timeout"
            )
            logger.info(f"Stopped task {task_arn}")
        except Exception as e:
            logger.error(f"Failed to stop task {task_arn}: {e}")

    # Clean up ALB resources
    cleanup_uat_resources(session)

    # Update session status
    table = dynamodb.Table(SESSIONS_TABLE)
    table.update_item(
        Key={"session_id": session_id},
        UpdateExpression="SET #status = :status, #updated_at = :updated_at",
        ExpressionAttributeNames={
            "#status": "status",
            "#updated_at": "updated_at"
        },
        ExpressionAttributeValues={
            ":status": "STOPPED",
            ":updated_at": int(time.time())
        }
    )

    logger.info(f"Session {session_id} marked as STOPPED")


def lambda_handler(event: dict, context: Any) -> dict:
    """
    Main Lambda handler for idle timeout checking.

    Scans for RUNNING sessions and stops those that have been idle
    for longer than IDLE_TIMEOUT_MINUTES.
    """
    logger.info(f"Checking for idle sessions (timeout: {IDLE_TIMEOUT_MINUTES} minutes)")

    if not SESSIONS_TABLE:
        logger.error("SESSIONS_TABLE not configured")
        return {"statusCode": 500, "body": "SESSIONS_TABLE not configured"}

    table = dynamodb.Table(SESSIONS_TABLE)
    now = int(time.time())
    timeout_threshold = now - (IDLE_TIMEOUT_MINUTES * 60)

    # Scan for RUNNING sessions
    # Note: In production with many sessions, use a GSI on status
    response = table.scan(
        FilterExpression="#status = :status",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": "RUNNING"}
    )

    sessions = response.get("Items", [])
    logger.info(f"Found {len(sessions)} running sessions")

    stopped_count = 0
    for session in sessions:
        session_id = session.get("session_id")

        # Get last activity time (or fall back to created_at)
        last_activity = session.get("last_activity") or session.get("created_at") or 0

        if isinstance(last_activity, dict):
            # Handle DynamoDB Decimal type
            last_activity = int(last_activity.get("N", 0))
        last_activity = int(last_activity)

        idle_minutes = (now - last_activity) // 60

        if last_activity < timeout_threshold:
            logger.info(f"Session {session_id} is idle ({idle_minutes} minutes)")
            stop_session(session)
            stopped_count += 1
        else:
            logger.info(f"Session {session_id} is active (idle {idle_minutes} minutes)")

    logger.info(f"Stopped {stopped_count} idle sessions")

    return {
        "statusCode": 200,
        "body": f"Checked {len(sessions)} sessions, stopped {stopped_count} idle"
    }
