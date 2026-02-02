"""
GitHub reporter for posting PR comments.

Posts progress updates and results to the associated PR.
Supports streaming updates for tool use and results.
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

import jwt
import requests

logger = logging.getLogger(__name__)


def format_tool_use(tool_name: str, tool_input: dict[str, Any]) -> str:
    """
    Format a tool use for display in PR comments.

    Args:
        tool_name: Name of the tool (Read, Write, Edit, Bash, etc.)
        tool_input: Tool input parameters

    Returns:
        Formatted markdown string
    """
    if tool_name == "Read":
        path = tool_input.get("file_path", "unknown")
        return f"📖 **Reading:** `{path}`"

    elif tool_name == "Write":
        path = tool_input.get("file_path", "unknown")
        content = tool_input.get("content", "")
        preview = content[:300] + "..." if len(content) > 300 else content
        return f"✏️ **Writing:** `{path}`\n```\n{preview}\n```"

    elif tool_name == "Edit":
        path = tool_input.get("file_path", "unknown")
        old = tool_input.get("old_string", "")[:150]
        new = tool_input.get("new_string", "")[:150]
        return f"✏️ **Editing:** `{path}`\n\n```diff\n- {old}\n+ {new}\n```"

    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")
        desc = tool_input.get("description", "")
        if desc:
            return f"💻 **Running:** `{cmd}`\n_{desc}_"
        return f"💻 **Running:** `{cmd}`"

    elif tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        return f"🔍 **Finding files:** `{pattern}`"

    elif tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        return f"🔎 **Searching:** `{pattern}`"

    elif tool_name == "Task":
        desc = tool_input.get("description", "subtask")
        return f"🤖 **Spawning agent:** {desc}"

    elif tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        if questions:
            q = questions[0].get("question", "")
            return f"❓ **Question:** {q}"
        return "❓ **Asking a question**"

    else:
        # Generic tool display
        input_preview = json.dumps(tool_input, indent=2)[:300]
        return f"🔧 **{tool_name}**\n```json\n{input_preview}\n```"


def format_tool_result(result: str, is_error: bool = False) -> str:
    """
    Format a tool result for display.

    Args:
        result: Tool result content
        is_error: Whether this is an error result

    Returns:
        Formatted markdown string
    """
    # Truncate long results
    if len(result) > 1000:
        result = result[:1000] + "\n...(truncated)"

    if is_error:
        return f"❌ **Error:**\n```\n{result}\n```"
    else:
        return f"✅ **Result:**\n```\n{result}\n```"


def format_text_response(text: str) -> str:
    """
    Format Claude's text response for display.

    Args:
        text: Claude's text response

    Returns:
        Formatted markdown string
    """
    # Truncate very long responses
    if len(text) > 2000:
        text = text[:2000] + "\n...(truncated)"

    return f"💬 **Claude:**\n\n{text}"


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


class StreamingReporter:
    """
    Batches and posts streaming updates to GitHub.

    Collects tool uses and results, posting them in batched comments
    to avoid rate limiting.
    """

    def __init__(self, github: GitHubReporter, batch_interval: float = 3.0):
        """
        Initialize streaming reporter.

        Args:
            github: GitHubReporter instance for posting
            batch_interval: Seconds between batched posts
        """
        self.github = github
        self.batch_interval = batch_interval
        self._pending_items: list[str] = []
        self._last_post_time: float = 0
        self._lock = asyncio.Lock()
        self._post_task: Optional[asyncio.Task] = None

    async def add_tool_use(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        """Add a tool use to the pending batch."""
        formatted = format_tool_use(tool_name, tool_input)
        async with self._lock:
            self._pending_items.append(formatted)
        await self._maybe_post()

    async def add_tool_result(self, result: str, is_error: bool = False) -> None:
        """Add a tool result to the pending batch."""
        formatted = format_tool_result(result, is_error)
        async with self._lock:
            self._pending_items.append(formatted)
        await self._maybe_post()

    async def add_text(self, text: str) -> None:
        """Add Claude's text response to the pending batch."""
        formatted = format_text_response(text)
        async with self._lock:
            self._pending_items.append(formatted)
        await self._maybe_post()

    async def _maybe_post(self) -> None:
        """Post if enough time has passed since last post."""
        now = time.time()

        async with self._lock:
            if not self._pending_items:
                return

            # Always post if we have many items
            if len(self._pending_items) >= 5:
                await self._post_batch()
                return

            # Post if enough time has passed
            if now - self._last_post_time >= self.batch_interval:
                await self._post_batch()

    async def _post_batch(self) -> None:
        """Post all pending items as a single comment."""
        if not self._pending_items:
            return

        # Combine all items
        body = "<!-- claude-agent-stream -->\n" + "\n\n---\n\n".join(self._pending_items)

        # Clear pending
        self._pending_items = []
        self._last_post_time = time.time()

        # Post comment
        await self.github.post_comment(body)

    async def flush(self) -> None:
        """Force post any remaining items."""
        async with self._lock:
            if self._pending_items:
                await self._post_batch()
