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

    def resolve_account_by_puuid(self, puuid: str) -> dict[str, Any]:
        """Resolve a Riot ID from a PUUID exposed by the local game session."""
        normalized = str(puuid or "").strip()
        if not normalized:
            raise RiotApiError("Riot 계정 PUUID가 비어 있습니다.")
        return self._get(
            "https://asia.api.riotgames.com/riot/account/v1/accounts/by-puuid/"
            f"{quote(normalized, safe='')}"
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

    def match_id_page(
        self, puuid: str, start: int = 0, count: int = 10
    ) -> list[str]:
        """Fetch one solo-ranked Match-v5 ID page for an inspected player.

        This deliberately stays separate from :meth:`match_ids`, whose larger
        paging behavior is reserved for the owner's local history sync.  A
        player-history tab must never turn into a 1,000-game fan-out, so both
        the requested offset and the page size are normalized here and exactly
        one Match-v5 IDs request is made.
        """
        page_start = max(0, int(start))
        page_size = max(1, min(int(count), 10))
        result = self._get(
            "https://asia.api.riotgames.com/lol/match/v5/matches/by-puuid/"
            f"{quote(puuid, safe='')}/ids?queue=420&start={page_start}&count={page_size}"
        )
        return [str(item) for item in list(result)[:page_size]]

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

    def sync_player_match_page(
        self,
        storage: Storage,
        game_name: str,
        tag_line: str,
        start: int = 0,
        count: int = 10,
    ) -> tuple[str, list[str], int, bool]:
        """Resolve an inspected player and cache one 10-game solo page.

        ``has_more`` is intentionally conservative: Riot's IDs endpoint has no
        next cursor, so a full page means another page *may* exist.  An exact
        end boundary is confirmed by the following request returning fewer
        than the requested number of IDs.  Already cached matches are returned
        in page order without being downloaded again.
        """
        requested_size = max(1, min(int(count), 10))
        account = self.resolve_account(game_name, tag_line)
        puuid = str(account.get("puuid") or "").strip()
        if not puuid:
            raise RiotApiError("Riot API 응답에 계정 PUUID가 없습니다.")

        resolved_game_name = str(account.get("gameName") or game_name).strip()
        resolved_tag_line = str(account.get("tagLine") or tag_line).strip()
        riot_id = f"{resolved_game_name}#{resolved_tag_line}"
        storage.save_player_identity(riot_id, puuid)

        ordered_ids = self.match_id_page(
            puuid,
            start=max(0, int(start)),
            count=requested_size,
        )
        known = storage.known_match_ids()
        fetched: list[dict[str, Any]] = []
        for match_id in ordered_ids:
            if match_id in known:
                continue
            fetched.append(self.match(match_id))
            known.add(match_id)
        saved = storage.save_matches(fetched) if fetched else 0
        # Opening the same player again should extend the one-day local
        # retention even when every detail payload was already cached.
        storage.touch_match_cache(ordered_ids)
        # A startup retention pass can race the initial known-ID snapshot.
        # Touching first makes compare-and-delete skip live rows; if pruning
        # already won, re-fetch only the vanished details before publishing a
        # fresh page manifest.
        vanished = [
            match_id for match_id in ordered_ids
            if storage.load_match(match_id) is None
        ]
        if vanished:
            saved += storage.save_matches([
                self.match(match_id) for match_id in vanished
            ])
        has_more = len(ordered_ids) == requested_size
        storage.save_player_match_page(
            riot_id,
            puuid,
            max(0, int(start)),
            ordered_ids,
            has_more,
        )
        return puuid, ordered_ids, saved, has_more

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
        # Keep a tiny owner-only marker so the UI can distinguish "the Riot
        # request completed" from "the just-finished match has actually been
        # published". Match-v5 can lag behind the client by tens of seconds;
        # without this marker a successful zero-result request ended the only
        # post-game refresh attempt.
        storage.set_setting("riot_latest_match_id", ids[0] if ids else "")
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
