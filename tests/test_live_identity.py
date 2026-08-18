from __future__ import annotations

from dataclasses import replace
import unittest

from lol_support_advisor.live_identity import (
    live_identity_count, merge_live_roster_identities,
    update_live_identity_payload,
)
from lol_support_advisor.models import LiveGameSnapshot, LivePlayer


def player(
    champion: str,
    name: str,
    tag: str,
    team: str,
    position: str,
    *,
    level: int = 1,
    active: bool = False,
) -> LivePlayer:
    return LivePlayer(
        champion, champion, name, tag, team, position, level, active,
    )


class LiveIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.visible = LiveGameSnapshot(
            players=[
                player("Ornn", "TopPlayer", "KR1", "ORDER", "TOP"),
                player("Leona", "SupportPlayer", "KR2", "CHAOS", "UTILITY"),
            ],
            active_team="ORDER",
            game_mode="CLASSIC",
        )

    def test_redacted_roster_restores_ids_but_keeps_fresh_game_state(self) -> None:
        payload = update_live_identity_payload(self.visible, now=100.0)
        hidden = LiveGameSnapshot(
            players=[
                player(
                    "Ornn", "비공개 ORDER TOP 1", "", "ORDER", "TOP",
                    level=8, active=True,
                ),
                player(
                    "Leona", "비공개 CHAOS UTILITY 2", "", "CHAOS", "UTILITY",
                    level=7,
                ),
            ],
            active_team="ORDER",
            game_time=320.0,
            game_mode="CLASSIC",
        )
        restored = merge_live_roster_identities(hidden, payload, now=101.0)
        self.assertEqual([row.riot_id for row in restored.players], [
            "TopPlayer#KR1", "SupportPlayer#KR2",
        ])
        self.assertEqual(restored.players[0].level, 8)
        self.assertTrue(restored.players[0].is_active_player)
        self.assertEqual(restored.active_riot_id, "TopPlayer#KR1")
        self.assertEqual(live_identity_count(restored), 2)

    def test_different_champion_roster_never_inherits_old_names(self) -> None:
        payload = update_live_identity_payload(self.visible, now=100.0)
        hidden = replace(
            self.visible,
            players=[
                player("Garen", "비공개 ORDER TOP 1", "", "ORDER", "TOP"),
                player("Leona", "비공개 CHAOS UTILITY 2", "", "CHAOS", "UTILITY"),
            ],
        )
        restored = merge_live_roster_identities(hidden, payload, now=101.0)
        self.assertEqual(live_identity_count(restored), 0)

    def test_expired_roster_never_restores_names(self) -> None:
        payload = update_live_identity_payload(self.visible, now=100.0)
        hidden = replace(
            self.visible,
            players=[
                player("Ornn", "비공개 ORDER TOP 1", "", "ORDER", "TOP"),
                player("Leona", "비공개 CHAOS UTILITY 2", "", "CHAOS", "UTILITY"),
            ],
        )
        restored = merge_live_roster_identities(
            hidden, payload, now=200.0, max_age_seconds=10.0,
        )
        self.assertEqual(live_identity_count(restored), 0)

    def test_partial_observations_accumulate_without_redaction_erasing_them(self) -> None:
        partial = replace(
            self.visible,
            players=[
                self.visible.players[0],
                player("Leona", "비공개 CHAOS UTILITY 2", "", "CHAOS", "UTILITY"),
            ],
        )
        first = update_live_identity_payload(partial, now=100.0)
        self.assertEqual(len(first["players"]), 1)  # type: ignore[index]
        second = update_live_identity_payload(self.visible, first, now=101.0)
        self.assertEqual(len(second["players"]), 2)  # type: ignore[index]
        hidden = replace(
            self.visible,
            players=[
                player("Ornn", "비공개 ORDER TOP 1", "", "ORDER", "TOP"),
                player("Leona", "비공개 CHAOS UTILITY 2", "", "CHAOS", "UTILITY"),
            ],
        )
        self.assertIs(update_live_identity_payload(hidden, second, now=102.0), second)


if __name__ == "__main__":
    unittest.main()
