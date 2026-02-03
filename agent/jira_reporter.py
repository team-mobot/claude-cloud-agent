"""
JIRA reporter for posting completion summaries.

Posts summary comments back to JIRA issues when agent work completes.
Fetches credentials from AWS Secrets Manager.
"""

import json
import logging
import os
from typing import Optional

import boto3
import requests

logger = logging.getLogger(__name__)


class JiraReporter:
    """
    Posts comments to JIRA issues.

    Fetches credentials from Secrets Manager and uses Basic Auth.
    """

    def __init__(self):
        """Initialize JIRA reporter from environment."""
        self.issue_key = os.environ.get("JIRA_ISSUE_KEY", "")
        self.site = os.environ.get("JIRA_SITE", "")
        self.secret_arn = os.environ.get("JIRA_SECRET_ARN", "")
        self.pr_number = os.environ.get("PR_NUMBER", "")
        self.repo_full_name = self._get_repo_full_name()

        self._credentials: Optional[dict] = None
        self._secrets_client = None

    def _get_repo_full_name(self) -> str:
        """Extract repo full name from environment."""
        # Try REPO_FULL_NAME first
        repo = os.environ.get("REPO_FULL_NAME", "")
        if repo:
            return repo

        # Fall back to extracting from REPO_CLONE_URL
        clone_url = os.environ.get("REPO_CLONE_URL", "")
        if clone_url:
            url = clone_url.rstrip("/")
            if url.endswith(".git"):
                url = url[:-4]
            parts = url.split("/")
            if len(parts) >= 2:
                return f"{parts[-2]}/{parts[-1]}"

        return ""

    @property
    def enabled(self) -> bool:
        """Check if JIRA reporting is enabled (has necessary config)."""
        return bool(self.issue_key and self.secret_arn)

    def _get_credentials(self) -> dict:
        """Fetch JIRA credentials from Secrets Manager."""
        if self._credentials:
            return self._credentials

        if not self.secret_arn:
            raise ValueError("JIRA_SECRET_ARN not configured")

        if self._secrets_client is None:
            self._secrets_client = boto3.client("secretsmanager")

        response = self._secrets_client.get_secret_value(SecretId=self.secret_arn)
        self._credentials = json.loads(response["SecretString"])

        logger.info("Fetched JIRA credentials from Secrets Manager")
        return self._credentials

    def _get_base_url(self) -> str:
        """Get JIRA base URL from credentials or site."""
        creds = self._get_credentials()
        base_url = creds.get("base_url", "")

        if not base_url and self.site:
            base_url = f"https://{self.site}"

        return base_url.rstrip("/")

    def _make_request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Make an authenticated API request to JIRA."""
        creds = self._get_credentials()
        base_url = self._get_base_url()

        url = f"{base_url}/rest/api/3{endpoint}"
        auth = (creds["email"], creds["api_token"])

        headers = kwargs.pop("headers", {})
        headers["Content-Type"] = "application/json"

        response = requests.request(
            method, url,
            auth=auth,
            headers=headers,
            **kwargs
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    async def post_completion_summary(
        self,
        success: bool,
        summary: str,
        commits: list[str],
        error: Optional[str] = None
    ) -> Optional[dict]:
        """
        Post a completion summary to JIRA.

        Args:
            success: Whether the implementation succeeded
            summary: Summary of changes made
            commits: List of commit messages
            error: Error message if failed

        Returns:
            Created comment data or None on failure
        """
        if not self.enabled:
            logger.info("JIRA reporting not enabled, skipping summary")
            return None

        try:
            pr_url = f"https://github.com/{self.repo_full_name}/pull/{self.pr_number}"

            if success:
                status_icon = "✅"
                status_text = "Implementation Complete"
            else:
                status_icon = "⚠️"
                status_text = "Implementation Failed"

            # Build ADF content
            content = [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": f"{status_icon} {status_text}", "marks": [{"type": "strong"}]}
                    ]
                },
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "GitHub PR: "},
                        {
                            "type": "text",
                            "text": f"#{self.pr_number}",
                            "marks": [
                                {
                                    "type": "link",
                                    "attrs": {"href": pr_url}
                                }
                            ]
                        }
                    ]
                }
            ]

            # Add summary
            if summary:
                content.append({
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Summary: ", "marks": [{"type": "strong"}]},
                        {"type": "text", "text": summary}
                    ]
                })

            # Add commits as bullet list
            if commits:
                commit_items = []
                for commit in commits[-5:]:  # Last 5 commits
                    commit_items.append({
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": commit, "marks": [{"type": "code"}]}
                                ]
                            }
                        ]
                    })

                content.append({
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Commits:", "marks": [{"type": "strong"}]}
                    ]
                })
                content.append({
                    "type": "bulletList",
                    "content": commit_items
                })

            # Add error if present
            if error:
                content.append({
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Error: ", "marks": [{"type": "strong"}]},
                        {"type": "text", "text": error}
                    ]
                })

            # Add footer
            content.append({
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "Review the PR and provide feedback there.",
                        "marks": [{"type": "em"}]
                    }
                ]
            })

            adf_body = {
                "type": "doc",
                "version": 1,
                "content": content
            }

            logger.info(f"Posting completion summary to JIRA issue {self.issue_key}")
            result = self._make_request(
                "POST",
                f"/issue/{self.issue_key}/comment",
                json={"body": adf_body}
            )

            logger.info(f"Posted completion summary to JIRA {self.issue_key}")
            return result

        except Exception as e:
            logger.exception(f"Failed to post JIRA completion summary: {e}")
            return None
