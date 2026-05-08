from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waveforward.core import AgentSyncError, initialize_workspace  # noqa: E402
from waveforward.runner import AgentRunResult  # noqa: E402
from waveforward.service import (  # noqa: E402
    create_conversation,
    execute_slash_command,
    get_conversation,
    run_conversation_turn,
)


class ServiceConversationTests(unittest.TestCase):
    def test_agent_route_options_reach_runner_and_messages(self) -> None:
        with git_repo() as root:
            initialize_workspace(root, machine_name="service-test")
            conversation = create_conversation(
                root,
                agent="codex",
                machine="Machine A",
                model="gpt-5.5",
                reasoning_effort="high",
                title="Model session",
            )

            def route_runner(
                _root: Path,
                *,
                agent: str,
                prompt: str,
                model: str | None = None,
                reasoning_effort: str | None = None,
            ) -> AgentRunResult:
                self.assertEqual(agent, "codex")
                self.assertIn("Model: gpt-5.4", prompt)
                self.assertIn("Reasoning effort: medium", prompt)
                return AgentRunResult(
                    agent=agent,
                    command=("fake-agent",),
                    returncode=0,
                    output="model-aware response",
                    model=model,
                    reasoning_effort=reasoning_effort,
                )

            result = run_conversation_turn(
                root,
                conversation["id"],
                content="Use the selected model.",
                agent="codex",
                machine="Machine A",
                model="gpt-5.4",
                reasoning_effort="medium",
                agent_runner=route_runner,
            )

            self.assertEqual(result.conversation["preferred"]["model"], "gpt-5.4")
            self.assertEqual(
                result.conversation["preferred"]["reasoning_effort"],
                "medium",
            )
            self.assertEqual(result.agent_run.model, "gpt-5.4")
            self.assertEqual(result.agent_run.reasoning_effort, "medium")
            self.assertEqual(result.conversation["messages"][-1]["model"], "gpt-5.4")

    def test_slash_commands_manage_conversation_without_agent_runner(self) -> None:
        with git_repo() as root:
            initialize_workspace(root, machine_name="command-test")
            conversation = create_conversation(
                root,
                title="Command session",
                agent="codex",
                machine="local",
            )

            sessions = execute_slash_command(
                root,
                conversation["id"],
                content="/sessions",
                agent="codex",
                machine="local",
            )

            self.assertEqual(sessions.command["name"], "sessions")
            self.assertEqual(
                sessions.conversation["messages"][-2]["content"], "/sessions"
            )
            self.assertEqual(sessions.conversation["messages"][-1]["role"], "service")
            self.assertIn("Sessions:", sessions.conversation["messages"][-1]["content"])

            model = execute_slash_command(
                root,
                conversation["id"],
                content="/model gpt-5.5 high",
                agent="codex",
                machine="local",
            )

            self.assertEqual(model.command["route"]["model"], "gpt-5.5")
            self.assertEqual(model.command["route"]["reasoning_effort"], "high")
            self.assertEqual(model.conversation["preferred"]["model"], "gpt-5.5")
            self.assertEqual(
                model.conversation["preferred"]["reasoning_effort"],
                "high",
            )

            renamed = execute_slash_command(
                root,
                conversation["id"],
                content="/rename Command Center",
            )

            self.assertEqual(renamed.conversation["title"], "Command Center")
            self.assertEqual(renamed.command["title"], "Command Center")

            archived = execute_slash_command(
                root,
                conversation["id"],
                content="/archive",
            )

            self.assertTrue(archived.command["archived"])
            self.assertIsNotNone(archived.conversation["archived_at"])

    def test_cancel_check_prevents_agent_and_assistant_message(self) -> None:
        with git_repo() as root:
            initialize_workspace(root, machine_name="cancel-test")
            conversation = create_conversation(root, title="Cancel session")

            def should_not_run(*_args: object, **_kwargs: object) -> AgentRunResult:
                raise AssertionError("agent runner should not be called")

            with self.assertRaisesRegex(AgentSyncError, "Run canceled"):
                run_conversation_turn(
                    root,
                    conversation["id"],
                    content="Cancel this turn.",
                    agent_runner=should_not_run,
                    cancel_check=lambda: True,
                )

            saved = get_conversation(root, conversation["id"])
            self.assertEqual([item["role"] for item in saved["messages"]], ["user"])


class git_repo:
    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        run(["git", "init"], root)
        run(["git", "config", "user.name", "WaveForward Tests"], root)
        run(["git", "config", "user.email", "waveforward@example.test"], root)
        (root / ".gitignore").write_text(".waveforward/\n", encoding="utf-8")
        run(["git", "add", ".gitignore"], root)
        run(["git", "commit", "-m", "initial"], root)
        self.root = root
        return root

    def __exit__(self, *_exc: object) -> None:
        self._tmp.cleanup()


def run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
