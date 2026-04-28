import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
import requests


WORK_DIR = Path(os.environ.get("UAT_WORK_DIR", "/workspace/uat-runner"))
ARTIFACT_DIR = WORK_DIR / "artifacts"
RESULTS_PATH = WORK_DIR / "results.json"
PLAN_PATH = WORK_DIR / "test-plan.json"
CLAUDE_LOG_PATH = ARTIFACT_DIR / "claude-output.log"
PROGRESS_PATH = ARTIFACT_DIR / "progress.jsonl"


@dataclass(frozen=True)
class RunnerConfig:
    session_id: str
    target_url: str
    results_s3_uri: str
    artifacts_s3_uri: str
    mobot_base_url: str
    mobot_api_key_secret_id: str
    pr_number: str = ""
    mobot_auth_header: str = ""
    mobot_auth_token: str = ""
    test_plan_s3_uri: str = ""
    test_plan_json: str = ""
    runner_prompt_s3_uri: str = ""
    runner_prompt: str = ""
    claude_model: str = ""


def env(name: str, fallback: str = "") -> str:
    return os.environ.get(name) or fallback


def required_env(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_config() -> RunnerConfig:
    return RunnerConfig(
        session_id=required_env("SESSION_ID"),
        target_url=required_env("TARGET_URL"),
        results_s3_uri=required_env("RESULTS_S3_URI"),
        artifacts_s3_uri=env("ARTIFACTS_S3_URI"),
        mobot_base_url=env("MOBOT_BASE_URL", "https://app.teammobot.dev").rstrip("/"),
        mobot_api_key_secret_id=env("MOBOT_API_KEY_SECRET_ID"),
        pr_number=env("PR_NUMBER"),
        mobot_auth_header=env("MOBOT_AUTH_HEADER"),
        mobot_auth_token=env("MOBOT_AUTH_TOKEN"),
        test_plan_s3_uri=env("TEST_PLAN_S3_URI"),
        test_plan_json=env("TEST_PLAN_JSON"),
        runner_prompt_s3_uri=env("RUNNER_PROMPT_S3_URI"),
        runner_prompt=env("RUNNER_PROMPT"),
        claude_model=env("CLAUDE_MODEL"),
    )


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError(f"Invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def s3_client():
    return boto3.client("s3")


def download_json(uri: str) -> dict[str, Any]:
    bucket, key = parse_s3_uri(uri)
    response = s3_client().get_object(Bucket=bucket, Key=key)
    return json.loads(response["Body"].read().decode("utf-8"))


def download_text(uri: str) -> str:
    bucket, key = parse_s3_uri(uri)
    response = s3_client().get_object(Bucket=bucket, Key=key)
    return response["Body"].read().decode("utf-8")


def upload_file(path: Path, uri: str) -> None:
    bucket, key = parse_s3_uri(uri)
    s3_client().upload_file(str(path), bucket, key)


def upload_directory(source: Path, destination_uri: str) -> None:
    if not destination_uri or not source.exists():
        return

    bucket, prefix = parse_s3_uri(destination_uri.rstrip("/") + "/placeholder")
    prefix = prefix.removesuffix("placeholder")
    client = s3_client()
    for file_path in source.rglob("*"):
        if file_path.is_file():
            key = f"{prefix}{file_path.relative_to(source).as_posix()}"
            client.upload_file(str(file_path), bucket, key)


def load_test_plan(config: RunnerConfig) -> dict[str, Any]:
    if config.test_plan_json:
        return json.loads(config.test_plan_json)
    if config.test_plan_s3_uri:
        return download_json(config.test_plan_s3_uri)
    raise RuntimeError("Set TEST_PLAN_JSON or TEST_PLAN_S3_URI")


def load_runner_prompt(config: RunnerConfig) -> str:
    if config.runner_prompt:
        return config.runner_prompt
    if config.runner_prompt_s3_uri:
        return download_text(config.runner_prompt_s3_uri)
    raise RuntimeError("Set RUNNER_PROMPT or RUNNER_PROMPT_S3_URI")


def get_mobot_api_key(config: RunnerConfig) -> str:
    direct_key = env("MOBOT_API_KEY")
    if direct_key:
        return direct_key
    if not config.mobot_api_key_secret_id:
        raise RuntimeError("Set MOBOT_API_KEY_SECRET_ID or MOBOT_API_KEY")

    response = boto3.client("secretsmanager").get_secret_value(
        SecretId=config.mobot_api_key_secret_id,
    )
    secret = response.get("SecretString") or ""
    try:
        data = json.loads(secret)
    except json.JSONDecodeError:
        return secret.strip()

    for key in ("api_key", "mobot_api_key", "MOBOT_API_KEY", "token"):
        if data.get(key):
            return str(data[key])
    raise RuntimeError(f"Secret {config.mobot_api_key_secret_id} did not include an API key")


def exchange_api_key(config: RunnerConfig) -> str:
    api_key = get_mobot_api_key(config)
    response = requests.post(
        f"{config.mobot_base_url}/api/login/api-key/session",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            f"Mobot API key session exchange failed: {response.status_code} {response.text[:500]}"
        )

    token = response.json().get("token")
    if not token:
        raise RuntimeError("Mobot API key session response did not include token")
    return str(token)


def resolve_auth_header(config: RunnerConfig) -> str:
    if config.mobot_auth_header:
        return config.mobot_auth_header
    if config.mobot_auth_token:
        return f"Token {config.mobot_auth_token}"
    return f"Token {exchange_api_key(config)}"


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(value, indent=2)}\n", encoding="utf-8")


def append_progress(message: str, payload: dict[str, Any] | None = None) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "message": message,
        **(payload or {}),
    }
    with PROGRESS_PATH.open("a", encoding="utf-8") as progress_file:
        progress_file.write(f"{json.dumps(event, separators=(',', ':'))}\n")
    print(f"UAT progress: {message}", flush=True)


def compact_text(value: str, max_length: int = 240) -> str:
    text = " ".join(value.split())
    if len(text) <= max_length:
        return text
    return f"{text[:max_length].rstrip()}..."


def redact_progress_text(value: str) -> str:
    text = re.sub(
        r"(?i)(authorization|password|secret|token|api[_-]?key)(\s*[:=]\s*)([^\s'\";]+)",
        r"\1\2[redacted]",
        value,
    )
    return re.sub(r"(?i)(bearer|token)\s+[a-z0-9._~+/=-]{12,}", r"\1 [redacted]", text)


def summarize_tool_use(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "unknown")
    tool_input = item.get("input")
    if not isinstance(tool_input, dict):
        return f"tool {name}"

    if name == "TodoWrite":
        todos = tool_input.get("todos")
        if isinstance(todos, list):
            in_progress = [
                str(todo.get("content") or "").strip()
                for todo in todos
                if isinstance(todo, dict) and todo.get("status") == "in_progress"
            ]
            if in_progress:
                return f"todo: {compact_text(in_progress[0], 180)}"
            pending_count = sum(
                1
                for todo in todos
                if isinstance(todo, dict) and todo.get("status") == "pending"
            )
            completed_count = sum(
                1
                for todo in todos
                if isinstance(todo, dict) and todo.get("status") == "completed"
            )
            return f"todo update: {completed_count} completed, {pending_count} pending"

    for key in ("description", "query", "path", "file_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return f"tool {name}: {compact_text(redact_progress_text(value), 180)}"

    command = tool_input.get("command")
    if isinstance(command, str) and command.strip():
        first_line = next((line.strip() for line in command.splitlines() if line.strip()), "")
        return f"tool {name}: {compact_text(redact_progress_text(first_line), 180)}"

    return f"tool {name}"


def emit_text_progress(text: str) -> None:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("UAT_PROGRESS"):
            append_progress(line)
            continue
        if len(line) >= 24:
            append_progress(compact_text(line), {"source": "assistant"})
            return


def handle_claude_stream_line(line: str) -> None:
    stripped = line.strip()
    if not stripped:
        return
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        append_progress(compact_text(stripped), {"source": "claude"})
        return

    event_type = event.get("type")
    if event_type == "assistant":
        for item in event.get("message", {}).get("content", []):
            item_type = item.get("type")
            if item_type == "text":
                emit_text_progress(str(item.get("text") or ""))
            elif item_type == "tool_use":
                append_progress(summarize_tool_use(item), {"source": "tool_use"})
    elif event_type == "result":
        status = event.get("subtype") or event.get("result") or "completed"
        append_progress(f"Claude result: {status}", {"source": "result"})
    elif event_type == "system" and event.get("subtype"):
        append_progress(f"Claude system: {event['subtype']}", {"source": "system"})


def render_prompt_template(template: str, config: RunnerConfig, plan: dict[str, Any]) -> str:
    replacements = {
        "targetUrl": config.target_url,
        "sessionId": config.session_id,
        "mobotBaseUrl": config.mobot_base_url,
        "prNumber": config.pr_number,
        "workDir": str(WORK_DIR),
        "artifactDir": str(ARTIFACT_DIR),
        "resultsPath": str(RESULTS_PATH),
        "testPlanJson": json.dumps(plan, indent=2),
    }
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
        rendered = rendered.replace(f"{{{{ {key} }}}}", value)
    return rendered.strip()


def run_claude(config: RunnerConfig, plan: dict[str, Any], runner_prompt: str) -> int:
    command = [
        "claude",
        "--dangerously-skip-permissions",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if config.claude_model:
        command.extend(["--model", config.claude_model])
    command.extend(["-p", render_prompt_template(runner_prompt, config, plan)])

    env_vars = os.environ.copy()
    env_vars.setdefault("CLAUDE_CODE_USE_BEDROCK", "1")
    env_vars.setdefault("NODE_PATH", "/usr/local/lib/node_modules")
    if config.claude_model:
        env_vars.setdefault("ANTHROPIC_MODEL", config.claude_model)
        if "opus" in config.claude_model:
            env_vars.setdefault("ANTHROPIC_DEFAULT_OPUS_MODEL", config.claude_model)

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    append_progress("starting Claude Code")
    with CLAUDE_LOG_PATH.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            cwd=str(WORK_DIR),
            env=env_vars,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if process.stdout:
            for line in process.stdout:
                log_file.write(line)
                log_file.flush()
                handle_claude_stream_line(line)
        return_code = process.wait()
    append_progress(f"Claude Code exited with code {return_code}")
    return return_code


def normalize_status(status: Any) -> str:
    value = str(status or "").lower()
    if value in {"pass", "passed", "success"}:
        return "pass"
    if value in {"fail", "failed", "failure"}:
        return "fail"
    if value == "blocked":
        return "blocked"
    return "blocked"


def fallback_case_id(config: RunnerConfig, index: int) -> str:
    if config.pr_number:
        return f"TC{config.pr_number}-{index + 1:02d}"
    return f"TC-{index + 1:03d}"


def normalize_results(config: RunnerConfig, plan: dict[str, Any]) -> dict[str, Any]:
    if not RESULTS_PATH.exists():
        return fallback_results(config, plan, "Claude did not write results.json")

    try:
        results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return fallback_results(config, plan, f"Claude wrote invalid results JSON: {exc}")

    cases = results.get("cases")
    if not isinstance(cases, list):
        cases = []

    normalized_cases = []
    for index, test_case in enumerate(cases):
        normalized_cases.append(
            {
                "id": str(test_case.get("id") or fallback_case_id(config, index)),
                "title": str(test_case.get("title") or f"Test case {index + 1}"),
                "status": normalize_status(test_case.get("status")),
                "notes": str(test_case.get("notes") or test_case.get("error") or ""),
                "evidence": test_case.get("evidence") if isinstance(test_case.get("evidence"), list) else [],
            }
        )

    status = str(results.get("status") or "").lower()
    if not status:
        if any(test_case["status"] == "fail" for test_case in normalized_cases):
            status = "failed"
        elif normalized_cases and all(test_case["status"] == "pass" for test_case in normalized_cases):
            status = "passed"
        elif normalized_cases:
            status = "blocked"
        else:
            status = "error"

    return {
        "status": status,
        "summary": str(results.get("summary") or "UAT runner completed."),
        "targetUrl": str(results.get("targetUrl") or config.target_url),
        "cases": normalized_cases,
    }


def fallback_results(config: RunnerConfig, plan: dict[str, Any], reason: str) -> dict[str, Any]:
    plan_cases = plan.get("testCases") if isinstance(plan.get("testCases"), list) else []
    cases = [
        {
            "id": str(test_case.get("id") or fallback_case_id(config, index)),
            "title": str(test_case.get("title") or f"Test case {index + 1}"),
            "status": "blocked",
            "notes": reason,
            "evidence": [],
        }
        for index, test_case in enumerate(plan_cases)
    ]
    return {
        "status": "error",
        "summary": reason,
        "targetUrl": config.target_url,
        "cases": cases,
    }


def publish_results(config: RunnerConfig, results: dict[str, Any]) -> None:
    write_json(RESULTS_PATH, results)
    upload_file(RESULTS_PATH, config.results_s3_uri)
    upload_directory(ARTIFACT_DIR, config.artifacts_s3_uri)


def main() -> int:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    config = load_config()
    plan = load_test_plan(config)
    runner_prompt = load_runner_prompt(config)
    write_json(PLAN_PATH, plan)

    try:
        auth_header = resolve_auth_header(config)
        os.environ["MOBOT_AUTH_HEADER"] = auth_header
        if auth_header.startswith("Token "):
            os.environ["MOBOT_AUTH_TOKEN"] = auth_header.removeprefix("Token ")

        claude_exit_code = run_claude(config, plan, runner_prompt)
        results = normalize_results(config, plan)
        if claude_exit_code != 0 and results.get("status") == "passed":
            results["status"] = "error"
            results["summary"] = f"Claude exited with code {claude_exit_code} after writing results."
    except Exception as error:
        results = fallback_results(config, plan, str(error))

    publish_results(config, results)
    print(json.dumps({"status": results["status"], "resultsS3Uri": config.results_s3_uri}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"UAT runner failed before publishing results: {error}", file=sys.stderr)
        raise
