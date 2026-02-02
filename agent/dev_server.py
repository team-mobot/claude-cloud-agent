"""
Dev server auto-detection and management.

Automatically detects and starts the target application's dev server.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class DevServerManager:
    """
    Manages the target application's dev server.

    Auto-detects the appropriate dev command based on project files.
    """

    # Detection priority order
    DETECTION_ORDER = [
        "package_json_dev",
        "package_json_start",
        "pyproject_poetry",
        "pyproject_uvicorn",
        "requirements_uvicorn",
        "requirements_flask",
        "requirements_django",
    ]

    def __init__(self, workspace: str):
        """
        Initialize dev server manager.

        Args:
            workspace: Path to the project workspace
        """
        self.workspace = Path(workspace)
        self.process: Optional[subprocess.Popen] = None
        self.port = 3000

    async def start(self) -> bool:
        """
        Detect and start the dev server.

        Returns:
            True if server started successfully
        """
        # Detect project type
        cmd, install_cmd = self._detect_dev_command()

        if not cmd:
            logger.warning("Could not detect dev server command")
            return False

        # Install dependencies if needed
        if install_cmd:
            logger.info(f"Installing dependencies: {install_cmd}")
            try:
                install_result = await asyncio.to_thread(
                    subprocess.run,
                    install_cmd,
                    shell=True,
                    cwd=self.workspace,
                    capture_output=True,
                    text=True,
                    timeout=300  # 5 minute timeout for install
                )
                if install_result.returncode != 0:
                    logger.error(f"Dependency install failed: {install_result.stderr}")
                    # Continue anyway, might work
            except Exception as e:
                logger.warning(f"Dependency install error: {e}")

        # Start dev server
        logger.info(f"Starting dev server: {cmd}")
        try:
            env = os.environ.copy()
            env["PORT"] = str(self.port)

            self.process = subprocess.Popen(
                cmd,
                shell=True,
                cwd=self.workspace,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid  # Create new process group for cleanup
            )

            # Start log forwarding
            asyncio.create_task(self._forward_logs())

            # Wait a moment for server to start
            await asyncio.sleep(3)

            if self.process.poll() is None:
                logger.info(f"Dev server started on port {self.port}")
                return True
            else:
                logger.error("Dev server exited immediately")
                return False

        except Exception as e:
            logger.exception(f"Failed to start dev server: {e}")
            return False

    def stop(self) -> None:
        """Stop the dev server."""
        if self.process:
            logger.info("Stopping dev server...")
            try:
                # Kill the process group
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=10)
            except Exception as e:
                logger.warning(f"Error stopping dev server: {e}")
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

    async def _forward_logs(self) -> None:
        """Forward dev server logs to our logger."""
        if not self.process or not self.process.stdout:
            return

        try:
            while self.process.poll() is None:
                line = await asyncio.to_thread(self.process.stdout.readline)
                if line:
                    logger.info(f"[dev-server] {line.decode().rstrip()}")
                else:
                    await asyncio.sleep(0.1)
        except Exception as e:
            logger.warning(f"Log forwarding ended: {e}")

    def _detect_dev_command(self) -> tuple[Optional[str], Optional[str]]:
        """
        Detect the appropriate dev command for this project.

        Returns:
            Tuple of (dev_command, install_command) or (None, None)
        """
        for detector in self.DETECTION_ORDER:
            method = getattr(self, f"_detect_{detector}", None)
            if method:
                result = method()
                if result[0]:
                    logger.info(f"Detected project type: {detector}")
                    return result

        return None, None

    def _detect_package_json_dev(self) -> tuple[Optional[str], Optional[str]]:
        """Detect npm/pnpm/yarn project with 'dev' script."""
        package_json = self.workspace / "package.json"
        if not package_json.exists():
            return None, None

        try:
            with open(package_json) as f:
                pkg = json.load(f)

            scripts = pkg.get("scripts", {})
            if "dev" not in scripts:
                return None, None

            # Detect package manager
            pm = self._detect_package_manager()
            install_cmd = f"{pm} install"

            # Check the dev script to determine how to pass port
            dev_script = scripts.get("dev", "")

            # Build command with port override
            # Most frameworks (Vite, Next.js) accept --port via passthrough
            if "vite" in dev_script.lower():
                # Vite uses -- --port
                dev_cmd = f"{pm} run dev -- --port {self.port} --host"
            elif "next" in dev_script.lower():
                # Next.js uses -p
                dev_cmd = f"{pm} run dev -- -p {self.port}"
            else:
                # Default: pass --port and hope it works, or rely on PORT env
                dev_cmd = f"{pm} run dev -- --port {self.port}"

            return dev_cmd, install_cmd

        except Exception as e:
            logger.warning(f"Error reading package.json: {e}")
            return None, None

    def _detect_package_json_start(self) -> tuple[Optional[str], Optional[str]]:
        """Detect npm project with 'start' script."""
        package_json = self.workspace / "package.json"
        if not package_json.exists():
            return None, None

        try:
            with open(package_json) as f:
                pkg = json.load(f)

            scripts = pkg.get("scripts", {})
            if "start" not in scripts:
                return None, None

            pm = self._detect_package_manager()
            install_cmd = f"{pm} install"
            start_cmd = f"{pm} start"

            return start_cmd, install_cmd

        except Exception as e:
            logger.warning(f"Error reading package.json: {e}")
            return None, None

    def _detect_package_manager(self) -> str:
        """Detect which package manager to use."""
        if (self.workspace / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (self.workspace / "yarn.lock").exists():
            return "yarn"
        return "npm"

    def _detect_pyproject_poetry(self) -> tuple[Optional[str], Optional[str]]:
        """Detect Poetry project."""
        pyproject = self.workspace / "pyproject.toml"
        if not pyproject.exists():
            return None, None

        try:
            content = pyproject.read_text()
            if "[tool.poetry]" not in content:
                return None, None

            # Look for common entry points
            if "uvicorn" in content.lower() or "fastapi" in content.lower():
                return (
                    "poetry run uvicorn main:app --host 0.0.0.0 --port 3000 --reload",
                    "poetry install"
                )

            # Try to find main module
            if (self.workspace / "main.py").exists():
                return "poetry run python main.py", "poetry install"

            return None, "poetry install"

        except Exception as e:
            logger.warning(f"Error reading pyproject.toml: {e}")
            return None, None

    def _detect_pyproject_uvicorn(self) -> tuple[Optional[str], Optional[str]]:
        """Detect pyproject.toml with uvicorn dependency."""
        pyproject = self.workspace / "pyproject.toml"
        if not pyproject.exists():
            return None, None

        try:
            content = pyproject.read_text()
            if "uvicorn" not in content.lower():
                return None, None

            # Find main app file
            for app_file in ["main.py", "app.py", "api.py"]:
                if (self.workspace / app_file).exists():
                    module = app_file.replace(".py", "")
                    return (
                        f"uvicorn {module}:app --host 0.0.0.0 --port 3000 --reload",
                        "pip install -e ."
                    )

            return None, None

        except Exception as e:
            logger.warning(f"Error: {e}")
            return None, None

    def _detect_requirements_uvicorn(self) -> tuple[Optional[str], Optional[str]]:
        """Detect requirements.txt with uvicorn."""
        req_file = self.workspace / "requirements.txt"
        if not req_file.exists():
            return None, None

        try:
            content = req_file.read_text()
            if "uvicorn" not in content.lower():
                return None, None

            # Find main app file
            for app_file in ["main.py", "app.py", "api.py"]:
                if (self.workspace / app_file).exists():
                    module = app_file.replace(".py", "")
                    return (
                        f"uvicorn {module}:app --host 0.0.0.0 --port 3000 --reload",
                        "pip install -r requirements.txt"
                    )

            return None, None

        except Exception as e:
            logger.warning(f"Error: {e}")
            return None, None

    def _detect_requirements_flask(self) -> tuple[Optional[str], Optional[str]]:
        """Detect Flask application."""
        req_file = self.workspace / "requirements.txt"
        if not req_file.exists():
            return None, None

        try:
            content = req_file.read_text()
            if "flask" not in content.lower():
                return None, None

            # Find main app file
            for app_file in ["app.py", "main.py", "wsgi.py"]:
                if (self.workspace / app_file).exists():
                    return (
                        f"flask run --host 0.0.0.0 --port 3000",
                        "pip install -r requirements.txt"
                    )

            return None, None

        except Exception as e:
            logger.warning(f"Error: {e}")
            return None, None

    def _detect_requirements_django(self) -> tuple[Optional[str], Optional[str]]:
        """Detect Django application."""
        req_file = self.workspace / "requirements.txt"
        manage_py = self.workspace / "manage.py"

        if not req_file.exists() or not manage_py.exists():
            return None, None

        try:
            content = req_file.read_text()
            if "django" not in content.lower():
                return None, None

            return (
                "python manage.py runserver 0.0.0.0:3000",
                "pip install -r requirements.txt"
            )

        except Exception as e:
            logger.warning(f"Error: {e}")
            return None, None
