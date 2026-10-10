import re
import string

import pytest

from deye_bot.i18n import MESSAGES, SUPPORTED, fmt_ago, fmt_num, fmt_power, t

REQUIRED = [
    "status.title", "status.updated", "status.pv", "status.load", "status.grid_import",
    "status.grid_export", "status.grid_idle", "status.battery", "status.state.charging",
    "status.state.discharging", "status.state.idle", "status.state.unknown", "status.na",
    "status.stale_warning", "alert.drop", "alert.recovered", "alert.stale", "alert.back_online",
    "chart.title", "chart.power_axis", "chart.soc_axis", "chart.pv", "chart.load",
    "chart.battery", "chart.grid", "chart.threshold", "chart.no_data", "chart.summary",
    "bot.help", "bot.not_allowed", "bot.admin_only", "bot.threshold.current",
    "bot.threshold.set", "bot.threshold.invalid", "bot.lang.current", "bot.lang.set",
    "bot.lang.invalid", "bot.lang.name.en", "bot.lang.name.uk", "bot.error",
    "bot.graph.invalid", "unit.w", "unit.kw", "time.ago.min", "time.ago.h", "time.just_now",
]  # fmt: skip


def placeholders(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


def test_key_sets_identical():
    assert set(MESSAGES["en"]) == set(MESSAGES["uk"])


def test_required_keys_present():
    assert set(REQUIRED) <= set(MESSAGES["en"])


@pytest.mark.parametrize("key", sorted(MESSAGES["en"]))
def test_placeholders_match(key):
    assert placeholders(MESSAGES["en"][key]) == placeholders(MESSAGES["uk"][key])


@pytest.mark.parametrize("lang", SUPPORTED)
def test_no_empty_values_and_help_is_html_safe(lang):
    assert all(MESSAGES[lang].values())
    stripped = re.sub(r"</?b>", "", MESSAGES[lang]["bot.help"])
    assert "<" not in stripped and ">" not in stripped
    for cmd in ("/status", "/graph", "/threshold", "/lang", "/help"):
        assert cmd in stripped


def test_t_fallbacks():
    assert t("uk", "no.such.key") == "no.such.key"
    assert t("en", "status.battery", soc=42) == "Battery 42%"
    assert t("uk", "bot.lang.name.uk") == "Українська"


def test_power_formats():
    assert fmt_power("en", 850) == "850 W"
    assert fmt_power("en", 3420) == "3.42 kW"
    assert fmt_power("uk", 850) == "850 Вт"
    assert fmt_power("uk", 3420) == "3,42 кВт"
    assert fmt_power("en", None) == "n/a"
    assert fmt_power("uk", None) == "н/д"
    assert fmt_num("uk", 1.25) == "1,2" or fmt_num("uk", 1.25) == "1,3"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (10, "щойно"),
        (60, "1 хвилину тому"),
        (120, "2 хвилини тому"),
        (300, "5 хвилин тому"),
        (11 * 60, "11 хвилин тому"),
        (12 * 60, "12 хвилин тому"),
        (21 * 60, "21 хвилину тому"),
        (22 * 60, "22 хвилини тому"),
        (3600, "1 годину тому"),
        (2 * 3600, "2 години тому"),
        (5 * 3600, "5 годин тому"),
        (11 * 3600, "11 годин тому"),
        (21 * 3600, "21 годину тому"),
        (24 * 3600, "24 години тому"),
        (30 * 3600, "30 годин тому"),
    ],
)
def test_uk_ago(seconds, expected):
    assert fmt_ago("uk", seconds) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "just now"),
        (60, "1 minute ago"),
        (300, "5 minutes ago"),
        (3600, "1 hour ago"),
        (7300, "2 hours ago"),
    ],
)
def test_en_ago(seconds, expected):
    assert fmt_ago("en", seconds) == expected
