"""
Context Agent
=============
Новый агент (26 августа, по прямому запросу Леонида -- "добавить ещё
одного агента, который будет отслеживать это всё [источники, влияющие на
рынок] и так же работать с этой информацией"). Роль уточнена через
AskUserQuestion в тот же день, три ответа:

  1. Что агент делает с информацией -> "Контекст в алертах": короткая
     пометка поверх уже существующего текста алерта, если рядом важное
     макрособытие. Агент НЕ решает, отправлять алерт или нет -- эту логику
     (should_send_level_watch() в dispatch_agent.py) он не трогает вообще
     и вызывается уже ПОСЛЕ того, как решение "отправляем" принято.
  2. Какие источники в первой версии -> "Только календарь": решения по
     ставке/ФРС, макростатистика (CPI/NFP/ВВП), решения ОПЕК+ -- у всех
     есть известные заранее даты, в отличие от горячих новостей и
     геополитики (та часть присланного 26 августа документа про источники,
     влияющие на рынок, сознательно не берётся в v1).
  3. Откуда брать сами данные -> "Поищи и предложи". Сравнивались три
     варианта:
       - Trading Economics -- отпал: настоящего бесплатного тарифа нет,
         только платный невозвратный триал, не подходит для личного
         проекта такого масштаба.
       - Finnhub -- честный бесплатный тариф именно для личных
         некоммерческих проектов, но потребовал бы нового аккаунта, нового
         ключа и нового секрета в .env.
       - FMP (уже используется, план Starter, тот же MARKET_DATA_API_KEY,
         что и у load_fmp_daily в data_agent.py) -- есть отдельный
         эндпоинт /stable/economic-calendar. Леонид проверил вживую на
         сервере 26 августа прямым curl'ом -- вернулся настоящий JSON с
         десятками реальных событий (не ошибка тарифа/ключа). Значит,
         Starter это покрывает -- выбран этот вариант, новый провайдер и
         новый секрет не понадобились.

Источник данных: FMP /stable/economic-calendar. Как и у load_fmp_daily --
requests.get напрямую (не WebFetch), рассчитано на запуск на VPS: сама
песочница Claude до financialmodelingprep.com не достаётся (тот же
allowlist, что и всегда в этом проекте).

ОПЕК+ отдельно от календаря FMP: календарь -- это плановая статистика и
решения центробанков, а даты встреч ОПЕК+ публикуются пресс-релизами
картеля, а не как стандартный статистический релиз. 26 августа проверено
WebSearch (несколько источников, включая специализированный трекер
встреч ОПЕК) -- НИ ОДИН источник на этот момент не публикует подтверждённую
дату следующей встречи после 2 августа 2026. Поэтому _OPEC_PLUS_MEETINGS
ниже начинается ПУСТЫМ списком -- заполнять реальными датами по мере
официальных анонсов, а не гадать (регламент, раздел 2: не выдумывать
данные). См. README, "Открытые вопросы".

Фильтр по событиям -- сознательно узкий список ключевых слов по названию
события (_TRACKED_EVENT_PATTERNS), а не бланковый фильтр по полю
"impact". Календарь FMP глобальный (десятки стран и событий в день), и
одного impact="High"/"Medium" недостаточно -- в реальном ответе от 26
августа с такими impact встречались, например, "Fed Barkin Speech" и
аукционы гособлигаций, которые Леонид явно не называл в числе того, что
хочет отслеживать. Список легко расширить точечно -- см. константу ниже.
Проверено программно (не на глаз), что \bgdp\b НЕ ловит "Atlanta Fed
GDPNow" (это не запланированный релиз, а постоянно обновляемая модель
Атлантского ФРБ -- шум) и ЛОВИТ "GDP Price Index QoQ" -- реальные примеры
из ответа FMP от 26 августа. Core PCE Price Index (тоже реальный пример
из того же ответа, impact=High) НЕ отслеживается -- Леонид называл именно
CPI, а не PCE; добавить -- одна строка в _TRACKED_EVENT_PATTERNS, если
понадобится.

Часовой пояс дат из FMP calendar не подтверждён официальной документацией
(открытый вопрос, см. README) -- в v1 сравнение "рядом ли событие" наивное,
без поправки на TZ. При окне LOOKAHEAD_HOURS_DEFAULT=24 несколько часов
возможной ошибки не критичны для необязательной информационной пометки
(это не торговое решение и не входит в регулируемые регламентом поля
раздела 24), но это не строгая гарантия синхронизации по времени.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# Ключевые слова по названию события, регистронезависимо. Каждый паттерн --
# отдельная категория из решения Леонида ("ФРС, макростатистика CPI/NFP/ВВП,
# ОПЕК+" -- ОПЕК+ обрабатывается отдельно, см. _OPEC_PLUS_MEETINGS ниже).
_TRACKED_EVENT_PATTERNS = [
    re.compile(r"interest rate decision", re.IGNORECASE),
    re.compile(r"\bfomc\b", re.IGNORECASE),
    re.compile(r"\bcpi\b", re.IGNORECASE),
    re.compile(r"non[\s-]?farm payrolls", re.IGNORECASE),
    re.compile(r"\bgdp\b", re.IGNORECASE),  # НЕ ловит "GDPNow" -- см. докстринг файла
]

# Инструменты бота -- USD-номинированные, торгуются в США (см. screener.py,
# INSTRUMENTS) -- поэтому фильтруем календарь именно по стране "US".
_TRACKED_COUNTRY = "US"

LOOKAHEAD_HOURS_DEFAULT = 24.0

# Даты встреч ОПЕК+ -- см. докстринг файла про то, почему список пуст на
# старте (26 августа 2026 подтверждённой даты следующей встречи ещё нет ни
# у одного проверенного источника). Формат -- date(год, месяц, день), дата
# самой встречи (решение обычно публикуется в тот же день).
_OPEC_PLUS_MEETINGS: list[date] = []


@dataclass(frozen=True)
class MacroEvent:
    name: str
    dt: datetime
    country: str | None
    source: str  # "FMP calendar" или "OPEC+ (вручную)"


def _event_is_tracked(name: str) -> bool:
    return any(p.search(name) for p in _TRACKED_EVENT_PATTERNS)


def filter_tracked_events(
    raw_events: list[dict],
    now: datetime,
    lookahead_hours: float = LOOKAHEAD_HOURS_DEFAULT,
    opec_plus_meetings: list[date] | None = None,
) -> list[MacroEvent]:
    """
    Чистая функция без сети -- принимает уже полученный сырой список
    событий FMP (как есть из JSON-ответа /stable/economic-calendar),
    фильтрует по стране, по ключевым словам названия и по окну
    [now, now + lookahead_hours], плюс добавляет встречи ОПЕК+ в то же
    окно. Специально отделена от fetch_fmp_calendar_raw() ниже, чтобы
    тестироваться без сети -- юнит-тесты используют фикстуры, построенные
    по образцу реального ответа FMP от 26 августа (те же имена полей).

    opec_plus_meetings по умолчанию -- модульная константа
    _OPEC_PLUS_MEETINGS; параметр существует ради тестов (тот же приём,
    что и fetch_fn в screener.scan_instrument()), чтобы не подменять
    глобальное состояние модуля.
    """
    if opec_plus_meetings is None:
        opec_plus_meetings = _OPEC_PLUS_MEETINGS

    window_end = now + timedelta(hours=lookahead_hours)
    out: list[MacroEvent] = []

    for row in raw_events:
        if row.get("country") != _TRACKED_COUNTRY:
            continue
        name = row.get("event") or ""
        if not _event_is_tracked(name):
            continue
        try:
            dt = datetime.strptime(row["date"], "%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError):
            continue  # неожиданный формат даты -- пропускаем эту запись, не падаем на всём списке
        if now <= dt <= window_end:
            out.append(MacroEvent(name=name, dt=dt, country=row.get("country"), source="FMP calendar"))

    for meeting_date in opec_plus_meetings:
        dt = datetime.combine(meeting_date, datetime.min.time())
        if now <= dt <= window_end:
            out.append(MacroEvent(name="Встреча ОПЕК+", dt=dt, country=None, source="OPEC+ (вручную)"))

    out.sort(key=lambda e: e.dt)
    return out


def fetch_fmp_calendar_raw(api_key: str, date_from: date, date_to: date) -> list[dict]:
    """
    Тонкая сетевая обёртка -- тот же принцип, что и у load_fmp_daily в
    data_agent.py: requests.get напрямую (не WebFetch), рассчитано на
    запуск на VPS. Эндпоинт /stable/economic-calendar подтверждён вживую
    26 августа реальным curl'ом с MARKET_DATA_API_KEY на плане FMP
    Starter -- вернулся настоящий JSON с событиями, не ошибка тарифа.
    """
    import requests  # локальный импорт: не нужен в офлайн-тестах, только для реальной отправки

    resp = requests.get(
        "https://financialmodelingprep.com/stable/economic-calendar",
        params={"from": date_from.isoformat(), "to": date_to.isoformat(), "apikey": api_key},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list):
        # FMP отдаёт ошибки как {"Error Message": "..."} -- бросаем как есть,
        # НЕ пытаемся угадать/подставить данные вместо ошибки (регламент, раздел 2).
        raise ValueError(f"Неожиданный ответ FMP economic-calendar (ожидался массив): {payload}")
    return payload


def get_upcoming_macro_events(
    now: datetime,
    api_key: str | None = None,
    lookahead_hours: float = LOOKAHEAD_HOURS_DEFAULT,
) -> list[MacroEvent]:
    """
    Точка входа для оркестратора/скринера: реальный сетевой запрос + фильтр
    в одном вызове. api_key по умолчанию берётся из MARKET_DATA_API_KEY --
    тот же ключ, что и у load_fmp_daily, отдельный ключ для этого агента
    не заводим (эндпоинт входит в тот же тариф, см. докстринг файла).
    """
    api_key = api_key or os.environ.get("MARKET_DATA_API_KEY")
    if not api_key:
        raise ValueError("Нет API-ключа: передай api_key или задай MARKET_DATA_API_KEY в окружении")

    date_from = now.date()
    date_to = date_from + timedelta(days=2)  # с запасом покрывает lookahead_hours по умолчанию (24ч)
    raw = fetch_fmp_calendar_raw(api_key, date_from, date_to)
    return filter_tracked_events(raw, now, lookahead_hours)


def build_context_note(events: list[MacroEvent]) -> str | None:
    """
    Короткая строка для добавления К УЖЕ ГОТОВОМУ тексту алерта (см.
    format_message() в dispatch_agent.py) -- НЕ внутрь самого
    format_message(), а отдельной строкой поверх, в orchestrator.py и
    screener.py, чтобы не трогать регулируемые регламентом обязательные
    поля (раздел 24). Возвращает None, если отслеживаемых событий рядом
    нет -- вызывающий код в этом случае просто ничего не добавляет.
    """
    if not events:
        return None
    if len(events) == 1:
        e = events[0]
        return f"⚠️ Рядом важное событие: {e.name} ({e.dt.strftime('%d.%m %H:%M')})"
    items = ", ".join(f"{e.name} ({e.dt.strftime('%d.%m %H:%M')})" for e in events)
    return f"⚠️ Рядом важные события: {items}"


if __name__ == "__main__":
    # Ручная проверка на реальном ключе, если он есть в окружении -- как и у
    # других агентов, реальная сеть только на VPS (см. докстринг файла).
    key = os.environ.get("MARKET_DATA_API_KEY")
    if not key:
        print("MARKET_DATA_API_KEY не задан в окружении -- нечего проверять живьём.")
    else:
        found = get_upcoming_macro_events(datetime.now(), api_key=key)
        if not found:
            print("Отслеживаемых событий в ближайшие 24 часа не найдено.")
        else:
            for e in found:
                print(f"{e.dt} [{e.source}] {e.name}")
        print(f"Строка для алерта: {build_context_note(found)!r}")
