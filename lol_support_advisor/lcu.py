from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .champions import ChampionRegistry
from .models import DraftBan, DraftMember, DraftSnapshot


class LcuUnavailable(RuntimeError):
    pass


class LcuActionError(LcuUnavailable):
    """A safe, user-actionable failure returned before an LCU write."""


class LcuActionStateChanged(LcuActionError):
    """The champ-select phase or the local player's action changed mid-check."""


@dataclass(frozen=True, slots=True)
class LcuChampionActionResult:
    action_type: str
    action_id: int
    champion_key: int
    completed: bool


def session_actions(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        action
        for group in data.get("actions", []) or []
        for action in (group or [])
        if isinstance(action, dict)
    ]


def find_local_champion_action(
    data: dict[str, Any], action_type: str, *, require_in_progress: bool = True,
) -> dict[str, Any]:
    """Find only the local player's unfinished pick/ban action.

    Never falling back to another cell is important because the action endpoint
    accepts an action id directly and otherwise makes it easy to target a team
    mate's action by mistake.
    """
    local_cell = int(data.get("localPlayerCellId", -1))
    wanted = action_type.strip().lower()
    candidates = [
        action for action in session_actions(data)
        if int(action.get("actorCellId", -999)) == local_cell
        and str(action.get("type") or "").lower() == wanted
        and not bool(action.get("completed"))
    ]
    if not candidates:
        label = "픽" if wanted == "pick" else "밴"
        raise LcuActionStateChanged(f"현재 내 {label} 작업을 찾지 못했습니다.")
    in_progress = next(
        (action for action in candidates if bool(action.get("isInProgress"))), None
    )
    if require_in_progress and in_progress is None:
        label = "픽" if wanted == "pick" else "밴"
        raise LcuActionStateChanged(f"아직 내 {label} 차례가 아닙니다.")
    action = in_progress or candidates[0]
    if int(action.get("id") or 0) <= 0:
        raise LcuActionStateChanged(
            "롤 클라이언트의 선택 작업 ID를 확인하지 못했습니다."
        )
    return action


def champion_action_in_progress(data: dict[str, Any], action_type: str) -> bool:
    try:
        find_local_champion_action(data, action_type, require_in_progress=True)
    except LcuActionError:
        return False
    return True


def champ_select_time_left_ms(data: dict[str, Any]) -> int | None:
    """Return the current champ-select phase countdown when LCU exposes it."""
    timer = data.get("timer") or {}
    if not isinstance(timer, dict) or bool(timer.get("isInfinite")):
        return None
    for key in ("adjustedTimeLeftInPhase", "timeLeftInPhase"):
        value = timer.get(key)
        try:
            milliseconds = int(float(value))
        except (TypeError, ValueError):
            continue
        if milliseconds >= 0:
            return milliseconds
    return None


def champ_select_timer_phase(data: dict[str, Any]) -> str:
    """Return the normalized inner champ-select phase (for example BAN_PICK)."""
    timer = data.get("timer") or {}
    if not isinstance(timer, dict):
        return ""
    return str(timer.get("phase") or "").strip().upper()


def deferred_ban_due(
    data: dict[str, Any], target_remaining_ms: int,
    fallback_deadline: float, now_monotonic: float,
) -> bool:
    """Use Riot's countdown, falling back only when that timer is unavailable."""
    remaining_ms = champ_select_time_left_ms(data)
    if remaining_ms is not None:
        return remaining_ms <= max(0, int(target_remaining_ms))
    return now_monotonic >= fallback_deadline


def _locked_champion_ids(data: dict[str, Any], action_type: str) -> set[int]:
    wanted = action_type.strip().lower()
    return {
        int(action.get("championId") or 0)
        for action in session_actions(data)
        if str(action.get("type") or "").lower() == wanted
        and bool(action.get("completed"))
        and int(action.get("championId") or 0) > 0
    }


def session_banned_champion_ids(data: dict[str, Any]) -> set[int]:
    bans = data.get("bans", {}) or {}
    summary = [
        *(bans.get("myTeamBans", []) or []),
        *(bans.get("theirTeamBans", []) or []),
    ]
    return {
        *(_locked_champion_ids(data, "ban")),
        *(int(value or 0) for value in summary if int(value or 0) > 0),
    }


class LcuClient:
    def __init__(self, configured_lockfile: str = "", timeout: float = 2.0) -> None:
        self.configured_lockfile = configured_lockfile
        self.timeout = timeout
        self._write_lock = threading.Lock()
        self._credentials_lock = threading.Lock()
        self._cached_credentials: tuple[int, str] | None = None
        self._credentials_failure_until = 0.0

    def _credentials_from_lockfile(self) -> tuple[int, str] | None:
        candidates: list[Path] = []
        if self.configured_lockfile:
            candidates.append(Path(self.configured_lockfile))
        for drive in ("C", "D", "E", "F", "G"):
            candidates.extend(
                [
                    Path(f"{drive}:/Riot Games/League of Legends/lockfile"),
                    Path(f"{drive}:/Games/Riot Games/League of Legends/lockfile"),
                    Path(f"{drive}:/Program Files/Riot Games/League of Legends/lockfile"),
                ]
            )
        for path in candidates:
            try:
                raw = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            parts = raw.split(":")
            if len(parts) >= 5:
                try:
                    port = int(parts[2])
                except (TypeError, ValueError):
                    continue
                token = str(parts[3]).strip()
                if port > 0 and token:
                    return port, token
        return None

    def _credentials_from_process(self) -> tuple[int, str] | None:
        if os.name != "nt":
            return None
        command = (
            "$p=Get-CimInstance Win32_Process -Filter \"Name='LeagueClientUx.exe'\" "
            "| Select-Object -First 1 -ExpandProperty CommandLine; if($p){$p}"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        port_match = re.search(r"--app-port=(\d+)", result.stdout)
        token_match = re.search(r"--remoting-auth-token=([^\s\"]+)", result.stdout)
        if not port_match or not token_match:
            return None
        return int(port_match.group(1)), token_match.group(1)

    def _credentials(self) -> tuple[int, str]:
        # Credential discovery can fall back to starting PowerShell and querying
        # LeagueClientUx.exe.  Cache the result so every fast champ-select poll
        # does not pay that process-discovery cost.  Holding the lock through
        # discovery also prevents concurrent background jobs from doing the same
        # expensive lookup in parallel.
        with self._credentials_lock:
            if self._cached_credentials is not None:
                return self._cached_credentials
            if time.monotonic() < self._credentials_failure_until:
                raise LcuUnavailable("롤 클라이언트를 찾을 수 없습니다.")
            credentials = (
                self._credentials_from_lockfile()
                or self._credentials_from_process()
            )
            if not credentials:
                # Waiting callers reuse this short negative result instead of
                # each launching their own three-second PowerShell discovery.
                self._credentials_failure_until = time.monotonic() + 0.75
                raise LcuUnavailable("롤 클라이언트를 찾을 수 없습니다.")
            self._cached_credentials = credentials
            self._credentials_failure_until = 0.0
            return credentials

    def _invalidate_credentials(
        self, credentials: tuple[int, str] | None = None,
    ) -> None:
        """Forget failed credentials without clearing a newer concurrent value."""
        with self._credentials_lock:
            if (
                credentials is None
                or self._cached_credentials == credentials
            ):
                self._cached_credentials = None
                self._credentials_failure_until = 0.0

    def request(self, method: str, path: str, payload: Any | None = None) -> Any:
        credentials = self._credentials()
        port, token = credentials
        authorization = base64.b64encode(f"riot:{token}".encode("utf-8")).decode("ascii")
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"https://127.0.0.1:{port}{path}",
            data=body,
            headers={
                "Authorization": f"Basic {authorization}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method.upper(),
        )
        context = ssl._create_unverified_context()  # LCU uses a local self-signed certificate.
        try:
            with urlopen(request, timeout=self.timeout, context=context) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else None
        except HTTPError as exc:
            if exc.code in {401, 403}:
                self._invalidate_credentials(credentials)
            if exc.code == 404 and path == "/lol-champ-select/v1/session":
                raise LcuUnavailable("현재 챔피언 선택 화면이 아닙니다.") from exc
            detail = ""
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
                detail = str(error_payload.get("message") or error_payload.get("errorCode") or "")
            except (ValueError, OSError, AttributeError):
                pass
            suffix = f" · {detail}" if detail else ""
            raise LcuUnavailable(
                f"롤 클라이언트 응답 오류: HTTP {exc.code}{suffix}"
            ) from exc
        except (URLError, OSError) as exc:
            self._invalidate_credentials(credentials)
            raise LcuUnavailable(f"롤 클라이언트 연결 실패: {exc}") from exc
        except ValueError as exc:
            raise LcuUnavailable(f"롤 클라이언트 연결 실패: {exc}") from exc

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, payload: Any) -> Any:
        return self.request("POST", path, payload)

    def put(self, path: str, payload: Any) -> Any:
        return self.request("PUT", path, payload)

    def patch(self, path: str, payload: Any) -> Any:
        return self.request("PATCH", path, payload)

    def champ_select_session(self) -> dict[str, Any]:
        result = self.get("/lol-champ-select/v1/session")
        if not isinstance(result, dict):
            raise LcuUnavailable("챔피언 선택 데이터를 읽지 못했습니다.")
        return result

    def accept_ready_check_if_pending(self) -> bool:
        ready_check = self.get("/lol-matchmaking/v1/ready-check")
        if not isinstance(ready_check, dict):
            raise LcuActionError("게임 수락 상태를 읽지 못했습니다.")
        state = str(ready_check.get("state") or "").lower()
        response = str(ready_check.get("playerResponse") or "").lower()
        if state not in {"inprogress", "in_progress"} or response not in {"", "none"}:
            return False
        self.request("POST", "/lol-matchmaking/v1/ready-check/accept")
        return True

    @staticmethod
    def _id_set(payload: Any, failure_message: str) -> set[int]:
        if not isinstance(payload, list):
            raise LcuActionError(failure_message)
        result: set[int] = set()
        for value in payload:
            try:
                numeric = int(value)
            except (TypeError, ValueError):
                continue
            if numeric > 0:
                result.add(numeric)
        return result

    def _owned_champion_ids(self) -> set[int]:
        payload = self.get("/lol-champions/v1/owned-champions-minimal")
        if not isinstance(payload, list):
            raise LcuActionError("보유 챔피언 목록을 확인하지 못했습니다.")
        owned: set[int] = set()
        for champion in payload:
            if not isinstance(champion, dict):
                continue
            ownership = champion.get("ownership", {}) or {}
            if bool(ownership.get("owned")):
                try:
                    champion_key = int(champion.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if champion_key > 0:
                    owned.add(champion_key)
        return owned

    def perform_champion_action(
        self, champion_key: int, action: str, *,
        expected_action_id: int | None = None,
        pre_commit_check: Callable[[], bool] | None = None,
    ) -> LcuChampionActionResult:
        with self._write_lock:
            if pre_commit_check is not None and not pre_commit_check():
                raise LcuActionStateChanged(
                    "예약된 챔피언 작업이 취소되었습니다."
                )
            return self._perform_champion_action(
                champion_key,
                action,
                expected_action_id=expected_action_id,
                pre_commit_check=pre_commit_check,
            )

    def _perform_champion_action(
        self, champion_key: int, action: str, *,
        expected_action_id: int | None = None,
        pre_commit_check: Callable[[], bool] | None = None,
    ) -> LcuChampionActionResult:
        """Preflight and perform an explicit local HOVER, pick, or ban action."""
        try:
            champion_key = int(champion_key)
        except (TypeError, ValueError) as exc:
            raise LcuActionError("챔피언 ID가 올바르지 않습니다.") from exc
        if champion_key <= 0:
            raise LcuActionError("챔피언 ID가 올바르지 않습니다.")
        normalized = action.strip().lower()
        if normalized not in {"hover", "pick", "ban"}:
            raise LcuActionError("지원하지 않는 챔피언 선택 작업입니다.")

        phase = str(self.get("/lol-gameflow/v1/gameflow-phase"))
        if phase != "ChampSelect":
            raise LcuActionError("현재 챔피언 선택 화면이 아닙니다.")
        session = self.champ_select_session()
        action_type = "ban" if normalized == "ban" else "pick"
        if action_type == "ban" and champ_select_timer_phase(session) != "BAN_PICK":
            raise LcuActionStateChanged(
                "아직 실제 밴 단계가 아닙니다. 밴 단계 진입을 기다립니다."
            )
        local_action = find_local_champion_action(
            session, action_type, require_in_progress=True
        )
        action_id = int(local_action["id"])
        if (
            expected_action_id is not None
            and action_id != int(expected_action_id)
        ):
            raise LcuActionStateChanged(
                "내 밴 작업이 변경되어 새 작업을 다시 확인합니다."
            )

        banned_ids = session_banned_champion_ids(session)
        selected_ids = _locked_champion_ids(session, "pick")
        if champion_key in banned_ids:
            raise LcuActionError("이미 밴된 챔피언입니다.")
        if champion_key in selected_ids:
            raise LcuActionError("이미 다른 플레이어가 선택한 챔피언입니다.")

        if action_type == "pick":
            if champion_key not in self._owned_champion_ids():
                raise LcuActionError("보유하지 않은 챔피언이라 선택할 수 없습니다.")
            pickable = self._id_set(
                self.get("/lol-champ-select/v1/pickable-champion-ids"),
                "현재 선택 가능한 챔피언 목록을 확인하지 못했습니다.",
            )
            if champion_key not in pickable:
                raise LcuActionError("현재 이 챔피언을 선택할 수 없습니다.")
        else:
            bannable = self._id_set(
                self.get("/lol-champ-select/v1/bannable-champion-ids"),
                "현재 밴 가능한 챔피언 목록을 확인하지 못했습니다.",
            )
            if champion_key not in bannable:
                raise LcuActionError("현재 이 챔피언을 밴할 수 없습니다.")

        # The automatic-ban toggle can be turned off while the LCU preflight
        # requests above are in flight.  Recheck immediately before the only
        # state-changing request so an already-cancelled monitor can never
        # commit a late PATCH.
        if pre_commit_check is not None and not pre_commit_check():
            raise LcuActionStateChanged(
                "예약된 챔피언 작업이 취소되었습니다."
            )
        completed = normalized in {"pick", "ban"}
        self.patch(
            f"/lol-champ-select/v1/session/actions/{action_id}",
            {"championId": champion_key, "completed": completed},
        )
        return LcuChampionActionResult(
            action_type=normalized,
            action_id=action_id,
            champion_key=champion_key,
            completed=completed,
        )


ROLE_MAP = {
    "top": "TOP",
    "jungle": "JUNGLE",
    "middle": "MIDDLE",
    "mid": "MIDDLE",
    "bottom": "BOTTOM",
    "utility": "SUPPORT",
    "support": "SUPPORT",
    "": "UNKNOWN",
}


def parse_lcu_session(data: dict[str, Any], registry: ChampionRegistry) -> DraftSnapshot:
    local_cell = int(data.get("localPlayerCellId", -1))
    my_team_raw = data.get("myTeam", []) or []
    their_team_raw = data.get("theirTeam", []) or []
    my_cells = {int(member.get("cellId", -999)) for member in my_team_raw}
    their_cells = {int(member.get("cellId", -999)) for member in their_team_raw}
    local_member = next(
        (member for member in my_team_raw if int(member.get("cellId", -999)) == local_cell),
        {},
    )
    my_role = ROLE_MAP.get(
        str(local_member.get("assignedPosition", "")).lower(), "UNKNOWN"
    )
    # Riot can briefly leave assignedPosition empty while champ select opens.
    # Preserve the app's original support-first behavior only for that short gap.
    if my_role == "UNKNOWN":
        my_role = "SUPPORT"

    completed: dict[int, int] = {}
    hovered: dict[int, int] = {}
    pick_orders: dict[str, dict[int, int]] = {"ally": {}, "enemy": {}}
    pick_turn_by_cell: dict[int, int] = {}
    ban_orders: dict[str, dict[int, int]] = {"ally": {}, "enemy": {}}
    ban_by_actor: dict[str, dict[int, DraftBan]] = {"ally": {}, "enemy": {}}
    local_action_state = "WAITING"
    global_pick_turn = 0

    for action_group in data.get("actions", []) or []:
        group = list(action_group or [])
        if any(str(action.get("type") or "").lower() == "pick" for action in group):
            global_pick_turn += 1
        for action in group:
            actor = int(action.get("actorCellId", -1))
            champion_key = int(action.get("championId") or 0)
            action_type = str(action.get("type") or "").lower()
            side = "ally" if actor in my_cells else ("enemy" if actor in their_cells else "")
            if action_type == "pick":
                if side and actor not in pick_orders[side]:
                    pick_orders[side][actor] = len(pick_orders[side]) + 1
                if side and actor not in pick_turn_by_cell:
                    pick_turn_by_cell[actor] = global_pick_turn
                if bool(action.get("completed")) and champion_key:
                    completed[actor] = champion_key
                    hovered.pop(actor, None)
                elif champion_key:
                    hovered[actor] = champion_key
                if actor == local_cell:
                    if bool(action.get("completed")):
                        local_action_state = "LOCKED"
                    elif bool(action.get("isInProgress")):
                        local_action_state = "SELECTING"
            elif action_type == "ban" and side:
                if actor not in ban_orders[side]:
                    ban_orders[side][actor] = len(ban_orders[side]) + 1
                champion_id, name = registry.from_key(champion_key) if champion_key else ("", "")
                state = (
                    "LOCKED" if bool(action.get("completed")) and champion_key else
                    "HOVER" if champion_key else "EMPTY"
                )
                ban_by_actor[side][actor] = DraftBan(
                    champion_id=champion_id,
                    champion_name_ko=name,
                    state=state,
                    actor_cell_id=actor,
                    order=ban_orders[side][actor],
                )

    ally_locked: list[DraftMember] = []
    ally_hover: list[DraftMember] = []
    my_hover: DraftMember | None = None

    def team_members(raw_team: list[dict[str, Any]], side: str) -> list[DraftMember]:
        result: list[tuple[int, DraftMember]] = []
        for raw_index, raw in enumerate(raw_team):
            cell = int(raw.get("cellId", -1))
            role = ROLE_MAP.get(str(raw.get("assignedPosition", "")).lower(), "UNKNOWN")
            champion_key = 0
            state = "EMPTY"
            if cell in completed:
                champion_key, state = completed[cell], "LOCKED"
            elif cell in hovered:
                champion_key, state = hovered[cell], "HOVER"
            else:
                fallback_key = int(raw.get("championId") or 0)
                if fallback_key:
                    champion_key, state = fallback_key, "LOCKED"
                else:
                    # During the short PLANNING/DECLARE step the client keeps
                    # a player's declared champion here instead of in the pick
                    # action.  Ignoring it made the local HOVER card stay in
                    # its waiting state even though the intent was visible in
                    # the League client.
                    intent_key = int(raw.get("championPickIntent") or 0)
                    if intent_key:
                        champion_key, state = intent_key, "HOVER"
            champion_id, name = registry.from_key(champion_key) if champion_key else ("", "")
            member = DraftMember(
                champion_id, name, role, state, cell,
                pick_orders[side].get(cell),
                pick_turn_by_cell.get(cell),
            )
            # Keep Riot's team ordering as the fallback when future pick actions
            # are briefly absent while a champ-select session is opening.
            result.append((raw_index, member))
        result.sort(key=lambda item: (
            item[1].pick_order is None,
            item[1].pick_order if item[1].pick_order is not None else item[0],
            item[0],
        ))
        return [member for _raw_index, member in result]

    ally_team_order = team_members(my_team_raw, "ally")
    enemy_team_order = team_members(their_team_raw, "enemy")
    for member in ally_team_order:
        if member.state == "LOCKED":
            ally_locked.append(member)
        elif member.state == "HOVER":
            if member.cell_id == local_cell:
                my_hover = member
            else:
                ally_hover.append(member)
    enemy_locked = [member for member in enemy_team_order if member.state == "LOCKED"]
    if my_hover and local_action_state == "WAITING":
        local_action_state = "SELECTING"

    active_pick_order_swap = next(
        (
            item for item in (data.get("pickOrderSwaps", []) or [])
            if str(item.get("state") or "").strip().upper()
            in {"SENT", "RECEIVED", "ACCEPTED"}
        ),
        None,
    )
    pick_order_swap_state = (
        str(active_pick_order_swap.get("state") or "").strip().upper()
        if active_pick_order_swap else ""
    )
    pick_order_swap_target_cell_id = (
        int(active_pick_order_swap.get("cellId", -1))
        if active_pick_order_swap else None
    )

    bans = data.get("bans", {}) or {}

    def merged_bans(side: str, summary_keys: list[Any]) -> list[DraftBan]:
        result = sorted(
            ban_by_actor[side].values(),
            key=lambda item: item.order if item.order is not None else 99,
        )
        for summary_index, raw_key in enumerate(summary_keys):
            key = int(raw_key or 0)
            if not key:
                continue
            champion_id, name = registry.from_key(key)
            if any(
                item.state == "LOCKED" and item.champion_id == champion_id
                for item in result
            ):
                continue
            replacement = next(
                (
                    item for item in result
                    if item.order == summary_index + 1 and item.state == "EMPTY"
                ),
                None,
            )
            if replacement:
                replacement.champion_id = champion_id
                replacement.champion_name_ko = name
                replacement.state = "LOCKED"
            else:
                result.append(DraftBan(
                    champion_id, name, "LOCKED", None, summary_index + 1
                ))
        return sorted(result, key=lambda item: item.order if item.order is not None else 99)

    ally_ban_actions = merged_bans("ally", list(bans.get("myTeamBans", []) or []))
    enemy_ban_actions = merged_bans("enemy", list(bans.get("theirTeamBans", []) or []))
    ally_bans = [item.champion_id for item in ally_ban_actions if item.state == "LOCKED"]
    enemy_bans = [item.champion_id for item in enemy_ban_actions if item.state == "LOCKED"]

    snapshot = DraftSnapshot(
        my_role=my_role,
        my_pick_order=pick_orders["ally"].get(local_cell),
        my_status=local_action_state,
        ally_locked=ally_locked,
        ally_hover=ally_hover,
        enemy_locked=enemy_locked,
        my_hover=my_hover,
        ally_team_order=ally_team_order,
        enemy_team_order=enemy_team_order,
        ally_bans=ally_bans,
        enemy_bans=enemy_bans,
        ally_ban_actions=ally_ban_actions,
        enemy_ban_actions=enemy_ban_actions,
        local_player_cell_id=local_cell,
        connection_state="CHAMP_SELECT",
        pick_order_swap_state=pick_order_swap_state,
        pick_order_swap_target_cell_id=pick_order_swap_target_cell_id,
    )
    snapshot.refresh_snapshot_id()
    return snapshot
