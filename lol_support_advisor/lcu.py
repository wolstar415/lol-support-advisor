from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .champions import ChampionRegistry
from .models import DraftMember, DraftSnapshot


class LcuUnavailable(RuntimeError):
    pass


class LcuClient:
    def __init__(self, configured_lockfile: str = "", timeout: float = 2.0) -> None:
        self.configured_lockfile = configured_lockfile
        self.timeout = timeout

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
                return int(parts[2]), parts[3]
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
        credentials = self._credentials_from_lockfile() or self._credentials_from_process()
        if not credentials:
            raise LcuUnavailable("롤 클라이언트를 찾을 수 없습니다.")
        return credentials

    def get(self, path: str) -> Any:
        port, token = self._credentials()
        authorization = base64.b64encode(f"riot:{token}".encode("utf-8")).decode("ascii")
        request = Request(
            f"https://127.0.0.1:{port}{path}",
            headers={"Authorization": f"Basic {authorization}"},
        )
        context = ssl._create_unverified_context()  # LCU uses a local self-signed certificate.
        try:
            with urlopen(request, timeout=self.timeout, context=context) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 404 and path == "/lol-champ-select/v1/session":
                raise LcuUnavailable("현재 챔피언 선택 화면이 아닙니다.") from exc
            raise LcuUnavailable(f"롤 클라이언트 응답 오류: HTTP {exc.code}") from exc
        except (URLError, OSError, ValueError) as exc:
            raise LcuUnavailable(f"롤 클라이언트 연결 실패: {exc}") from exc

    def champ_select_session(self) -> dict[str, Any]:
        result = self.get("/lol-champ-select/v1/session")
        if not isinstance(result, dict):
            raise LcuUnavailable("챔피언 선택 데이터를 읽지 못했습니다.")
        return result


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
    pick_order_by_cell: dict[int, int] = {}
    team_pick_ordinal = 0
    local_action_state = "WAITING"

    for action_group in data.get("actions", []) or []:
        for action in action_group or []:
            if str(action.get("type")) != "pick":
                continue
            actor = int(action.get("actorCellId", -1))
            champion_key = int(action.get("championId") or 0)
            if actor in my_cells and actor not in pick_order_by_cell:
                team_pick_ordinal += 1
                pick_order_by_cell[actor] = team_pick_ordinal
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

    ally_locked: list[DraftMember] = []
    ally_hover: list[DraftMember] = []
    my_hover: DraftMember | None = None

    for raw in my_team_raw:
        cell = int(raw.get("cellId", -1))
        role = ROLE_MAP.get(str(raw.get("assignedPosition", "")).lower(), "UNKNOWN")
        if cell in completed:
            champion_id, name = registry.from_key(completed[cell])
            ally_locked.append(DraftMember(champion_id, name, role, "LOCKED", cell))
        elif cell in hovered:
            champion_id, name = registry.from_key(hovered[cell])
            member = DraftMember(champion_id, name, role, "HOVER", cell)
            if cell == local_cell:
                my_hover = member
            else:
                ally_hover.append(member)

    enemy_locked: list[DraftMember] = []
    for raw in their_team_raw:
        champion_key = int(raw.get("championId") or 0)
        if not champion_key:
            continue
        champion_id, name = registry.from_key(champion_key)
        role = ROLE_MAP.get(str(raw.get("assignedPosition", "")).lower(), "UNKNOWN")
        enemy_locked.append(
            DraftMember(champion_id, name, role, "LOCKED", int(raw.get("cellId", -1)))
        )

    bans = data.get("bans", {}) or {}
    ally_bans = [registry.from_key(key)[0] for key in bans.get("myTeamBans", []) if int(key or 0)]
    enemy_bans = [registry.from_key(key)[0] for key in bans.get("theirTeamBans", []) if int(key or 0)]

    snapshot = DraftSnapshot(
        my_role=my_role,
        my_pick_order=pick_order_by_cell.get(local_cell),
        my_status=local_action_state,
        ally_locked=ally_locked,
        ally_hover=ally_hover,
        enemy_locked=enemy_locked,
        my_hover=my_hover,
        ally_bans=ally_bans,
        enemy_bans=enemy_bans,
        local_player_cell_id=local_cell,
        connection_state="CHAMP_SELECT",
    )
    snapshot.refresh_snapshot_id()
    return snapshot
