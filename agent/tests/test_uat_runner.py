import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import uat_runner


class FakeProcess:
    def __init__(self, output: str, return_code: int = 0):
        self.stdout = io.StringIO(output)
        self.return_code = return_code

    def wait(self) -> int:
        return self.return_code


class UatRunnerTests(unittest.TestCase):
    def config(self) -> uat_runner.RunnerConfig:
        return uat_runner.RunnerConfig(
            session_id="uat-123",
            target_url="https://uat.example.test/path",
            results_s3_uri="s3://results/uat-123/results.json",
            artifacts_s3_uri="s3://results/uat-123/artifacts/",
            mobot_base_url="https://app.teammobot.dev",
            mobot_api_key_secret_id="",
            runner_prompt="Test {{targetUrl}}",
            azure_responses_endpoint="https://uat-gateway.example.openai.azure.com",
            azure_responses_api_key="dedicated-key",
        )

    def test_load_config_requires_dedicated_azure_values_and_defaults_model(self):
        values = {
            "SESSION_ID": "uat-123",
            "TARGET_URL": "https://uat.example.test",
            "RESULTS_S3_URI": "s3://results/uat-123/results.json",
            "TEST_PLAN_JSON": '{"testCases": []}',
            "RUNNER_PROMPT": "run {{targetUrl}}",
            "UAT_RUNNER_AZURE_RESPONSES_ENDPOINT": "https://gateway.example.openai.azure.com",
            "UAT_RUNNER_AZURE_RESPONSES_API_KEY": "dedicated-key",
        }
        with patch.dict(os.environ, values, clear=True):
            config = uat_runner.load_config()

        self.assertEqual(config.azure_responses_model, "gpt-5.6-terra")
        self.assertEqual(
            config.azure_responses_endpoint,
            "https://gateway.example.openai.azure.com",
        )

    def test_load_config_rejects_missing_dedicated_azure_key(self):
        values = {
            "SESSION_ID": "uat-123",
            "TARGET_URL": "https://uat.example.test",
            "RESULTS_S3_URI": "s3://results/uat-123/results.json",
            "UAT_RUNNER_AZURE_RESPONSES_ENDPOINT": "https://gateway.example.openai.azure.com",
        }
        with patch.dict(os.environ, values, clear=True):
            with self.assertRaisesRegex(
                RuntimeError, "UAT_RUNNER_AZURE_RESPONSES_API_KEY"
            ):
                uat_runner.load_config()

    def test_pi_command_uses_azure_responses_and_enabled_browser_workflow_tools(self):
        command = uat_runner.build_pi_command(
            self.config(), {"testCases": []}, "Run {{targetUrl}}"
        )

        self.assertEqual(command[0], "pi")
        self.assertIn("azure-openai-responses", command)
        self.assertIn("gpt-5.6-terra", command)
        self.assertIn("--mode", command)
        self.assertIn("json", command)
        self.assertIn("--tools", command)
        self.assertIn("bash,read,write,edit,grep,find,ls", command)
        self.assertIn("Run https://uat.example.test/path", command)
        self.assertNotIn("claude", command)
        self.assertNotIn("--dangerously-skip-permissions", command)

    def test_run_pi_maps_only_dedicated_azure_env_and_streams_progress(self):
        config = self.config()
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_dir = Path(temporary_directory) / "artifacts"
            log_path = artifact_dir / "pi-output.log"
            process = FakeProcess(
                json.dumps(
                    {
                        "type": "tool_execution_start",
                        "toolName": "bash",
                        "args": {"command": "npx playwright test"},
                    }
                )
                + "\n"
            )
            with (
                patch.object(uat_runner, "ARTIFACT_DIR", artifact_dir),
                patch.object(uat_runner, "PI_LOG_PATH", log_path),
                patch.dict(
                    os.environ,
                    {
                        "CLAUDE_CODE_USE_BEDROCK": "1",
                        "ANTHROPIC_MODEL": "old-model",
                        "ANTHROPIC_DEFAULT_OPUS_MODEL": "old-opus",
                    },
                    clear=True,
                ),
                patch("agent.uat_runner.subprocess.Popen", return_value=process) as popen,
                patch("agent.uat_runner.append_progress") as progress,
            ):
                exit_code = uat_runner.run_pi(config, {"testCases": []}, "Run {{targetUrl}}")

        self.assertEqual(exit_code, 0)
        _, kwargs = popen.call_args
        child_env = kwargs["env"]
        self.assertEqual(
            child_env["AZURE_OPENAI_BASE_URL"], config.azure_responses_endpoint
        )
        self.assertEqual(child_env["AZURE_OPENAI_API_KEY"], config.azure_responses_api_key)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", child_env)
        self.assertNotIn("ANTHROPIC_MODEL", child_env)
        self.assertNotIn("ANTHROPIC_DEFAULT_OPUS_MODEL", child_env)
        progress_messages = [call.args[0] for call in progress.call_args_list]
        self.assertIn("starting Pi with Azure OpenAI Responses", progress_messages)
        self.assertIn("tool bash: npx playwright test", progress_messages)
        self.assertIn("Pi exited with code 0", progress_messages)

    def test_pi_text_and_completion_events_produce_progress(self):
        with patch("agent.uat_runner.append_progress") as progress:
            uat_runner.handle_pi_stream_line(
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "text_delta",
                            "delta": "This is enough assistant progress to publish.",
                        },
                    }
                )
            )
            uat_runner.handle_pi_stream_line(json.dumps({"type": "agent_end"}))

        messages = [call.args[0] for call in progress.call_args_list]
        self.assertIn("This is enough assistant progress to publish.", messages)
        self.assertIn("Pi agent completed", messages)


if __name__ == "__main__":
    unittest.main()
