from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


LevelScores = tuple[int, int, int]
LanePhaseScores = tuple[int, int, int, int]


# These values are deliberately coarse.  They describe a champion's usual
# level-one to level-three 2v2 pressure, not a fabricated win-rate.  Unknown
# or off-role picks fall back to neutral and lower the confidence badge.
ADC_EARLY_POWER: dict[str, LevelScores] = {
    "Aphelios": (1, 1, 2), "Ashe": (2, 2, 2),
    "Caitlyn": (3, 3, 3), "Corki": (1, 2, 2),
    "Draven": (3, 3, 3), "Ezreal": (1, 1, 2),
    "Jhin": (2, 2, 2), "Jinx": (1, 1, 2),
    "Kaisa": (1, 2, 2), "Kalista": (3, 3, 3),
    "KogMaw": (1, 1, 2), "Lucian": (2, 3, 3),
    "MissFortune": (2, 3, 3), "Nilah": (1, 2, 3),
    "Samira": (1, 2, 3), "Senna": (2, 2, 2),
    "Seraphine": (2, 2, 2), "Sivir": (2, 2, 2),
    "Smolder": (0, 1, 1), "Swain": (2, 3, 3),
    "Tristana": (1, 3, 3), "Twitch": (1, 2, 2),
    "Varus": (2, 3, 3), "Vayne": (0, 1, 2),
    "Xayah": (1, 2, 3), "Yasuo": (1, 2, 3),
    "Zeri": (1, 1, 2), "Ziggs": (2, 2, 2),
}

SUPPORT_EARLY_POWER: dict[str, LevelScores] = {
    "Alistar": (0, 3, 3), "Bard": (2, 2, 2),
    "Blitzcrank": (2, 3, 3), "Brand": (2, 3, 3),
    "Braum": (3, 3, 3), "Camille": (2, 3, 3),
    "Fiddlesticks": (1, 2, 2), "Galio": (1, 3, 3),
    "Heimerdinger": (3, 3, 3), "Hwei": (2, 2, 3),
    "Janna": (2, 2, 2), "Karma": (3, 3, 3),
    "Leona": (0, 3, 3), "Lulu": (2, 2, 2),
    "Lux": (3, 3, 3), "Maokai": (1, 3, 3),
    "Milio": (2, 2, 2), "Morgana": (2, 2, 2),
    "Nami": (3, 3, 3), "Nautilus": (2, 3, 3),
    "Neeko": (3, 3, 3), "Pantheon": (2, 3, 3),
    "Poppy": (2, 3, 3), "Pyke": (2, 3, 3),
    "Rakan": (1, 3, 3), "Rell": (1, 3, 3),
    "Renata": (2, 2, 3), "Senna": (2, 2, 2),
    "Seraphine": (2, 2, 2), "Shaco": (2, 2, 2),
    "Shen": (2, 3, 3), "Sona": (1, 1, 2),
    "Soraka": (2, 2, 2), "Swain": (2, 3, 3),
    "TahmKench": (2, 2, 3), "Taric": (1, 2, 3),
    "Thresh": (2, 3, 3), "Velkoz": (2, 3, 3),
    "Xerath": (2, 3, 3), "Yuumi": (0, 1, 1),
    "Zilean": (1, 2, 2), "Zyra": (3, 3, 3),
}

ENGAGE_SUPPORTS = {
    "Alistar", "Blitzcrank", "Camille", "Galio", "Leona", "Maokai",
    "Nautilus", "Pantheon", "Poppy", "Pyke", "Rakan", "Rell", "Shen",
    "Thresh",
}
FOLLOW_UP_ADCS = {
    "Draven", "Jhin", "Kaisa", "Kalista", "Lucian", "MissFortune",
    "Nilah", "Samira", "Tristana", "Varus", "Xayah", "Yasuo",
}
POKE_SUPPORTS = {
    "Brand", "Heimerdinger", "Hwei", "Karma", "Lux", "Morgana",
    "Neeko", "Seraphine", "Velkoz", "Xerath", "Zyra",
}
POKE_ADCS = {"Ashe", "Caitlyn", "Ezreal", "Jhin", "MissFortune", "Varus", "Ziggs"}

# Relative level-six impact.  It is added to the level-three baseline so the
# output can say when an early advantage is expected to end or begin.
ADC_LEVEL_SIX_POWER: dict[str, int] = {
    "Aphelios": 2, "Ashe": 2, "Caitlyn": 1, "Corki": 1,
    "Draven": 0, "Ezreal": 1, "Jhin": 1, "Jinx": 1,
    "Kaisa": 2, "Kalista": 1, "KogMaw": 1, "Lucian": 1,
    "MissFortune": 2, "Nilah": 2, "Samira": 2, "Senna": 1,
    "Seraphine": 2, "Sivir": 1, "Smolder": 1, "Swain": 2,
    "Tristana": 1, "Twitch": 2, "Varus": 2, "Vayne": 2,
    "Xayah": 2, "Yasuo": 2, "Zeri": 1, "Ziggs": 2,
}
SUPPORT_LEVEL_SIX_POWER: dict[str, int] = {
    "Alistar": 2, "Bard": 1, "Blitzcrank": 1, "Brand": 2,
    "Braum": 2, "Camille": 2, "Fiddlesticks": 2, "Galio": 2,
    "Heimerdinger": 2, "Hwei": 2, "Janna": 1, "Karma": 1,
    "Leona": 2, "Lulu": 2, "Lux": 2, "Maokai": 2,
    "Milio": 2, "Morgana": 2, "Nami": 2, "Nautilus": 2,
    "Neeko": 2, "Pantheon": 0, "Poppy": 2, "Pyke": 2,
    "Rakan": 2, "Rell": 2, "Renata": 2, "Senna": 1,
    "Seraphine": 2, "Shaco": 2, "Shen": 2, "Sona": 2,
    "Soraka": 1, "Swain": 2, "TahmKench": 2, "Taric": 2,
    "Thresh": 1, "Velkoz": 2, "Xerath": 2, "Yuumi": 2,
    "Zilean": 2, "Zyra": 2,
}


@dataclass(frozen=True, slots=True)
class BottomLaneAnalysis:
    ally_adc_id: str
    ally_support_id: str
    enemy_adc_id: str
    enemy_support_id: str
    level_results: tuple[str, str, str, str]
    level_differences: tuple[int, int, int, int]
    style: str
    timing: str
    confidence: str
    known_champions: int
    laning_edge: float | None = None


def _duo_scores(adc_id: str, support_id: str) -> tuple[LanePhaseScores, int]:
    adc = ADC_EARLY_POWER.get(adc_id)
    support = SUPPORT_EARLY_POWER.get(support_id)
    known = int(adc is not None) + int(support is not None)
    adc = adc or (1, 1, 1)
    support = support or (1, 1, 1)
    values = [adc[index] + support[index] for index in range(3)]
    if support_id in ENGAGE_SUPPORTS and adc_id in FOLLOW_UP_ADCS:
        values[1] += 1
        values[2] += 1
    if support_id in POKE_SUPPORTS and adc_id in POKE_ADCS:
        values[0] += 1
        values[1] += 1
    level_six = (
        values[2]
        + ADC_LEVEL_SIX_POWER.get(adc_id, 1)
        + SUPPORT_LEVEL_SIX_POWER.get(support_id, 1)
    )
    return (values[0], values[1], values[2], level_six), known


def _level_result(difference: int) -> str:
    if difference >= 2:
        return "WIN"
    if difference <= -2:
        return "LOSE"
    return "EVEN"


def _timing_plan(results: tuple[str, str, str, str]) -> str:
    early = results[:3]
    level_six = results[3]
    if all(value == "LOSE" for value in results):
        return "SAFE_ALL"
    if all(value == "EVEN" for value in results):
        return "EVEN_ALL"
    if level_six == "LOSE" and "WIN" in early:
        return "EARLY_THEN_SAFE"
    if level_six == "WIN" and "WIN" not in early:
        return "LEVEL6_TURN"
    if early[0] == "LOSE" and "WIN" in early[1:]:
        return "WAIT_LEVEL2" if early[1] == "WIN" else "WAIT_LEVEL3"
    if "LOSE" not in results and results.count("WIN") >= 2:
        return "PRESS_ALL"
    return "MIXED"


def analyze_bottom_lane(
    ally_adc_id: str,
    ally_support_id: str,
    enemy_adc_id: str,
    enemy_support_id: str,
    *,
    ally_laning_win_rates: Iterable[float | None] = (),
) -> BottomLaneAnalysis:
    ally_scores, ally_known = _duo_scores(ally_adc_id, ally_support_id)
    enemy_scores, enemy_known = _duo_scores(enemy_adc_id, enemy_support_id)
    differences = tuple(
        ally_scores[index] - enemy_scores[index] for index in range(4)
    )
    rates = [float(value) for value in ally_laning_win_rates if value is not None]
    laning_edge = (sum(rates) / len(rates) - 50.0) if rates else None
    weighted_edge = (
        differences[0] * 0.25
        + differences[1] * 0.40
        + differences[2] * 0.35
    )
    # External laning data only breaks a close heuristic result; it never
    # rewrites the individual level-one/two/three labels.
    if abs(weighted_edge) < 1.0 and laning_edge is not None:
        if laning_edge >= 2.5:
            weighted_edge = 1.0
        elif laning_edge <= -2.5:
            weighted_edge = -1.0
    style = (
        "AGGRESSIVE" if weighted_edge >= 1.0
        else "SAFE" if weighted_edge <= -1.0
        else "EVEN"
    )
    known = ally_known + enemy_known
    confidence = (
        "HIGH" if known == 4 and len(rates) >= 2
        else "MEDIUM" if known == 4
        else "LOW"
    )
    level_results = tuple(_level_result(value) for value in differences)
    return BottomLaneAnalysis(
        ally_adc_id=ally_adc_id,
        ally_support_id=ally_support_id,
        enemy_adc_id=enemy_adc_id,
        enemy_support_id=enemy_support_id,
        level_results=level_results,
        level_differences=differences,
        style=style,
        timing=_timing_plan(level_results),
        confidence=confidence,
        known_champions=known,
        laning_edge=laning_edge,
    )
