from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from lol_support_advisor.codex_cli import (
    CodexCliClient, CodexCliError, FAST_CODEX_MODEL, FAST_REASONING_EFFORT,
    parse_codex_jsonl,
)


class CodexCliTests(unittest.TestCase):
    def test_jsonl_extracts_thread_and_last_agent_message(self) -> None:
        output = "\n".join((
            json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "first"},
            }),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "final"},
            }),
        ))
        self.assertEqual(parse_codex_jsonl(output), ("thread-123", "final"))

    def test_resume_uses_fast_model_minimal_effort_and_chatgpt_auth_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = CodexCliClient(
                Path(temp_dir), command="codex.cmd", timeout_seconds=5,
            )
            response = "\n".join((
                json.dumps({"type": "thread.started", "thread_id": "ephemeral-child"}),
                json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "answer"},
                }),
            ))
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=response, stderr="",
            )
            with patch.dict(os.environ, {"OPENAI_API_KEY": "must-not-be-used"}):
                with patch("subprocess.run", return_value=completed) as run:
                    turn = client.recommend("thread-123", "question")

            args = run.call_args.args[0]
            environment = run.call_args.kwargs["env"]
            self.assertEqual(turn.thread_id, "thread-123")
            self.assertIn("resume", args)
            self.assertIn(FAST_CODEX_MODEL, args)
            self.assertIn("--ephemeral", args)
            self.assertEqual(turn.thread_id, "thread-123")
            self.assertEqual(FAST_REASONING_EFFORT, "none")
            self.assertIn('model_reasoning_effort="none"', args)
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertEqual(run.call_args.kwargs["input"], "question")
            self.assertNotEqual(run.call_args.kwargs["creationflags"], None)

    def test_new_memory_thread_requires_returned_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = CodexCliClient(Path(temp_dir), command="codex.cmd")
            response = json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "ready"},
            })
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=response, stderr="",
            )
            with patch("subprocess.run", return_value=completed):
                with self.assertRaises(CodexCliError):
                    client.register_memory("rules")

    def test_memory_registration_starts_a_clean_persistent_base(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = CodexCliClient(Path(temp_dir), command="codex.cmd")
            response = "\n".join((
                json.dumps({"type": "thread.started", "thread_id": "clean-base"}),
                json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "ready"},
                }),
            ))
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=response, stderr="",
            )
            with patch("subprocess.run", return_value=completed) as run:
                turn = client.register_memory("rules", "old-slow-thread")
            args = run.call_args.args[0]
            self.assertEqual(turn.thread_id, "clean-base")
            self.assertNotIn("resume", args)
            self.assertNotIn("--ephemeral", args)

    def test_unsupported_fast_model_retries_with_account_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = CodexCliClient(Path(temp_dir), command="codex.cmd")
            failed = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="model not supported",
            )
            succeeded = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="\n".join((
                    json.dumps({"type": "thread.started", "thread_id": "new-thread"}),
                    json.dumps({
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "ready"},
                    }),
                )),
                stderr="",
            )
            with patch("subprocess.run", side_effect=(failed, succeeded)) as run:
                turn = client.register_memory("rules")
            self.assertEqual(run.call_count, 2)
            self.assertEqual(turn.model, "계정 기본 모델")
            self.assertNotIn("--model", run.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
