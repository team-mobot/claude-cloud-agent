"""
Claude Code CLI wrapper.

Runs Claude Code as a subprocess and captures results.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


class ClaudeRunner:
    """
    Wrapper for Claude Code CLI.

    Runs prompts through the CLI and extracts results.
    """

    def __init__(self, workspace: str):
        """
        Initialize Claude runner.

        Args:
            workspace: Path to the git repository workspace
        """
        self.workspace = workspace
        self.conversation_id = None

    async def run_prompt(self, prompt: str) -> dict[str, Any]:
        """
        Run a prompt through Claude Code.

        Args:
            prompt: The prompt to process

        Returns:
            Result dict with:
            - success: bool
            - summary: str (brief description of what was done)
            - commits: list[str] (commit SHAs if any)
            - error: str (if not successful)
        """
        logger.info(f"Running Claude Code with prompt: {prompt[:100]}...")

        # Build command
        cmd = ["claude", "--dangerously-skip-permissions"]

        # Continue conversation if we have one
        if self.conversation_id:
            cmd.extend(["--continue", self.conversation_id])

        # Add the prompt
        cmd.extend(["--print", prompt])

        try:
            # Run Claude Code
            result = await asyncio.to_thread(
                self._run_subprocess,
                cmd
            )

            if result["returncode"] != 0:
                logger.error(f"Claude Code failed: {result['stderr']}")
                return {
                    "success": False,
                    "error": result["stderr"] or "Claude Code exited with error"
                }

            # Parse output
            output = result["stdout"]
            logger.info(f"Claude Code output: {output[:500]}...")

            # Extract conversation ID for continuation
            self._extract_conversation_id(output)

            # Check for commits
            commits = await self._get_recent_commits()

            # Generate summary
            summary = self._extract_summary(output)

            return {
                "success": True,
                "summary": summary,
                "commits": commits,
                "raw_output": output
            }

        except Exception as e:
            logger.exception(f"Error running Claude Code: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    def _run_subprocess(self, cmd: list[str]) -> dict:
        """
        Run a subprocess synchronously.

        Args:
            cmd: Command and arguments

        Returns:
            Dict with stdout, stderr, returncode
        """
        logger.info(f"Running: {' '.join(cmd)}")

        env = os.environ.copy()
        env["CLAUDE_CODE_USE_BEDROCK"] = "1"

        result = subprocess.run(
            cmd,
            cwd=self.workspace,
            capture_output=True,
            text=True,
            env=env,
            timeout=600  # 10 minute timeout
        )

        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }

    def _extract_conversation_id(self, output: str) -> None:
        """Extract and store conversation ID from output."""
        # Claude Code outputs conversation ID in various formats
        # Look for patterns like "conversation: abc123" or similar
        patterns = [
            r'conversation[:\s]+([a-f0-9-]+)',
            r'session[:\s]+([a-f0-9-]+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, output, re.IGNORECASE)
            if match:
                self.conversation_id = match.group(1)
                logger.info(f"Captured conversation ID: {self.conversation_id}")
                break

    def _extract_summary(self, output: str) -> str:
        """
        Extract a brief summary from Claude's output.

        Args:
            output: Raw Claude Code output

        Returns:
            Brief summary string
        """
        # Take the first meaningful paragraph
        lines = output.strip().split("\n")
        summary_lines = []

        for line in lines:
            line = line.strip()
            if not line:
                if summary_lines:
                    break
                continue
            # Skip tool output markers
            if line.startswith("```") or line.startswith("---"):
                continue
            summary_lines.append(line)
            if len(summary_lines) >= 3:
                break

        if summary_lines:
            return " ".join(summary_lines)[:500]

        return "Task completed"

    async def _get_recent_commits(self) -> list[str]:
        """
        Get recent commits made during this session.

        Returns:
            List of recent commit messages (last 5)
        """
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "log", "--oneline", "-5", "--format=%h %s"],
                cwd=self.workspace,
                capture_output=True,
                text=True
            )

            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split("\n")

        except Exception as e:
            logger.warning(f"Failed to get commits: {e}")

        return []

    async def push_changes(self) -> bool:
        """
        Push committed changes to remote.

        Returns:
            True if push succeeded
        """
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "push", "origin", "HEAD"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode != 0:
                logger.error(f"Git push failed: {result.stderr}")
                return False

            logger.info("Changes pushed successfully")
            return True

        except Exception as e:
            logger.exception(f"Error pushing changes: {e}")
            return False
