"""
Agent orchestrator - main entry point.

Coordinates:
1. Session initialization and IP reporting
2. API server for receiving prompts
3. Claude Code CLI execution
4. Dev server management
5. GitHub PR comment posting
6. Idle timeout handling
"""

import asyncio
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import uvicorn

from api_server import app, prompt_queue
from claude_runner import ClaudeRunner
from session_reporter import SessionReporter
from github_reporter import GitHubReporter, StreamingReporter
from jira_reporter import JiraReporter
from dev_server import DevServerManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Configuration from environment
SESSION_ID = os.environ.get("SESSION_ID", "")
REPO_CLONE_URL = os.environ.get("REPO_CLONE_URL", "")
BRANCH_NAME = os.environ.get("BRANCH_NAME", "")
ISSUE_NUMBER = int(os.environ.get("ISSUE_NUMBER", "0"))
PR_NUMBER = int(os.environ.get("PR_NUMBER", "0"))
INITIAL_PROMPT = os.environ.get("INITIAL_PROMPT", "")
UAT_DOMAIN_SUFFIX = os.environ.get("UAT_DOMAIN_SUFFIX", "uat.teammobot.dev")

# Idle timeout (60 minutes)
IDLE_TIMEOUT_SECONDS = 60 * 60
IDLE_WARNING_SECONDS = 55 * 60  # Warn 5 minutes before timeout

# Global state
last_activity_time = time.time()
shutdown_requested = False


def get_workspace_path() -> str:
    """Get path to the cloned repository."""
    repo_name = os.path.basename(REPO_CLONE_URL).replace(".git", "")
    return f"/workspace/{repo_name}"


async def run_api_server():
    """Run the FastAPI server on port 3000 (where ALB routes traffic)."""
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=3000,  # ALB routes here; we proxy dev server requests to 3001
        log_level="info"
    )
    server = uvicorn.Server(config)
    await server.serve()


async def process_prompts(
    claude: ClaudeRunner,
    github: GitHubReporter,
    session: SessionReporter
):
    """
    Process prompts from the queue.

    Runs Claude Code for each prompt and streams results to GitHub.
    """
    global last_activity_time

    while not shutdown_requested:
        try:
            # Wait for prompt with timeout
            prompt_data = await asyncio.wait_for(
                prompt_queue.get(),
                timeout=10.0
            )

            last_activity_time = time.time()
            logger.info(f"Processing prompt from {prompt_data.get('author', 'unknown')}")

            prompt = prompt_data.get("prompt", "")
            author = prompt_data.get("author", "unknown")

            # Post acknowledgment
            await github.post_comment(
                f"<!-- claude-agent -->\n:robot: Processing feedback from @{author}..."
            )

            # Create streaming reporter for this prompt
            streamer = StreamingReporter(github)

            # Run Claude Code with streaming callbacks
            result = await claude.run_prompt(
                prompt,
                on_tool_use=streamer.add_tool_use,
                on_tool_result=streamer.add_tool_result,
                on_text=streamer.add_text,
            )

            # Flush any remaining updates
            await streamer.flush()

            # Post final result
            if result["success"]:
                # Check if there were commits
                if result.get("commits"):
                    commit_info = "\n".join([f"- `{c}`" for c in result["commits"][-3:]])
                    await github.post_comment(
                        f"<!-- claude-agent -->\n:white_check_mark: **Changes committed**\n\n{commit_info}"
                    )
                else:
                    await github.post_comment(
                        f"<!-- claude-agent -->\n:white_check_mark: **Completed**"
                    )
            else:
                await github.post_comment(
                    f"<!-- claude-agent -->\n:warning: **Error**\n\n```\n{result.get('error', 'Unknown error')}\n```"
                )

            # Update session activity
            session.update_activity()

        except asyncio.TimeoutError:
            # No prompt received, check for idle timeout
            continue
        except Exception as e:
            logger.exception(f"Error processing prompt: {e}")


async def check_idle_timeout(github: GitHubReporter):
    """
    Monitor for idle timeout.

    Posts warning and shuts down if idle too long.
    """
    global shutdown_requested
    warning_posted = False

    while not shutdown_requested:
        await asyncio.sleep(60)  # Check every minute

        idle_time = time.time() - last_activity_time

        # Post warning
        if idle_time > IDLE_WARNING_SECONDS and not warning_posted:
            await github.post_comment(
                f"<!-- claude-agent -->\n:hourglass: **Idle Warning**\n\nNo activity for {int(idle_time / 60)} minutes. "
                f"Session will terminate in {int((IDLE_TIMEOUT_SECONDS - idle_time) / 60)} minutes.\n\n"
                f"Comment on this PR to keep the session alive."
            )
            warning_posted = True
            logger.warning(f"Posted idle warning, idle for {idle_time}s")

        # Shutdown on timeout
        if idle_time > IDLE_TIMEOUT_SECONDS:
            logger.warning(f"Idle timeout reached ({idle_time}s), shutting down")
            await github.post_comment(
                f"<!-- claude-agent -->\n:zzz: **Session Terminated**\n\n"
                f"No activity for {int(idle_time / 60)} minutes. Session has been terminated.\n\n"
                f"To restart, remove and re-add the `claude-dev` label on the original issue."
            )
            shutdown_requested = True
            break


async def main():
    """Main entry point."""
    global last_activity_time, shutdown_requested

    logger.info("=" * 60)
    logger.info("Claude Cloud Agent Starting")
    logger.info(f"Session ID: {SESSION_ID}")
    logger.info(f"PR Number: {PR_NUMBER}")
    logger.info("=" * 60)

    workspace = get_workspace_path()
    logger.info(f"Workspace: {workspace}")

    # Initialize components
    session = SessionReporter()
    github = GitHubReporter()
    jira = JiraReporter()
    claude = ClaudeRunner(workspace)
    dev_server = DevServerManager(workspace)

    # Report IP and mark session as running
    logger.info("Reporting container IP...")
    container_ip = session.discover_and_report_ip()
    uat_url = f"https://{SESSION_ID}.{UAT_DOMAIN_SUFFIX}"

    # Post UAT URL to PR
    await github.post_comment(
        f"<!-- claude-agent -->\n:rocket: **Agent Ready**\n\n"
        f"Session ID: `{SESSION_ID}`\n"
        f"UAT Preview: {uat_url}\n\n"
        f"I'm starting work on this issue. Comment on this PR to provide feedback."
    )

    # Start dev server (on port 3001, proxied through API server on 3000)
    logger.info("Starting dev server...")
    dev_server_started = await dev_server.start()
    if dev_server_started:
        logger.info("Dev server started on port 3001 (proxied via API server on 3000)")
    else:
        logger.warning("Could not auto-detect dev server")

    # Start API server immediately so PR comments can be received during initial prompt processing
    logger.info("Starting API server on port 3000...")
    api_server_task = asyncio.create_task(run_api_server())
    # Give it a moment to start
    await asyncio.sleep(0.5)
    logger.info("API server started, ready to receive prompts")

    # Process initial prompt
    if INITIAL_PROMPT:
        logger.info("Processing initial prompt...")
        last_activity_time = time.time()

        # Create streaming reporter for initial prompt
        streamer = StreamingReporter(github)

        # Run Claude Code with streaming callbacks
        result = await claude.run_prompt(
            INITIAL_PROMPT,
            on_tool_use=streamer.add_tool_use,
            on_tool_result=streamer.add_tool_result,
            on_text=streamer.add_text,
        )

        # Flush any remaining updates
        await streamer.flush()

        # Post final result
        if result["success"]:
            if result.get("commits"):
                commit_info = "\n".join([f"- `{c}`" for c in result["commits"][-3:]])
                await github.post_comment(
                    f"<!-- claude-agent -->\n:white_check_mark: **Initial implementation complete**\n\n{commit_info}"
                )
            else:
                await github.post_comment(
                    f"<!-- claude-agent -->\n:white_check_mark: **Analysis complete**"
                )
        else:
            await github.post_comment(
                f"<!-- claude-agent -->\n:warning: **Error during initial implementation**\n\n```\n{result.get('error', 'Unknown error')}\n```"
            )

        # Post summary to JIRA if this was a JIRA-triggered session
        if jira.enabled:
            summary = result.get("summary", "Initial implementation completed.")
            await jira.post_completion_summary(
                success=result["success"],
                summary=summary,
                commits=result.get("commits", []),
                error=result.get("error")
            )

    # Start background tasks (API server already running)
    logger.info("Starting prompt processing loop...")

    try:
        await asyncio.gather(
            api_server_task,  # Already running, just await it
            process_prompts(claude, github, session),
            check_idle_timeout(github),
        )
    except asyncio.CancelledError:
        logger.info("Shutdown requested")
    finally:
        # Cleanup
        logger.info("Shutting down...")
        dev_server.stop()
        session.mark_completed()
        logger.info("Agent terminated")


def handle_signal(signum, frame):
    """Handle shutdown signals."""
    global shutdown_requested
    logger.info(f"Received signal {signum}, requesting shutdown")
    shutdown_requested = True


if __name__ == "__main__":
    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Run main
    asyncio.run(main())
