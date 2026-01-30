"""
ECS task launcher for agent containers.

Launches Fargate tasks for new agent sessions.
"""

import logging
import os
from typing import Optional

import boto3

logger = logging.getLogger(__name__)


class ECSLauncher:
    """
    Launches and manages ECS Fargate tasks for agent containers.
    """

    def __init__(self):
        """Initialize ECS launcher with configuration from environment."""
        self.cluster = os.environ.get("ECS_CLUSTER")
        self.task_definition = os.environ.get("AGENT_TASK_DEFINITION")
        self.security_group = os.environ.get("AGENT_SECURITY_GROUP")
        self.subnets = os.environ.get("AGENT_SUBNETS", "").split(",")
        self.uat_domain_suffix = os.environ.get("UAT_DOMAIN_SUFFIX", "uat.teammobot.dev")
        self.sessions_table = os.environ.get("SESSIONS_TABLE")
        # test_tickets specific configuration
        self.test_tickets_task_definition = os.environ.get("TEST_TICKETS_TASK_DEFINITION", "")
        self.test_tickets_image_uri = os.environ.get(
            "TEST_TICKETS_IMAGE_URI",
            "678954237808.dkr.ecr.us-east-1.amazonaws.com/test-tickets-uat"
        )
        self._ecs_client = None

    @property
    def ecs(self):
        """Get ECS client."""
        if self._ecs_client is None:
            self._ecs_client = boto3.client("ecs")
        return self._ecs_client

    def launch_agent_task(
        self,
        session_id: str,
        repo_clone_url: str,
        branch_name: str,
        issue_number: int,
        pr_number: int,
        initial_prompt: str,
        github_token: str = "",
        installation_id: int = 0,
        repo_full_name: str = ""
    ) -> str:
        """
        Launch an ECS task for an agent session.

        Args:
            session_id: Unique session identifier
            repo_clone_url: Git repository URL to clone
            branch_name: Branch to checkout
            issue_number: Source issue number
            pr_number: PR number for comments
            initial_prompt: Initial prompt from issue body
            github_token: GitHub installation token for cloning
            installation_id: GitHub App installation ID for token refresh
            repo_full_name: Repository full name (owner/repo)

        Returns:
            Task ARN
        """
        # Build environment overrides
        env_overrides = [
            {"name": "SESSION_ID", "value": session_id},
            {"name": "REPO_CLONE_URL", "value": repo_clone_url},
            {"name": "BRANCH_NAME", "value": branch_name},
            {"name": "ISSUE_NUMBER", "value": str(issue_number)},
            {"name": "PR_NUMBER", "value": str(pr_number)},
            {"name": "INITIAL_PROMPT", "value": initial_prompt},
            {"name": "UAT_DOMAIN_SUFFIX", "value": self.uat_domain_suffix},
            {"name": "GITHUB_TOKEN", "value": github_token},
            {"name": "GITHUB_INSTALLATION_ID", "value": str(installation_id)},
            {"name": "REPO_FULL_NAME", "value": repo_full_name}
        ]

        logger.info(f"Launching task for session {session_id}")
        logger.info(f"  Cluster: {self.cluster}")
        logger.info(f"  Task Definition: {self.task_definition}")
        logger.info(f"  Subnets: {self.subnets}")

        response = self.ecs.run_task(
            cluster=self.cluster,
            taskDefinition=self.task_definition,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": self.subnets,
                    "securityGroups": [self.security_group],
                    "assignPublicIp": "ENABLED"
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": "agent",
                        "environment": env_overrides
                    }
                ]
            },
            tags=[
                {"key": "SessionId", "value": session_id},
                {"key": "IssueNumber", "value": str(issue_number)},
                {"key": "PRNumber", "value": str(pr_number)}
            ]
        )

        tasks = response.get("tasks", [])
        if not tasks:
            failures = response.get("failures", [])
            logger.error(f"Failed to launch task: {failures}")
            raise RuntimeError(f"Failed to launch ECS task: {failures}")

        task_arn = tasks[0]["taskArn"]
        logger.info(f"Launched task {task_arn}")

        return task_arn

    def stop_task(self, task_arn: str, reason: str = "Session ended") -> None:
        """
        Stop an ECS task.

        Args:
            task_arn: Task ARN to stop
            reason: Reason for stopping
        """
        logger.info(f"Stopping task {task_arn}: {reason}")

        self.ecs.stop_task(
            cluster=self.cluster,
            task=task_arn,
            reason=reason
        )

    def get_task_status(self, task_arn: str) -> Optional[dict]:
        """
        Get the status of an ECS task.

        Args:
            task_arn: Task ARN

        Returns:
            Task description or None if not found
        """
        response = self.ecs.describe_tasks(
            cluster=self.cluster,
            tasks=[task_arn]
        )

        tasks = response.get("tasks", [])
        if not tasks:
            return None

        return tasks[0]

    def get_task_ip(self, task_arn: str) -> Optional[str]:
        """
        Get the public IP of a running task.

        Args:
            task_arn: Task ARN

        Returns:
            Public IP address or None
        """
        task = self.get_task_status(task_arn)
        if not task:
            return None

        # Get ENI attachment
        attachments = task.get("attachments", [])
        for attachment in attachments:
            if attachment.get("type") == "ElasticNetworkInterface":
                details = {d["name"]: d["value"] for d in attachment.get("details", [])}
                return details.get("privateIPv4Address")  # Note: This returns private IP

        return None

    def _register_task_definition_with_image(self, base_task_definition: str, image_uri: str) -> str:
        """
        Register a new task definition revision with a custom image.

        ECS doesn't support overriding the container image at runtime via containerOverrides.
        Instead, we must register a new task definition revision with the desired image.

        Args:
            base_task_definition: The task definition family or ARN to base the new revision on
            image_uri: The full image URI including tag (e.g., repo:staging)

        Returns:
            The new task definition ARN
        """
        # Get the current task definition
        response = self.ecs.describe_task_definition(taskDefinition=base_task_definition)
        task_def = response["taskDefinition"]

        # Clone container definitions and update the image
        container_defs = task_def["containerDefinitions"]
        for container in container_defs:
            container["image"] = image_uri

        # Register new revision (only include fields that register_task_definition accepts)
        register_params = {
            "family": task_def["family"],
            "containerDefinitions": container_defs,
            "taskRoleArn": task_def.get("taskRoleArn"),
            "executionRoleArn": task_def.get("executionRoleArn"),
            "networkMode": task_def.get("networkMode"),
            "requiresCompatibilities": task_def.get("requiresCompatibilities", []),
            "cpu": task_def.get("cpu"),
            "memory": task_def.get("memory"),
        }

        # Remove None values
        register_params = {k: v for k, v in register_params.items() if v is not None}

        logger.info(f"Registering new task definition revision with image: {image_uri}")
        response = self.ecs.register_task_definition(**register_params)

        new_task_def_arn = response["taskDefinition"]["taskDefinitionArn"]
        logger.info(f"Registered new task definition: {new_task_def_arn}")

        return new_task_def_arn

    def launch_test_tickets_task(
        self,
        session_id: str,
        branch: str,
        pr_number: int,
        repo: str,
        github_token: str = "",
        image_tag: str = "latest"
    ) -> str:
        """
        Launch an ECS task for test_tickets UAT environment.

        This launches a container with PostgreSQL running in-container
        that clones data from staging and runs the test_tickets app.

        Args:
            session_id: Unique session identifier (used as subdomain)
            branch: Git branch to deploy
            pr_number: PR/issue number for reference
            repo: Repository full name (owner/repo)
            github_token: GitHub token for cloning the repository
            image_tag: Docker image tag to use (default: "latest", use "staging" for testing)

        Returns:
            Task ARN

        Raises:
            RuntimeError: If task launch fails
            ValueError: If test_tickets task definition not configured
        """
        if not self.test_tickets_task_definition:
            raise ValueError(
                "TEST_TICKETS_TASK_DEFINITION not configured. "
                "Deploy the SAM template with TestTicketsImageUri parameter."
            )

        # Build environment overrides for the container
        env_overrides = [
            {"name": "SESSION_ID", "value": session_id},
            {"name": "BRANCH", "value": branch},
            {"name": "PR_NUMBER", "value": str(pr_number)},
            {"name": "REPO", "value": repo},
            {"name": "SESSIONS_TABLE", "value": self.sessions_table or ""},
            {"name": "GITHUB_TOKEN", "value": github_token},
            {"name": "UAT_DOMAIN_SUFFIX", "value": self.uat_domain_suffix},
        ]

        # Construct the full image URI with tag
        image_uri = f"{self.test_tickets_image_uri}:{image_tag}"

        # Determine which task definition to use
        # If using a non-default image tag, register a new task definition revision
        if image_tag != "latest":
            task_definition_arn = self._register_task_definition_with_image(
                self.test_tickets_task_definition,
                image_uri
            )
        else:
            task_definition_arn = self.test_tickets_task_definition

        logger.info(f"Launching test_tickets task for session {session_id}")
        logger.info(f"  Cluster: {self.cluster}")
        logger.info(f"  Task Definition: {task_definition_arn}")
        logger.info(f"  Image: {image_uri}")
        logger.info(f"  Branch: {branch}")

        response = self.ecs.run_task(
            cluster=self.cluster,
            taskDefinition=task_definition_arn,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": self.subnets,
                    "securityGroups": [self.security_group],
                    "assignPublicIp": "ENABLED"
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": "test-tickets",
                        "environment": env_overrides
                    }
                ]
            },
            tags=[
                {"key": "SessionId", "value": session_id},
                {"key": "AppType", "value": "test-tickets"},
                {"key": "Branch", "value": branch},
                {"key": "PRNumber", "value": str(pr_number)},
                {"key": "ImageTag", "value": image_tag}
            ]
        )

        tasks = response.get("tasks", [])
        if not tasks:
            failures = response.get("failures", [])
            logger.error(f"Failed to launch test_tickets task: {failures}")
            raise RuntimeError(f"Failed to launch test_tickets ECS task: {failures}")

        task_arn = tasks[0]["taskArn"]
        logger.info(f"Launched test_tickets task {task_arn}")

        return task_arn
