"""Message catalogs (en, uk) and small formatting helpers."""

from typing import Literal

Lang = Literal["en", "uk"]
SUPPORTED: tuple[Lang, ...] = ("en", "uk")

_HELP_EN = (
    "<b>Deye inverter monitor</b>\n\n"
    "/status - current status with a power-flow diagram\n"
    "/graph [hours] - chart for the last 1-48 hours (default 24)\n"
    "/threshold [value] - show or set the battery alert level, %\n"
    "/lang [en|uk] - show or set the interface language\n"
    "/help - this message"
)
_HELP_UK = (
    "<b>Моніторинг інвертора Deye</b>\n\n"
    "/status - поточний стан зі схемою потоків потужності\n"
    "/graph [години] - графік за останні 1-48 годин (типово 24)\n"
    "/threshold [значення] - показати або змінити поріг сповіщення про заряд, %\n"
    "/lang [en|uk] - показати або змінити мову інтерфейсу\n"
    "/help - ця довідка"
)

MESSAGES: dict[Lang, dict[str, str]] = {
    "en": {
        "status.title": "Inverter status",
        "status.updated": "Updated {time} ({ago})",
        "status.pv": "Solar",
        "status.load": "Load",
        "status.grid_import": "Grid: importing",
        "status.grid_export": "Grid: exporting",
        "status.grid_idle": "Grid: idle",
        "status.battery": "Battery {soc}%",
        "status.state.charging": "Charging at {power}",
        "status.state.discharging": "Discharging at {power}",
        "status.state.idle": "Idle",
        "status.state.unknown": "State unknown",
        "status.na": "n/a",
        "status.stale_warning": "Data may be outdated: last update {ago}.",
        "alert.drop": (
            "Battery dropped to {soc}% (passed the {level}% level). Current load: {power}."
        ),
        "alert.recovered": "Battery recovered to {soc}%.",
        "alert.stale": "No new data from the inverter, last update {ago}.",
        "alert.back_online": "Data from the inverter is flowing again.",
        "chart.title": "Last {hours} h",
        "chart.power_axis": "Power, kW",
        "chart.soc_axis": "Battery, %",
        "chart.pv": "Solar",
        "chart.load": "Load",
        "chart.battery": "Battery (+ charge)",
        "chart.grid": "Grid (+ import)",
        "chart.threshold": "Alert level",
        "chart.no_data": "No data for this period",
        "chart.summary": (
            "Solar {pv_kwh} kWh, load {load_kwh} kWh\n"
            "Grid: imported {import_kwh} kWh, exported {export_kwh} kWh\n"
            "Battery: charged {charge_kwh} kWh, discharged {discharge_kwh} kWh"
        ),
        "flow.pv": "Solar",
        "flow.grid": "Grid",
        "flow.battery": "Battery",
        "flow.home": "Home",
        "flow.grid_import": "importing",
        "flow.grid_export": "exporting",
        "flow.grid_idle": "idle",
        "flow.state_charging": "charging",
        "flow.state_discharging": "discharging",
        "flow.state_idle": "idle",
        "flow.updated": "{time} · {ago}",
        "flow.stale": "Outdated data: {time} · {ago}",
        "bot.help": _HELP_EN,
        "bot.not_allowed": "Sorry, you are not allowed to use this bot.",
        "bot.admin_only": "This command is for administrators only.",
        "bot.threshold.current": "Alert level: {threshold}%. Alerts repeat every {step}%.",
        "bot.threshold.set": "Alert level set to {threshold}%.",
        "bot.threshold.invalid": "Enter a number from 5 to 100, e.g. /threshold 50",
        "bot.lang.current": "Language: {lang}",
        "bot.lang.set": "Language changed.",
        "bot.lang.invalid": "Supported languages: en, uk. Example: /lang uk",
        "bot.lang.name.en": "English",
        "bot.lang.name.uk": "Українська",
        "bot.error": "Something went wrong. Please try again later.",
        "bot.graph.invalid": "Enter a whole number of hours from 1 to 48, e.g. /graph 12",
        "portal.auth_failed": (
            "The Solarman portal rejected the saved token, so the bot cannot read inverter "
            "data. To get a new one: open a private browser window, log in with the test "
            "account at home.solarmanpv.com, press F12, go to Application, Cookies, and copy "
            "the value of the cookie 442287045fabeaa868450dec4baee7f4. Close the window "
            "without logging out. Then send /token <value> to this bot in a private chat."
        ),
        "token.ok": "Token accepted and saved.",
        "token.failed": (
            "The token was not accepted. Make sure you copied the whole cookie value and try again."
        ),
        "token.private_only": (
            "Send /token only in a private chat with the bot. Your message was deleted; if it "
            "contained a token, treat that token as compromised."
        ),
        "token.usage": "Usage: /token <refresh_token>",
        "token.not_portal": "This command only works with SOLARMAN_BACKEND=portal.",
        "unit.w": "W",
        "unit.kw": "kW",
        "time.ago.min": "{n} minutes ago",
        "time.ago.min.one": "{n} minute ago",
        "time.ago.min.few": "{n} minutes ago",
        "time.ago.h": "{n} hours ago",
        "time.ago.h.one": "{n} hour ago",
        "time.ago.h.few": "{n} hours ago",
        "time.just_now": "just now",
    },
    "uk": {
        "status.title": "Стан інвертора",
        "status.updated": "Оновлено {time} ({ago})",
        "status.pv": "Сонце",
        "status.load": "Споживання",
        "status.grid_import": "Мережа: споживання з мережі",
        "status.grid_export": "Мережа: віддача в мережу",
        "status.grid_idle": "Мережа: без обміну",
        "status.battery": "Батарея {soc}%",
        "status.state.charging": "Заряджається, {power}",
        "status.state.discharging": "Розряджається, {power}",
        "status.state.idle": "Очікування",
        "status.state.unknown": "Стан невідомий",
        "status.na": "н/д",
        "status.stale_warning": "Дані можуть бути застарілими: останнє оновлення {ago}.",
        "alert.drop": (
            "Заряд батареї знизився до {soc}% (пройдено рівень {level}%). "
            "Поточне споживання: {power}."
        ),
        "alert.recovered": "Заряд батареї відновився до {soc}%.",
        "alert.stale": "Немає нових даних від інвертора, останнє оновлення {ago}.",
        "alert.back_online": "Дані від інвертора знову надходять.",
        "chart.title": "Останні {hours} год",
        "chart.power_axis": "Потужність, кВт",
        "chart.soc_axis": "Батарея, %",
        "chart.pv": "Сонце",
        "chart.load": "Споживання",
        "chart.battery": "Батарея (+ заряд)",
        "chart.grid": "Мережа (+ імпорт)",
        "chart.threshold": "Поріг сповіщення",
        "chart.no_data": "Немає даних за цей період",
        "chart.summary": (
            "Сонце {pv_kwh} кВт·год, споживання {load_kwh} кВт·год\n"
            "Мережа: взято {import_kwh} кВт·год, віддано {export_kwh} кВт·год\n"
            "Батарея: заряд {charge_kwh} кВт·год, розряд {discharge_kwh} кВт·год"
        ),
        "flow.pv": "Сонце",
        "flow.grid": "Мережа",
        "flow.battery": "Батарея",
        "flow.home": "Будинок",
        "flow.grid_import": "з мережі",
        "flow.grid_export": "у мережу",
        "flow.grid_idle": "без обміну",
        "flow.state_charging": "заряджається",
        "flow.state_discharging": "розряджається",
        "flow.state_idle": "очікування",
        "flow.updated": "{time} · {ago}",
        "flow.stale": "Дані застаріли: {time} · {ago}",
        "bot.help": _HELP_UK,
        "bot.not_allowed": "Вибачте, у вас немає доступу до цього бота.",
        "bot.admin_only": "Ця команда доступна лише адміністраторам.",
        "bot.threshold.current": "Поріг сповіщення: {threshold}%. Повтор кожні {step}%.",
        "bot.threshold.set": "Поріг сповіщення встановлено: {threshold}%.",
        "bot.threshold.invalid": "Введіть число від 5 до 100, наприклад /threshold 50",
        "bot.lang.current": "Мова: {lang}",
        "bot.lang.set": "Мову змінено.",
        "bot.lang.invalid": "Доступні мови: en, uk. Приклад: /lang uk",
        "bot.lang.name.en": "English",
        "bot.lang.name.uk": "Українська",
        "bot.error": "Щось пішло не так. Спробуйте пізніше.",
        "bot.graph.invalid": "Введіть ціле число годин від 1 до 48, наприклад /graph 12",
        "portal.auth_failed": (
            "Портал Solarman відхилив збережений токен, тому бот не може читати дані інвертора. "
            "Щоб отримати новий: відкрийте приватне вікно браузера, увійдіть тестовим обліковим "
            "записом на home.solarmanpv.com, натисніть F12, перейдіть до Application, Cookies і "
            "скопіюйте значення cookie 442287045fabeaa868450dec4baee7f4. Закрийте вікно, не "
            "виходячи з облікового запису. Потім надішліть боту в приватному чаті: "
            "/token <значення>."
        ),
        "token.ok": "Токен прийнято й збережено.",
        "token.failed": (
            "Токен не прийнято. Переконайтеся, що скопіювали все значення cookie, "
            "і спробуйте ще раз."
        ),
        "token.private_only": (
            "Надсилайте /token лише в приватному чаті з ботом. Повідомлення видалено; "
            "якщо в ньому був токен, вважайте його скомпрометованим."
        ),
        "token.usage": "Використання: /token <токен>",
        "token.not_portal": "Ця команда працює лише з SOLARMAN_BACKEND=portal.",
        "unit.w": "Вт",
        "unit.kw": "кВт",
        "time.ago.min": "{n} хвилин тому",
        "time.ago.min.one": "{n} хвилину тому",
        "time.ago.min.few": "{n} хвилини тому",
        "time.ago.h": "{n} годин тому",
        "time.ago.h.one": "{n} годину тому",
        "time.ago.h.few": "{n} години тому",
        "time.just_now": "щойно",
    },
}


def t(lang: Lang, key: str, **kwargs: object) -> str:
    """Translate `key`; fall back to English, then to the key itself."""
    text = MESSAGES.get(lang, {}).get(key) or MESSAGES["en"].get(key)
    if text is None:
        return key
    return text.format(**kwargs) if kwargs else text


def fmt_num(lang: Lang, value: float, digits: int = 1) -> str:
    """Format a number with a locale decimal separator (comma for Ukrainian)."""
    s = f"{value:.{digits}f}"
    return s.replace(".", ",") if lang == "uk" else s


def fmt_power(lang: Lang, watts: float | None) -> str:
    """850 -> '850 W' / '850 Вт'; 3420 -> '3.42 kW' / '3,42 кВт'."""
    if watts is None:
        return t(lang, "status.na")
    if abs(watts) < 1000:
        return f"{round(watts)} {t(lang, 'unit.w')}"
    return f"{fmt_num(lang, watts / 1000, 2)} {t(lang, 'unit.kw')}"


def _plural_form(lang: Lang, n: int) -> Literal["one", "few", "many"]:
    if lang == "en":
        return "one" if n == 1 else "many"
    if n % 10 == 1 and n % 100 != 11:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


def _ago_key(base: str, form: str) -> str:
    return base if form == "many" else f"{base}.{form}"


def fmt_ago(lang: Lang, seconds: float) -> str:
    """'just now', '5 minutes ago', '3 hours ago' (with correct Ukrainian plurals)."""
    minutes = int(max(seconds, 0) // 60)
    if minutes < 1:
        return t(lang, "time.just_now")
    base, n = ("time.ago.min", minutes) if minutes < 60 else ("time.ago.h", minutes // 60)
    return t(lang, _ago_key(base, _plural_form(lang, n)), n=n)
