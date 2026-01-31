"""
JIRA API client for posting comments.

Uses Basic Auth with email + API token.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class JiraClient:
    """
    JIRA Cloud API client for posting comments to issues.
    """

    def __init__(self, base_url: str, email: str, api_token: str):
        """
        Initialize JIRA client.

        Args:
            base_url: JIRA instance URL (e.g., https://company.atlassian.net)
            email: Service account email
            api_token: API token for authentication
        """
        self.base_url = base_url.rstrip("/")
        self.auth = (email, api_token)

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Make an authenticated API request."""
        url = f"{self.base_url}/rest/api/3{endpoint}"
        headers = kwargs.pop("headers", {})
        headers["Content-Type"] = "application/json"

        response = requests.request(
            method, url,
            auth=self.auth,
            headers=headers,
            **kwargs
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def add_comment(self, issue_key: str, body: str) -> dict:
        """
        Add a comment to a JIRA issue.

        Args:
            issue_key: JIRA issue key (e.g., AGNTS-119)
            body: Comment body in plain text (will be converted to ADF)

        Returns:
            Created comment data
        """
        # Convert plain text to Atlassian Document Format (ADF)
        adf_body = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": line}
                    ]
                }
                for line in body.split("\n") if line.strip()
            ]
        }

        logger.info(f"Posting comment to JIRA issue {issue_key}")
        return self._request(
            "POST",
            f"/issue/{issue_key}/comment",
            json={"body": adf_body}
        )

    def add_formatted_comment(
        self,
        issue_key: str,
        title: str,
        fields: dict,
        footer: Optional[str] = None
    ) -> dict:
        """
        Add a formatted comment with title and key-value fields.

        Args:
            issue_key: JIRA issue key
            title: Comment title/header
            fields: Dictionary of field names to values
            footer: Optional footer text

        Returns:
            Created comment data
        """
        content = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": title, "marks": [{"type": "strong"}]}
                ]
            }
        ]

        # Add fields as bullet list
        if fields:
            list_items = []
            for key, value in fields.items():
                list_items.append({
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": f"{key}: ", "marks": [{"type": "strong"}]},
                                {"type": "text", "text": str(value)}
                            ]
                        }
                    ]
                })
            content.append({
                "type": "bulletList",
                "content": list_items
            })

        if footer:
            content.append({
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": footer, "marks": [{"type": "em"}]}
                ]
            })

        adf_body = {
            "type": "doc",
            "version": 1,
            "content": content
        }

        logger.info(f"Posting formatted comment to JIRA issue {issue_key}")
        return self._request(
            "POST",
            f"/issue/{issue_key}/comment",
            json={"body": adf_body}
        )
