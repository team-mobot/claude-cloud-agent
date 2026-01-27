"""
GitHub API client with App authentication.

Uses JWT for App authentication, then exchanges for installation token.
"""

import logging
import time
from typing import Optional

import jwt
import requests

logger = logging.getLogger(__name__)


class GitHubClient:
    """
    GitHub API client using App authentication.

    Authenticates as a GitHub App installation to perform actions
    on behalf of the app.
    """

    BASE_URL = "https://api.github.com"

    def __init__(self, app_id: str, private_key: str, installation_id: int):
        """
        Initialize GitHub client.

        Args:
            app_id: GitHub App ID
            private_key: GitHub App private key (PEM format)
            installation_id: Installation ID for the target org/repo
        """
        self.app_id = app_id
        self.private_key = private_key
        self.installation_id = installation_id
        self._installation_token: Optional[str] = None
        self._token_expires_at: float = 0

    def _generate_jwt(self) -> str:
        """
        Generate a JWT for GitHub App authentication.

        JWT is valid for 10 minutes.
        """
        now = int(time.time())
        payload = {
            "iat": now - 60,  # Allow for clock skew
            "exp": now + 600,  # 10 minutes
            "iss": self.app_id
        }
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    def _get_installation_token(self) -> str:
        """
        Get or refresh the installation access token.

        Caches token until near expiration.
        """
        # Check if we have a valid cached token
        if self._installation_token and time.time() < self._token_expires_at - 60:
            return self._installation_token

        # Generate new JWT and exchange for installation token
        jwt_token = self._generate_jwt()

        url = f"{self.BASE_URL}/app/installations/{self.installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        response = requests.post(url, headers=headers)
        response.raise_for_status()

        data = response.json()
        self._installation_token = data["token"]

        # Parse expiration (GitHub returns ISO 8601 format)
        from datetime import datetime
        expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        self._token_expires_at = expires_at.timestamp()

        logger.info(f"Obtained new installation token, expires at {data['expires_at']}")
        return self._installation_token

    def _headers(self) -> dict:
        """Get headers for API requests."""
        token = self._get_installation_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }

    def get_token(self) -> str:
        """Get the current installation access token for use by other components."""
        return self._get_installation_token()

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Make an authenticated API request."""
        url = f"{self.BASE_URL}{endpoint}"
        response = requests.request(method, url, headers=self._headers(), **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def get_ref(self, repo: str, ref: str) -> dict:
        """
        Get a git reference.

        Args:
            repo: Repository full name (owner/repo)
            ref: Reference name (e.g., heads/main)
        """
        return self._request("GET", f"/repos/{repo}/git/ref/{ref}")

    def create_ref(self, repo: str, ref: str, sha: str) -> dict:
        """
        Create a git reference (branch).

        Args:
            repo: Repository full name
            ref: Reference name (e.g., refs/heads/feature)
            sha: Commit SHA to point to
        """
        return self._request(
            "POST",
            f"/repos/{repo}/git/refs",
            json={"ref": ref, "sha": sha}
        )

    def create_branch(self, repo: str, branch_name: str, from_branch: str) -> dict:
        """
        Create a new branch from an existing branch.

        Args:
            repo: Repository full name
            branch_name: New branch name
            from_branch: Source branch name
        """
        # Get SHA of source branch
        ref = self.get_ref(repo, f"heads/{from_branch}")
        sha = ref["object"]["sha"]

        # Create new branch
        return self.create_ref(repo, f"refs/heads/{branch_name}", sha)

    def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False
    ) -> dict:
        """
        Create a pull request.

        Args:
            repo: Repository full name
            title: PR title
            body: PR body
            head: Head branch name
            base: Base branch name
            draft: Whether to create as draft
        """
        return self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
                "draft": draft
            }
        )

    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> dict:
        """
        Create a comment on an issue or PR.

        Args:
            repo: Repository full name
            issue_number: Issue or PR number
            body: Comment body
        """
        return self._request(
            "POST",
            f"/repos/{repo}/issues/{issue_number}/comments",
            json={"body": body}
        )

    def update_pull_request(
        self,
        repo: str,
        pr_number: int,
        **kwargs
    ) -> dict:
        """
        Update a pull request.

        Args:
            repo: Repository full name
            pr_number: PR number
            **kwargs: Fields to update (title, body, state, base)
        """
        return self._request(
            "PATCH",
            f"/repos/{repo}/pulls/{pr_number}",
            json=kwargs
        )

    def get_pull_request(self, repo: str, pr_number: int) -> dict:
        """
        Get a pull request.

        Args:
            repo: Repository full name
            pr_number: PR number
        """
        return self._request("GET", f"/repos/{repo}/pulls/{pr_number}")

    def create_or_update_file(
        self,
        repo: str,
        path: str,
        message: str,
        content: str,
        branch: str,
        sha: Optional[str] = None
    ) -> dict:
        """
        Create or update a file in a repository.

        Args:
            repo: Repository full name
            path: File path in the repository
            message: Commit message
            content: File content (will be base64 encoded)
            branch: Branch to commit to
            sha: SHA of file being replaced (required for updates)
        """
        import base64
        encoded_content = base64.b64encode(content.encode()).decode()

        data = {
            "message": message,
            "content": encoded_content,
            "branch": branch
        }
        if sha:
            data["sha"] = sha

        return self._request(
            "PUT",
            f"/repos/{repo}/contents/{path}",
            json=data
        )
