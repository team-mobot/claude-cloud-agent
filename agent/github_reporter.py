"""
GitHub reporter for posting PR comments.

Posts progress updates and results to the associated PR.
"""

import logging
import os
import time
from typing import Optional

import jwt
import requests

logger = logging.getLogger(__name__)


class GitHubReporter:
    """
    Posts comments to GitHub PRs.

    Uses GitHub App authentication.
    """

    BASE_URL = "https://api.github.com"

    def __init__(self):
        """Initialize GitHub reporter from environment."""
        self.app_id = os.environ.get("GITHUB_APP_ID")
        self.private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
        self.repo_clone_url = os.environ.get("REPO_CLONE_URL", "")
        self.pr_number = int(os.environ.get("PR_NUMBER", "0"))

        # Extract repo full name from clone URL
        # https://github.com/owner/repo.git -> owner/repo
        self.repo_full_name = self._extract_repo_name(self.repo_clone_url)

        self._installation_id: Optional[int] = None
        self._installation_token: Optional[str] = None
        self._token_expires_at: float = 0

    def _extract_repo_name(self, clone_url: str) -> str:
        """Extract owner/repo from clone URL."""
        if not clone_url:
            return ""

        # Remove .git suffix
        url = clone_url.rstrip("/")
        if url.endswith(".git"):
            url = url[:-4]

        # Extract owner/repo
        parts = url.split("/")
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1]}"

        return ""

    def _generate_jwt(self) -> str:
        """Generate JWT for GitHub App authentication."""
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 600,
            "iss": self.app_id
        }
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    async def _get_installation_id(self) -> int:
        """Get installation ID for the repository."""
        if self._installation_id:
            return self._installation_id

        jwt_token = self._generate_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        # Get repository installation
        url = f"{self.BASE_URL}/repos/{self.repo_full_name}/installation"
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        self._installation_id = response.json()["id"]
        logger.info(f"Got installation ID: {self._installation_id}")
        return self._installation_id

    async def _get_token(self) -> str:
        """Get or refresh installation access token."""
        if self._installation_token and time.time() < self._token_expires_at - 60:
            return self._installation_token

        installation_id = await self._get_installation_id()
        jwt_token = self._generate_jwt()

        url = f"{self.BASE_URL}/app/installations/{installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        response = requests.post(url, headers=headers)
        response.raise_for_status()

        data = response.json()
        self._installation_token = data["token"]

        from datetime import datetime
        expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        self._token_expires_at = expires_at.timestamp()

        logger.info(f"Got new installation token, expires at {data['expires_at']}")
        return self._installation_token

    async def post_comment(self, body: str) -> Optional[dict]:
        """
        Post a comment to the PR.

        Args:
            body: Comment body (markdown)

        Returns:
            Created comment data or None on failure
        """
        if not self.repo_full_name or not self.pr_number:
            logger.warning("Missing repo or PR number, cannot post comment")
            return None

        try:
            token = await self._get_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"
            }

            url = f"{self.BASE_URL}/repos/{self.repo_full_name}/issues/{self.pr_number}/comments"

            response = requests.post(url, headers=headers, json={"body": body})
            response.raise_for_status()

            logger.info(f"Posted comment to PR #{self.pr_number}")
            return response.json()

        except Exception as e:
            logger.exception(f"Failed to post comment: {e}")
            return None

    async def update_pr_body(self, body: str) -> Optional[dict]:
        """
        Update the PR body.

        Args:
            body: New PR body

        Returns:
            Updated PR data or None on failure
        """
        try:
            token = await self._get_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"
            }

            url = f"{self.BASE_URL}/repos/{self.repo_full_name}/pulls/{self.pr_number}"

            response = requests.patch(url, headers=headers, json={"body": body})
            response.raise_for_status()

            logger.info(f"Updated PR #{self.pr_number} body")
            return response.json()

        except Exception as e:
            logger.exception(f"Failed to update PR: {e}")
            return None

    def get_auth_token_sync(self) -> Optional[str]:
        """
        Get authentication token synchronously (for git operations).

        Returns:
            Installation token or None
        """
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Create a new loop in a thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, self._get_token())
                    return future.result()
            else:
                return loop.run_until_complete(self._get_token())
        except Exception as e:
            logger.exception(f"Failed to get token: {e}")
            return None
