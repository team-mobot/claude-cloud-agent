"""
Claude Code CLI wrapper with streaming support.

Runs Claude Code as a subprocess and streams results to GitHub.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class ClaudeRunner:
    """
    Wrapper for Claude Code CLI with streaming output.

    Runs prompts through the CLI and streams tool uses/results
    via callback functions.
    """

    def __init__(self, workspace: str):
        """
        Initialize Claude runner.

        Args:
            workspace: Path to the git repository workspace
        """
        self.workspace = workspace
        self.conversation_id = None

    async def run_prompt(
        self,
        prompt: str,
        on_tool_use: Optional[Callable[[str, dict], Any]] = None,
        on_tool_result: Optional[Callable[[str, bool], Any]] = None,
        on_text: Optional[Callable[[str], Any]] = None,
    ) -> dict[str, Any]:
        """
        Run a prompt through Claude Code with streaming output.

        Args:
            prompt: The prompt to process
            on_tool_use: Callback for tool uses (tool_name, tool_input)
            on_tool_result: Callback for tool results (result, is_error)
            on_text: Callback for Claude's text responses

        Returns:
            Result dict with:
            - success: bool
            - summary: str (brief description of what was done)
            - commits: list[str] (commit SHAs if any)
            - error: str (if not successful)
        """
        logger.info(f"Running Claude Code with prompt: {prompt[:100]}...")

        # Build command with streaming JSON output
        cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
        ]

        # Continue conversation if we have one
        if self.conversation_id:
            cmd.extend(["--continue", self.conversation_id])

        # Add the prompt
        cmd.extend(["-p", prompt])

        try:
            # Run Claude Code with streaming
            result = await self._run_streaming(
                cmd,
                on_tool_use=on_tool_use,
                on_tool_result=on_tool_result,
                on_text=on_text,
            )

            if result["returncode"] != 0:
                logger.error(f"Claude Code failed with code {result['returncode']}")
                return {
                    "success": False,
                    "error": result.get("stderr") or "Claude Code exited with error"
                }

            # Check for commits
            commits = await self._get_recent_commits()

            # Push changes if any commits were made
            if commits:
                await self.push_changes()

            return {
                "success": True,
                "summary": result.get("summary", "Task completed"),
                "commits": commits,
            }

        except Exception as e:
            logger.exception(f"Error running Claude Code: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def _run_streaming(
        self,
        cmd: list[str],
        on_tool_use: Optional[Callable] = None,
        on_tool_result: Optional[Callable] = None,
        on_text: Optional[Callable] = None,
    ) -> dict:
        """
        Run command with streaming output parsing.

        Parses JSON stream and calls callbacks for each event.
        """
        logger.info(f"Running: {' '.join(cmd)}")

        env = os.environ.copy()
        env["CLAUDE_CODE_USE_BEDROCK"] = "1"

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workspace,
            env=env,
        )

        summary_parts = []
        pending_tool_uses = {}  # Track tool_use_id -> (name, input)
        buffer = b""  # Buffer for incomplete lines

        try:
            # Read stdout in chunks to handle large JSON lines
            # (readline() has a 64KB limit which Claude Code can exceed)
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        process.stdout.read(1024 * 1024),  # Read up to 1MB at a time
                        timeout=600  # 10 minute timeout
                    )
                except asyncio.TimeoutError:
                    logger.error("Claude Code timed out")
                    process.kill()
                    return {"returncode": -1, "stderr": "Timeout"}

                if not chunk:
                    # Process any remaining data in buffer
                    if buffer:
                        line_str = buffer.decode("utf-8").strip()
                        if line_str:
                            try:
                                data = json.loads(line_str)
                                await self._handle_stream_event(
                                    data, pending_tool_uses, summary_parts,
                                    on_tool_use, on_tool_result, on_text,
                                )
                            except json.JSONDecodeError:
                                logger.debug(f"Non-JSON output: {line_str[:200]}")
                    break

                buffer += chunk

                # Process complete lines from buffer
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line_str = line.decode("utf-8").strip()
                    if not line_str:
                        continue

                    # Try to parse as JSON
                    try:
                        data = json.loads(line_str)
                        await self._handle_stream_event(
                            data,
                            pending_tool_uses,
                            summary_parts,
                            on_tool_use,
                            on_tool_result,
                            on_text,
                        )
                    except json.JSONDecodeError:
                        # Not JSON, log it
                        logger.debug(f"Non-JSON output: {line_str[:200]}")

        except Exception as e:
            logger.error(f"Error reading Claude Code output: {e}")
            process.kill()
            return {"returncode": -1, "stderr": str(e)}

        # Wait for process to complete
        await process.wait()

        # Read any remaining stderr
        stderr = await process.stderr.read()

        return {
            "returncode": process.returncode,
            "stderr": stderr.decode("utf-8") if stderr else "",
            "summary": " ".join(summary_parts) if summary_parts else "Task completed",
        }

    async def _handle_stream_event(
        self,
        data: dict,
        pending_tool_uses: dict,
        summary_parts: list,
        on_tool_use: Optional[Callable],
        on_tool_result: Optional[Callable],
        on_text: Optional[Callable],
    ) -> None:
        """Handle a single stream event."""
        msg_type = data.get("type")

        if msg_type == "assistant":
            message = data.get("message", {})
            content = message.get("content", [])

            for item in content:
                item_type = item.get("type")

                if item_type == "text":
                    text = item.get("text", "").strip()
                    if text:
                        # Collect for summary
                        if len(summary_parts) < 3:
                            summary_parts.append(text[:200])

                        # Callback
                        if on_text:
                            try:
                                result = on_text(text)
                                if asyncio.iscoroutine(result):
                                    await result
                            except Exception as e:
                                logger.warning(f"on_text callback error: {e}")

                elif item_type == "tool_use":
                    tool_name = item.get("name", "unknown")
                    tool_input = item.get("input", {})
                    tool_id = item.get("id", "")

                    # Track for matching with result
                    pending_tool_uses[tool_id] = (tool_name, tool_input)

                    logger.info(f"Tool use: {tool_name}")

                    # Callback
                    if on_tool_use:
                        try:
                            result = on_tool_use(tool_name, tool_input)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as e:
                            logger.warning(f"on_tool_use callback error: {e}")

        elif msg_type == "user":
            # Tool results come in user messages
            message = data.get("message", {})
            content = message.get("content", [])

            for item in content:
                if item.get("type") == "tool_result":
                    tool_id = item.get("tool_use_id", "")
                    result_content = item.get("content", "")
                    is_error = item.get("is_error", False)

                    # Extract result text
                    if isinstance(result_content, list):
                        # Handle structured content
                        result_text = ""
                        for part in result_content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                result_text += part.get("text", "")
                    else:
                        result_text = str(result_content)

                    logger.info(f"Tool result: {'error' if is_error else 'success'}")

                    # Callback
                    if on_tool_result:
                        try:
                            result = on_tool_result(result_text, is_error)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as e:
                            logger.warning(f"on_tool_result callback error: {e}")

        elif msg_type == "result":
            # Final result message
            session_id = data.get("session_id")
            if session_id:
                self.conversation_id = session_id
                logger.info(f"Session ID: {session_id}")

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
