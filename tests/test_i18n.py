from __future__ import annotations

import unittest
import ast
import re
from pathlib import Path

from lol_support_advisor.i18n import (
    ENGLISH_TEXT, RUNE_NAMES_EN, RUNE_STYLE_NAMES_EN,
    SUMMONER_SPELL_NAMES_EN, UI_STRINGS, localized_text,
    normalize_language, translate_text,
)


class LanguageTests(unittest.TestCase):
    def test_language_setting_is_normalized(self) -> None:
        self.assertEqual(normalize_language("English"), "en")
        self.assertEqual(normalize_language("en-US"), "en")
        self.assertEqual(normalize_language("ko-KR"), "ko")
        self.assertEqual(normalize_language("broken"), "ko")

    def test_main_navigation_and_dynamic_record_use_authored_template(self) -> None:
        self.assertEqual(translate_text("1  선택창", "en"), "1  Draft")
        self.assertEqual(translate_text("3  전적", "en"), "3  Match History")
        self.assertEqual(
            localized_text(
                "history.recent_summary", "en", games=10,
                wins=6, losses=4, rate="60.0%",
            ),
            "Last 10 matches · 6W 4L · 60.0% win rate",
        )

    def test_korean_mode_and_unknown_player_text_are_preserved(self) -> None:
        source = "최근 10경기 · 6승 4패"
        self.assertEqual(translate_text(source, "ko"), source)
        self.assertEqual(translate_text("Faker", "en"), "Faker")

    def test_unknown_dynamic_copy_is_never_partially_translated(self) -> None:
        text = "상대 서포터 계정 분석 · 우리게임#KR1 · 레오나"
        translated = translate_text(text, "en", {"레오나": "Leona"})
        self.assertEqual(translated, text)

    def test_exact_champion_value_can_be_swapped_without_phrase_translation(self) -> None:
        self.assertEqual(
            translate_text("  레오나", "en", {"레오나": "Leona"}),
            "  Leona",
        )

    def test_english_draft_templates_contain_no_korean(self) -> None:
        self.assertEqual(set(UI_STRINGS["ko"]), set(UI_STRINGS["en"]))
        keys = {
            key for key in UI_STRINGS["en"]
            if key.startswith(("header.", "draft.", "hover.", "prompt.", "recommendations."))
        }
        self.assertGreater(len(keys), 40)
        for key in keys:
            with self.subTest(key=key):
                self.assertIsNone(re.search(r"[가-힣]", UI_STRINGS["en"][key]))

    def test_detailed_analysis_tabs_and_table_values_translate(self) -> None:
        expected = {
            "원딜 × 서포터 조합": "ADC × support synergy",
            "OP.GG 포지션 메타": "OP.GG position meta",
            "상대 상성 · 내 전적": "Enemy matchup · my records",
            "플레이 유형": "Playstyle",
            "종합 점수": "Overall score",
            "표본 미제공": "Sample unavailable",
            "추천 가능": "Recommended",
            "OP.GG 상대 상성": "OP.GG matchup",
            "외부 통계 · 현재 상대 기준 후보별 승률과 표본": (
                "External stats · candidate win rate and sample versus the current enemy"
            ),
        }
        for source, translated in expected.items():
            with self.subTest(source=source):
                self.assertEqual(translate_text(source, "en"), translated)

    def test_all_authored_english_templates_are_hangul_free(self) -> None:
        self.assertEqual(set(UI_STRINGS["ko"]), set(UI_STRINGS["en"]))
        for key, value in UI_STRINGS["en"].items():
            with self.subTest(key=key):
                self.assertIsNone(re.search(r"[가-힣]", value))

    def test_settings_play_history_and_build_copy_is_authored_in_english(self) -> None:
        sources = (
            "픽 추천은 명시적으로 허용한 경우에만 선택창에 표시되고 Codex CLI로 전송됩니다. 자동 밴은 아래에서 선택한 챔피언을 사용합니다.",
            "오래된 로컬 데이터는 즉시 보여 주고, 아래 시간이 지난 뒤에만 같은 외부 요청을 다시 보냅니다. (24시간=1일, 재요청 1~720시간 · 메타 1~20개)",
            "DUO: 게임 시작 후 현재 10명의 최근 100경기 교집합을 확인합니다.",
            "LP 변동은 새 솔로랭크부터 정확히 기록합니다.",
            "롤 클라이언트를 실행하면 전체 룬 선택 데이터와 설명을 로컬에 저장합니다.",
        )
        for source in sources:
            with self.subTest(source=source):
                translated = ENGLISH_TEXT[source]
                self.assertIsNone(re.search(r"[가-힣]", translated))
                self.assertNotEqual(translated, source)

    def test_asset_names_use_authored_id_resources(self) -> None:
        self.assertEqual(SUMMONER_SPELL_NAMES_EN[4], "Flash")
        self.assertEqual(RUNE_STYLE_NAMES_EN[8400], "Resolve")
        self.assertEqual(RUNE_NAMES_EN[8465], "Guardian")

    def test_every_static_korean_widget_label_has_an_authored_english_resource(self) -> None:
        source_path = Path(__file__).parents[1] / "lol_support_advisor" / "ui.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        missing: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                value = keyword.value
                if (
                    keyword.arg == "text"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and re.search(r"[가-힣]", value.value)
                    and value.value not in ENGLISH_TEXT
                ):
                    missing.append((node.lineno, value.value))
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
