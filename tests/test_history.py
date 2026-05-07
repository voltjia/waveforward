from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waveforward.history import (  # noqa: E402
    discover_agent_sessions,
    import_agent_sessions,
)


class AgentHistoryImportTests(unittest.TestCase):
    def test_discovers_claude_codex_and_opencode_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_jsonl(
                home / ".claude/projects/demo/claude.jsonl",
                [
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "Help with tests"},
                        "timestamp": "2026-05-05T10:00:00Z",
                    },
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Use unittest."}],
                        },
                        "timestamp": "2026-05-05T10:01:00Z",
                    },
                ],
            )
            write_jsonl(
                home / ".codex/sessions/2026/05/rollout.jsonl",
                [
                    {
                        "type": "user_message",
                        "message": "Refactor the CLI",
                        "timestamp": "2026-05-05T11:00:00Z",
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "Done."},
                            ],
                        },
                        "timestamp": "2026-05-05T11:01:00Z",
                    },
                ],
            )
            write_json(
                home / ".local/share/opencode/storage/session/sess_alpha.json",
                {"id": "sess_alpha", "title": "OpenCode session"},
            )
            write_json(
                home / ".local/share/opencode/storage/message/sess_alpha/msg_1.json",
                {
                    "role": "user",
                    "parts": [{"type": "text", "text": "Add OpenCode support"}],
                    "created_at": "2026-05-05T12:00:00Z",
                },
            )
            write_json(
                home / ".local/share/opencode/storage/message/sess_alpha/msg_2.json",
                {
                    "role": "assistant",
                    "parts": [{"type": "text", "text": "Supported."}],
                    "created_at": "2026-05-05T12:01:00Z",
                },
            )

            candidates = discover_agent_sessions(home=home, limit=10)

            sources = {item["source"] for item in candidates}
            self.assertEqual(sources, {"claude-code", "codex", "opencode"})
            self.assertTrue(all("path" not in item for item in candidates))
            self.assertTrue(all(item["message_count"] == 2 for item in candidates))

    def test_import_selected_session_as_waveforward_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            home = Path(tmp) / "home"
            root.mkdir()
            write_jsonl(
                home / ".claude/projects/demo/claude.jsonl",
                [
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "Move this session"},
                        "timestamp": "2026-05-05T10:00:00Z",
                    },
                    {
                        "type": "assistant",
                        "message": {"role": "assistant", "content": "Ready."},
                        "timestamp": "2026-05-05T10:01:00Z",
                    },
                ],
            )
            [candidate] = discover_agent_sessions(home=home, sources=["claude-code"])

            [conversation] = import_agent_sessions(
                root,
                [candidate["id"]],
                home=home,
                machine="Laptop",
                owner="alpha",
            )

            self.assertEqual(conversation["owner"], "alpha")
            self.assertEqual(conversation["preferred"]["agent"], "claude-code")
            self.assertEqual(conversation["preferred"]["machine"], "Laptop")
            self.assertEqual(len(conversation["messages"]), 2)
            self.assertEqual(
                conversation["messages"][0]["content"],
                "Move this session",
            )
            self.assertEqual(conversation["imported_from"]["source"], "claude-code")


def write_jsonl(path: Path, items: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(item) for item in items) + "\n",
        encoding="utf-8",
    )


def write_json(path: Path, item: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(item), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
