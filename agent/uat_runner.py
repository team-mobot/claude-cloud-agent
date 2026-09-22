import base64
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
PLAYWRIGHT_CONFIG_PATH = WORK_DIR / ".playwright" / "cli.config.json"
PI_LOG_PATH = ARTIFACT_DIR / "pi-output.log"
PROGRESS_PATH = ARTIFACT_DIR / "progress.jsonl"
PI_PROVIDER = "azure-openai-responses"
DEFAULT_AZURE_RESPONSES_MODEL = "gpt-5.6-terra"
PI_TOOLS = "bash,read,write,edit,grep,find,ls"


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
    azure_responses_endpoint: str = ""
    azure_responses_api_key: str = ""
    azure_responses_model: str = DEFAULT_AZURE_RESPONSES_MODEL


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
        azure_responses_endpoint=required_env("UAT_RUNNER_AZURE_RESPONSES_ENDPOINT"),
        azure_responses_api_key=required_env("UAT_RUNNER_AZURE_RESPONSES_API_KEY"),
        azure_responses_model=env(
            "UAT_RUNNER_AZURE_RESPONSES_MODEL", DEFAULT_AZURE_RESPONSES_MODEL
        ),
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


def jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{payload}{padding}")
        value = json.loads(decoded.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def token_is_expired(token: str) -> bool:
    exp = jwt_payload(token).get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return exp <= time.time() + 60


def has_api_key_auth(config: RunnerConfig) -> bool:
    return bool(env("MOBOT_API_KEY") or config.mobot_api_key_secret_id)


def resolve_auth_header(config: RunnerConfig) -> str:
    if config.mobot_auth_header:
        return config.mobot_auth_header
    if config.mobot_auth_token:
        if token_is_expired(config.mobot_auth_token):
            if has_api_key_auth(config):
                append_progress("provided Mobot auth token is expired; exchanging API key for a fresh token")
                return f"Token {exchange_api_key(config)}"
            raise RuntimeError(
                "MOBOT_AUTH_TOKEN is expired; provide a fresh token or configure MOBOT_API_KEY_SECRET_ID"
            )
        return f"Token {config.mobot_auth_token}"
    return f"Token {exchange_api_key(config)}"


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(value, indent=2)}\n", encoding="utf-8")


def configure_playwright_cli() -> None:
    write_json(
        PLAYWRIGHT_CONFIG_PATH,
        {
            "browser": {
                "browserName": "chromium",
                "launchOptions": {
                    "channel": "chromium",
                    "headless": True,
                },
            },
            "timeouts": {
                "action": 10000,
                "navigation": 90000,
            },
        },
    )
    append_progress("configured Playwright CLI for Chromium")


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


def handle_pi_stream_line(line: str) -> None:
    stripped = line.strip()
    if not stripped:
        return
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        append_progress(compact_text(stripped), {"source": "pi"})
        return

    event_type = event.get("type")
    if event_type == "tool_execution_start":
        append_progress(
            summarize_tool_use(
                {
                    "name": event.get("toolName"),
                    "input": event.get("args"),
                }
            ),
            {"source": "tool_use"},
        )
    elif event_type == "tool_execution_end":
        status = "failed" if event.get("isError") else "completed"
        append_progress(
            f"tool {event.get('toolName') or 'unknown'} {status}",
            {"source": "tool_use"},
        )
    elif event_type == "message_update":
        assistant_event = event.get("assistantMessageEvent")
        if isinstance(assistant_event, dict) and assistant_event.get("type") == "text_delta":
            emit_text_progress(str(assistant_event.get("delta") or ""))
    elif event_type == "message_end":
        message = event.get("message")
        if isinstance(message, dict):
            for item in message.get("content", []):
                if isinstance(item, dict) and item.get("type") == "text":
                    emit_text_progress(str(item.get("text") or ""))
    elif event_type == "agent_end":
        append_progress("Pi agent completed", {"source": "result"})


def render_prompt_template(template: str, config: RunnerConfig, plan: dict[str, Any]) -> str:
    target = urlparse(config.target_url)
    replacements = {
        "targetUrl": config.target_url,
        "targetOrigin": f"{target.scheme}://{target.netloc}",
        "targetHost": target.hostname or target.netloc,
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


def build_pi_command(config: RunnerConfig, plan: dict[str, Any], runner_prompt: str) -> list[str]:
    return [
        "pi",
        "--provider",
        PI_PROVIDER,
        "--model",
        config.azure_responses_model,
        "--mode",
        "json",
        "--no-session",
        "--tools",
        PI_TOOLS,
        "--print",
        render_prompt_template(runner_prompt, config, plan),
    ]


def run_pi(config: RunnerConfig, plan: dict[str, Any], runner_prompt: str) -> int:
    env_vars = os.environ.copy()
    # Pi uses these provider-native names. Keep the runner's Azure credentials
    # distinct from any generic application or development-agent credentials.
    env_vars["AZURE_OPENAI_BASE_URL"] = config.azure_responses_endpoint
    env_vars["AZURE_OPENAI_API_KEY"] = config.azure_responses_api_key
    env_vars.pop("CLAUDE_CODE_USE_BEDROCK", None)
    env_vars.pop("ANTHROPIC_MODEL", None)
    env_vars.pop("ANTHROPIC_DEFAULT_OPUS_MODEL", None)

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    append_progress("starting Pi with Azure OpenAI Responses")
    with PI_LOG_PATH.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            build_pi_command(config, plan, runner_prompt),
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
                handle_pi_stream_line(line)
        return_code = process.wait()
    append_progress(f"Pi exited with code {return_code}")
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
        return fallback_results(config, plan, "UAT agent did not write results.json")

    try:
        results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return fallback_results(config, plan, f"UAT agent wrote invalid results JSON: {exc}")

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
    configure_playwright_cli()

    try:
        auth_header = resolve_auth_header(config)
        os.environ["MOBOT_AUTH_HEADER"] = auth_header
        if auth_header.startswith("Token "):
            os.environ["MOBOT_AUTH_TOKEN"] = auth_header.removeprefix("Token ")

        pi_exit_code = run_pi(config, plan, runner_prompt)
        results = normalize_results(config, plan)
        if pi_exit_code != 0 and results.get("status") == "passed":
            results["status"] = "error"
            results["summary"] = f"Pi exited with code {pi_exit_code} after writing results."
    except Exception as error:
        results = fallback_results(config, plan, str(error))

    publish_results(config, results)
    print(json.dumps({"status": results["status"], "resultsS3Uri": config.results_s3_uri}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # A preflight failure (for example a missing prompt or malformed runner
        # configuration) used to exit before `main()` had a RunnerConfig, so no
        # result object was uploaded. The workflow then reported every planned
        # case as mysteriously blocked. Publish a best-effort diagnostic result
        # whenever RESULTS_S3_URI is present, even if config loading failed.
        message = f"UAT runner failed before normal result publishing: {error}"
        print(message, file=sys.stderr)
        try:
            results_s3_uri = env("RESULTS_S3_URI")
            if results_s3_uri:
                target_url = env("TARGET_URL")
                result = {
                    "status": "error",
                    "summary": message,
                    "targetUrl": target_url,
                    "cases": [],
                }
                WORK_DIR.mkdir(parents=True, exist_ok=True)
                write_json(RESULTS_PATH, result)
                upload_file(RESULTS_PATH, results_s3_uri)
                artifacts_s3_uri = env("ARTIFACTS_S3_URI")
                if artifacts_s3_uri:
                    upload_directory(ARTIFACT_DIR, artifacts_s3_uri)
                print(json.dumps({"status": "error", "resultsS3Uri": results_s3_uri}))
        except Exception as publish_error:
            print(f"UAT runner also failed to publish fallback result: {publish_error}", file=sys.stderr)
        sys.exit(1)
