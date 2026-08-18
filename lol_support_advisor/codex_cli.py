from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any


FAST_CODEX_MODEL = "gpt-5.6-luna"
FAST_REASONING_EFFORT = "none"


class CodexCliError(RuntimeError):
    """Raised when the local Codex CLI cannot complete a conversation turn."""


class CodexCliUnavailable(CodexCliError):
    """Raised when no usable Codex CLI command can be found."""


@dataclass(frozen=True)
class CodexTurn:
    thread_id: str
    message: str
    model: str


def parse_codex_jsonl(output: str, existing_thread_id: str = "") -> tuple[str, str]:
    """Extract the persistent thread id and final agent text from exec JSONL."""
    thread_id = existing_thread_id.strip()
    messages: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event: dict[str, Any] = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if event.get("type") == "thread.started" and event.get("thread_id"):
            thread_id = str(event["thread_id"]).strip()
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and item.get("text")
        ):
            messages.append(str(item["text"]).strip())
    return thread_id, messages[-1] if messages else ""


class CodexCliClient:
    """Small, console-free wrapper around a ChatGPT-authenticated Codex CLI."""

    def __init__(
        self,
        work_dir: Path,
        command: str | Path | None = None,
        *,
        model: str = FAST_CODEX_MODEL,
        timeout_seconds: int = 90,
    ) -> None:
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.command = str(command) if command else self.find_command()
        self.model = model
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def find_command() -> str:
        candidates: list[Path] = []
        local_app_data = os.environ.get("LOCALAPPDATA")
        app_data = os.environ.get("APPDATA")
        if local_app_data:
            pnpm_bin = Path(local_app_data) / "pnpm" / "bin"
            candidates.extend((pnpm_bin / "codex.cmd", pnpm_bin / "codex.exe"))
        if app_data:
            npm_bin = Path(app_data) / "npm"
            candidates.extend((npm_bin / "codex.cmd", npm_bin / "codex.exe"))
        for name in ("codex.cmd", "codex.exe", "codex"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate).casefold()
            if key in seen:
                continue
            seen.add(key)
            if candidate.is_file():
                return str(candidate)
        raise CodexCliUnavailable(
            "Codex CLI를 찾지 못했습니다. Riot 설정에서 설치 상태를 확인하세요."
        )

    def version(self) -> str:
        result = self._subprocess([self.command, "--version"], "")
        if result.returncode:
            raise CodexCliError(self._friendly_error(result.stderr, result.stdout))
        return result.stdout.strip()

    def login_status(self) -> str:
        result = self._subprocess([self.command, "login", "status"], "")
        combined = "\n".join((result.stdout, result.stderr)).strip()
        if result.returncode:
            raise CodexCliError(self._friendly_error(combined, ""))
        return combined

    def register_memory(self, memory_prompt: str, thread_id: str = "") -> CodexTurn:
        prompt = (
            memory_prompt.rstrip()
            + "\n\n지금은 1회 규칙 등록 단계다. 파일·명령·웹 도구를 사용하지 말고 "
              "LOL_PICK_MEMORY_READY 한 줄만 답해."
        )
        # Always create a clean memory-only base. Reusing an old recommendation
        # thread makes every later turn slower as draft history accumulates.
        return self._run_turn(prompt, "")

    def recommend(self, thread_id: str, prompt: str) -> CodexTurn:
        if not thread_id.strip():
            raise CodexCliError(
                "저장된 thread_id가 없습니다. Riot 설정에서 1회 규칙 보내기를 먼저 누르세요."
            )
        # Resume the memory-only base ephemerally. The recommendation answer is
        # not appended to that base, so the next request stays equally small.
        return self._run_turn(
            prompt, thread_id.strip(), ephemeral=True,
            preserve_thread_id=True,
        )

    def _run_turn(
        self,
        prompt: str,
        thread_id: str,
        *,
        ephemeral: bool = False,
        preserve_thread_id: bool = False,
    ) -> CodexTurn:
        try:
            return self._run_turn_once(
                prompt, thread_id, self.model,
                ephemeral=ephemeral,
                preserve_thread_id=preserve_thread_id,
            )
        except CodexCliError as exc:
            if not self._looks_like_model_error(str(exc)):
                raise
            # Older plans/CLI releases may not expose the preferred fast model.
            # Retrying without -m lets the signed-in account choose its default.
            return self._run_turn_once(
                prompt, thread_id, "",
                ephemeral=ephemeral,
                preserve_thread_id=preserve_thread_id,
            )

    def _run_turn_once(
        self,
        prompt: str,
        thread_id: str,
        model: str,
        *,
        ephemeral: bool = False,
        preserve_thread_id: bool = False,
    ) -> CodexTurn:
        if thread_id:
            args = [
                self.command, "exec", "resume", "--json",
                "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
            ]
        else:
            args = [
                self.command, "exec", "--json", "--color", "never",
                "--sandbox", "read-only", "--ignore-user-config", "--ignore-rules",
                "--skip-git-repo-check", "--cd", str(self.work_dir),
            ]
        if model:
            args.extend(("--model", model))
        if ephemeral:
            args.append("--ephemeral")
        args.extend(("--config", f'model_reasoning_effort="{FAST_REASONING_EFFORT}"'))
        if thread_id:
            args.extend((thread_id, "-"))
        else:
            args.append("-")

        result = self._subprocess(args, prompt)
        if result.returncode:
            raise CodexCliError(self._friendly_error(result.stderr, result.stdout))
        returned_thread_id, message = parse_codex_jsonl(result.stdout, thread_id)
        if preserve_thread_id and thread_id:
            # An ephemeral resume may announce a transient child id. Keep the
            # persistent memory-base id for the next recommendation.
            returned_thread_id = thread_id
        if not returned_thread_id:
            raise CodexCliError("Codex CLI 응답에서 thread_id를 찾지 못했습니다.")
        if not message:
            raise CodexCliError("Codex CLI 응답에서 최종 답변을 찾지 못했습니다.")
        return CodexTurn(
            thread_id=returned_thread_id,
            message=message,
            model=model or "계정 기본 모델",
        )

    def _subprocess(
        self, args: list[str], prompt: str,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        # The integration intentionally uses the saved ChatGPT login, not an API key.
        for key in (
            "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID",
            "OPENAI_PROJECT_ID", "CODEX_API_KEY",
        ):
            environment.pop(key, None)
        try:
            return subprocess.run(
                args,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                cwd=self.work_dir,
                env=environment,
                timeout=self.timeout_seconds,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexCliError(
                f"Codex CLI 응답이 {self.timeout_seconds}초 안에 오지 않았습니다. 다시 시도하세요."
            ) from exc
        except OSError as exc:
            raise CodexCliUnavailable(f"Codex CLI 실행 실패: {exc}") from exc

    @staticmethod
    def _looks_like_model_error(message: str) -> bool:
        lowered = message.casefold()
        return "model" in lowered and any(
            token in lowered
            for token in ("not found", "not supported", "unavailable", "access", "지원")
        )

    @staticmethod
    def _friendly_error(stderr: str, stdout: str) -> str:
        message = (stderr or stdout or "Codex CLI 실행에 실패했습니다.").strip()
        lowered = message.casefold()
        if "not logged in" in lowered or "login required" in lowered:
            return "Codex CLI 로그인이 필요합니다. 터미널에서 codex를 한 번 실행해 로그인하세요."
        if "usage limit" in lowered or "rate limit" in lowered:
            return "현재 ChatGPT 플랜의 Codex 사용 한도에 도달했습니다. 잠시 후 다시 시도하세요."
        if "session" in lowered and any(
            token in lowered for token in ("not found", "does not exist", "unknown")
        ):
            return "저장된 thread_id 대화를 찾지 못했습니다. 1회 규칙 보내기로 새 대화를 만드세요."
        compact = " ".join(message.split())
        return compact[-900:]
