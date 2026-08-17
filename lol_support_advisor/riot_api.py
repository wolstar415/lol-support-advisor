from __future__ import annotations

import json
import time
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .storage import Storage


class RiotApiError(RuntimeError):
    pass


class RiotApiClient:
    def __init__(self, api_key: str, timeout: float = 15.0) -> None:
        self.api_key = api_key.strip()
        self.timeout = timeout

    def _get(self, url: str) -> Any:
        if not self.api_key:
            raise RiotApiError("Riot API 키가 설정되지 않았습니다.")
        while True:
            request = Request(
                url,
                headers={"X-Riot-Token": self.api_key, "User-Agent": "LOL-Support-Advisor/0.1"},
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code == 401 or exc.code == 403:
                    raise RiotApiError("Riot API 키가 만료되었거나 올바르지 않습니다.") from exc
                if exc.code == 429:
                    try:
                        retry_after = int(exc.headers.get("Retry-After", "2"))
                    except (TypeError, ValueError):
                        retry_after = 2
                    time.sleep(max(1, min(retry_after, 120)))
                    continue
                raise RiotApiError(f"Riot API 오류: HTTP {exc.code}") from exc
            except OSError as exc:
                raise RiotApiError(f"Riot API 연결 실패: {exc}") from exc

    def resolve_account(self, game_name: str, tag_line: str) -> dict[str, Any]:
        encoded_name = quote(game_name.strip(), safe="")
        encoded_tag = quote(tag_line.strip(), safe="")
        return self._get(
            f"https://asia.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
            f"{encoded_name}/{encoded_tag}"
        )

    def validate_key_for_account(self, game_name: str, tag_line: str) -> str:
        account = self.resolve_account(game_name, tag_line)
        puuid = str(account.get("puuid") or "").strip()
        if not puuid:
            raise RiotApiError("Riot API 응답에 계정 PUUID가 없어 키를 확인할 수 없습니다.")
        return puuid

    def match_ids(self, puuid: str, count: int = 100) -> list[str]:
        requested = max(1, min(count, 1000))
        ids: list[str] = []
        for start in range(0, requested, 100):
            page_size = min(100, requested - start)
            result = self._get(
                "https://asia.api.riotgames.com/lol/match/v5/matches/by-puuid/"
                f"{quote(puuid, safe='')}/ids?queue=420&start={start}&count={page_size}"
            )
            page = [str(item) for item in result]
            ids.extend(page)
            if len(page) < page_size:
                break
        return ids

    def match(self, match_id: str) -> dict[str, Any]:
        return self._get(
            f"https://asia.api.riotgames.com/lol/match/v5/matches/{quote(match_id, safe='')}"
        )

    def league_entries_by_puuid(
        self, puuid: str, platform: str = "kr"
    ) -> list[dict[str, Any]]:
        result = self._get(
            f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/"
            f"{quote(puuid, safe='')}"
        )
        return list(result)

    def sync_live_profile(
        self,
        storage: Storage,
        game_name: str,
        tag_line: str,
        recent_matches: int = 20,
        platform: str = "kr",
    ) -> tuple[str, dict[str, Any], int]:
        account = self.resolve_account(game_name, tag_line)
        puuid = str(account["puuid"])
        entries = self.league_entries_by_puuid(puuid, platform=platform)
        solo_entry = next(
            (entry for entry in entries if entry.get("queueType") == "RANKED_SOLO_5x5"),
            {},
        )
        ids = self.match_ids(puuid, count=recent_matches)
        known = storage.known_match_ids()
        fetched: list[dict[str, Any]] = []
        for match_id in ids:
            if match_id not in known:
                fetched.append(self.match(match_id))
                known.add(match_id)
        storage.save_matches(fetched)
        payload = {"solo_entry": solo_entry, "recent_match_count": len(ids)}
        storage.save_live_profile(f"{game_name}#{tag_line}", puuid, payload)
        return puuid, payload, len(fetched)

    def sync(
        self,
        storage: Storage,
        game_name: str,
        tag_line: str,
        count: int = 1000,
        progress: Callable[[int, int], None] | None = None,
    ) -> tuple[str, int, int]:
        account = self.resolve_account(game_name, tag_line)
        puuid = str(account["puuid"])
        entries = self.league_entries_by_puuid(puuid, platform="kr")
        solo_entry = next(
            (entry for entry in entries if entry.get("queueType") == "RANKED_SOLO_5x5"),
            {},
        )
        full_history_cached = storage.get_setting("riot_full_history_puuid") == puuid
        ids = self.match_ids(puuid, count=100 if full_history_cached else count)
        known = storage.known_match_ids()
        missing = [match_id for match_id in ids if match_id not in known]
        fetched: list[dict[str, Any]] = []
        saved = 0
        total = len(missing)
        for index, match_id in enumerate(missing, start=1):
            fetched.append(self.match(match_id))
            if len(fetched) >= 20:
                saved += storage.save_matches(fetched)
                fetched.clear()
            if progress:
                progress(index, total)
        if fetched:
            saved += storage.save_matches(fetched)
        storage.set_setting("riot_puuid", puuid)
        storage.set_setting("riot_game_name", game_name.strip())
        storage.set_setting("riot_tag_line", tag_line.strip())
        local_total = storage.count_player_matches(puuid, limit=count)
        storage.save_live_profile(
            f"{game_name.strip()}#{tag_line.strip()}",
            puuid,
            {
                "solo_entry": solo_entry,
                "recent_match_count": local_total,
            },
        )
        if not full_history_cached:
            storage.set_setting("riot_full_history_puuid", puuid)
        return puuid, saved, local_total
