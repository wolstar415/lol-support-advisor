from __future__ import annotations

import json
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .champions import ChampionRegistry
from .models import LiveGameSnapshot, LivePlayer


class LiveClientUnavailable(RuntimeError):
    pass


class LiveClient:
    def __init__(self, registry: ChampionRegistry, timeout: float = 2.0) -> None:
        self.registry = registry
        self.timeout = timeout

    def get(self, path: str) -> Any:
        request = Request(f"https://127.0.0.1:2999{path}")
        context = ssl._create_unverified_context()
        try:
            with urlopen(request, timeout=self.timeout, context=context) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise LiveClientUnavailable("진행 중인 게임 데이터를 아직 읽을 수 없습니다.") from exc

    def snapshot(self) -> LiveGameSnapshot:
        # playerlist is materially smaller than allgamedata and contains every
        # player field this screen needs.
        players_raw = self.get("/liveclientdata/playerlist")
        active_raw = self.get("/liveclientdata/activeplayer")
        game_raw = self.get("/liveclientdata/gamestats")
        active_riot_id = str(active_raw.get("riotId") or active_raw.get("summonerName") or "")
        active_game_name = str(active_raw.get("riotIdGameName") or active_raw.get("summonerName") or "")
        active_tag_line = str(active_raw.get("riotIdTagLine") or "")
        active_team = "ORDER"
        players: list[LivePlayer] = []
        for raw in players_raw:
            game_name = str(raw.get("riotIdGameName") or raw.get("summonerName") or "알 수 없음")
            tag_line = str(raw.get("riotIdTagLine") or "")
            riot_id = str(raw.get("riotId") or "")
            if not tag_line and "#" in riot_id:
                game_name, tag_line = riot_id.rsplit("#", 1)
            champion_id = self.registry.normalize_id(str(raw.get("championName") or "Unknown"))
            player = LivePlayer(
                champion_id=champion_id,
                champion_name_ko=self.registry.ko_name(champion_id),
                riot_game_name=game_name,
                riot_tag_line=tag_line,
                team=str(raw.get("team") or "UNKNOWN"),
                position=str(raw.get("position") or "UNKNOWN"),
                level=int(raw.get("level") or 1),
                is_active_player=bool(riot_id and riot_id == active_riot_id)
                    or bool(
                        game_name and game_name == active_game_name
                        and (not active_tag_line or tag_line == active_tag_line)
                    ),
            )
            if player.is_active_player:
                active_team = player.team
            players.append(player)
        return LiveGameSnapshot(
            players=players,
            active_riot_id=active_riot_id,
            active_team=active_team,
            game_time=float(game_raw.get("gameTime") or 0),
            game_mode=str(game_raw.get("gameMode") or ""),
        )
