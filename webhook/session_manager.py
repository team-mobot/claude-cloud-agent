"""
Session manager for DynamoDB operations.

Manages session state for running agent containers.
"""

import logging
import time
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)


class SessionManager:
    """
    Manages agent sessions in DynamoDB.

    Session lifecycle:
    STARTING -> RUNNING -> COMPLETED
                  |
                  v
               FAILED
    """

    # Session TTL: 7 days after completion
    SESSION_TTL_DAYS = 7

    def __init__(self, table_name: str):
        """
        Initialize session manager.

        Args:
            table_name: DynamoDB table name
        """
        self.table_name = table_name
        self._dynamodb = None
        self._table = None

    @property
    def dynamodb(self):
        """Get DynamoDB resource."""
        if self._dynamodb is None:
            self._dynamodb = boto3.resource("dynamodb")
        return self._dynamodb

    @property
    def table(self):
        """Get DynamoDB table."""
        if self._table is None:
            self._table = self.dynamodb.Table(self.table_name)
        return self._table

    def create_session(
        self,
        session_id: str,
        repo_full_name: str,
        issue_number: int,
        pr_number: int,
        branch_name: str,
        source: str = "github",
        jira_issue_key: Optional[str] = None
    ) -> dict:
        """
        Create a new session.

        Args:
            session_id: Unique session identifier
            repo_full_name: GitHub repository (owner/repo)
            issue_number: Source issue number
            pr_number: Created PR number
            branch_name: Feature branch name
            source: Trigger source ("github" or "jira")
            jira_issue_key: JIRA issue key if triggered from JIRA (e.g., "AGNTS-118")

        Returns:
            Created session record
        """
        now = int(time.time())

        item = {
            "session_id": session_id,
            "repo_full_name": repo_full_name,
            "issue_number": issue_number,
            "pr_number": pr_number,
            "branch_name": branch_name,
            "status": "STARTING",
            "source": source,
            "created_at": now,
            "updated_at": now
        }

        if jira_issue_key:
            item["jira_issue_key"] = jira_issue_key

        self.table.put_item(Item=item)
        logger.info(f"Created session {session_id} for {repo_full_name} PR #{pr_number} (source={source})")

        return item

    def get_session(self, session_id: str) -> Optional[dict]:
        """
        Get a session by ID.

        Args:
            session_id: Session identifier

        Returns:
            Session record or None
        """
        response = self.table.get_item(Key={"session_id": session_id})
        return response.get("Item")

    def get_session_by_pr(self, repo_full_name: str, pr_number: int) -> Optional[dict]:
        """
        Get session by repository and PR number.

        Uses the pr-index GSI.

        Args:
            repo_full_name: GitHub repository (owner/repo)
            pr_number: PR number

        Returns:
            Session record or None
        """
        response = self.table.query(
            IndexName="pr-index",
            KeyConditionExpression=Key("repo_full_name").eq(repo_full_name) & Key("pr_number").eq(pr_number)
        )

        items = response.get("Items", [])
        if not items:
            return None

        # Return most recent session for this PR
        return max(items, key=lambda x: x.get("created_at", 0))

    def update_session(
        self,
        session_id: str,
        status: Optional[str] = None,
        container_ip: Optional[str] = None,
        task_arn: Optional[str] = None,
        uat_url: Optional[str] = None,
        last_activity: Optional[int] = None,
        claude_session_id: Optional[str] = None,
        initial_prompt: Optional[str] = None
    ) -> dict:
        """
        Update a session.

        Args:
            session_id: Session identifier
            status: New status (STARTING, RUNNING, COMPLETED, FAILED)
            container_ip: Container's public IP address
            task_arn: ECS task ARN
            uat_url: UAT preview URL
            last_activity: Timestamp of last activity
            claude_session_id: Claude session ID for --resume continuity
            initial_prompt: Auto-start prompt for claude-dev label

        Returns:
            Updated session record
        """
        update_expr_parts = ["#updated_at = :updated_at"]
        expr_names = {"#updated_at": "updated_at"}
        expr_values = {":updated_at": int(time.time())}

        if status is not None:
            update_expr_parts.append("#status = :status")
            expr_names["#status"] = "status"
            expr_values[":status"] = status

            # Set TTL when session completes
            if status in ("COMPLETED", "FAILED"):
                ttl = int(time.time()) + (self.SESSION_TTL_DAYS * 24 * 60 * 60)
                update_expr_parts.append("#ttl = :ttl")
                expr_names["#ttl"] = "ttl"
                expr_values[":ttl"] = ttl

        if container_ip is not None:
            update_expr_parts.append("#container_ip = :container_ip")
            expr_names["#container_ip"] = "container_ip"
            expr_values[":container_ip"] = container_ip

        if task_arn is not None:
            update_expr_parts.append("#task_arn = :task_arn")
            expr_names["#task_arn"] = "task_arn"
            expr_values[":task_arn"] = task_arn

        if uat_url is not None:
            update_expr_parts.append("#uat_url = :uat_url")
            expr_names["#uat_url"] = "uat_url"
            expr_values[":uat_url"] = uat_url

        if last_activity is not None:
            update_expr_parts.append("#last_activity = :last_activity")
            expr_names["#last_activity"] = "last_activity"
            expr_values[":last_activity"] = last_activity

        if claude_session_id is not None:
            update_expr_parts.append("#claude_session_id = :claude_session_id")
            expr_names["#claude_session_id"] = "claude_session_id"
            expr_values[":claude_session_id"] = claude_session_id

        if initial_prompt is not None:
            update_expr_parts.append("#initial_prompt = :initial_prompt")
            expr_names["#initial_prompt"] = "initial_prompt"
            expr_values[":initial_prompt"] = initial_prompt

        update_expr = "SET " + ", ".join(update_expr_parts)

        response = self.table.update_item(
            Key={"session_id": session_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
            ReturnValues="ALL_NEW"
        )

        logger.info(f"Updated session {session_id}: {list(expr_values.keys())}")
        return response.get("Attributes", {})

    def mark_running(self, session_id: str, container_ip: str) -> dict:
        """
        Mark session as running with container IP.

        Args:
            session_id: Session identifier
            container_ip: Container's public IP

        Returns:
            Updated session record
        """
        return self.update_session(
            session_id,
            status="RUNNING",
            container_ip=container_ip,
            last_activity=int(time.time())
        )

    def mark_failed(self, session_id: str) -> dict:
        """
        Mark session as failed.

        Args:
            session_id: Session identifier

        Returns:
            Updated session record
        """
        return self.update_session(session_id, status="FAILED")

    def mark_completed(self, session_id: str) -> dict:
        """
        Mark session as completed.

        Args:
            session_id: Session identifier

        Returns:
            Updated session record
        """
        return self.update_session(session_id, status="COMPLETED")
