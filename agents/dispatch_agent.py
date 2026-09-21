"""
Dispatch Agent (финальный агент)
==================================
Решает, что отправлять и как -- по прямому запросу Леонида ("финальный
агент, который решал, что отправляется и как это правильно отправлять").
Две обязанности:
  1. Собрать сообщение по формату раздела 24 регламента.
  2. Разослать его списку получателей.

Токен бота получен и подтверждён (23 августа). Реальная отправка через
Bot API реализована в send_via_telegram() ниже -- но она может реально
сработать только там, где есть обычный интернет (GitHub Actions runner,
VPS): сама песочница Claude не достаёт напрямую до api.telegram.org
(проверено curl'ом -- 403 на уровне прокси allowlist'а). Поэтому
send_via_telegram(bot_token=None) остаётся дефолтным режимом (DRY RUN,
печатает, что было бы отправлено) везде, кроме продакшен-запуска, где
TELEGRAM_BOT_TOKEN приходит из переменных окружения / секретов, не из
кода.
"""
from __future__ import annotations

import html as _html
import math
import re
from dataclasses import dataclass, field

from agents.edge_stats import format_edge_line
from agents.fibo_agent import FiboStructure
from agents.price_behavior_agent import NearestLevelInfo, RecentEvent
from agents.risk_agent import build_risk_plan, format_risk_line
from agents.verification_agent import ChecklistReport

# Эмодзи-иконка по направлению структуры -- используется в format_message()
# для визуального заголовка (25 августа, по запросу Леонида "чтобы это не
# выглядело просто как слова и цифры"). Ключи -- буквальные значения
# Direction.value из agents/fibo_agent.py (см. там же).
_DIRECTION_EMOJI = {
    "восходящий": "📈",
    "нисходящий": "📉",
}


def _esc(value: object) -> str:
    """HTML-экранирование для parse_mode='HTML' в send_via_telegram --
    защита на случай, если в тикер/источник/заметку когда-нибудь попадёт
    '<', '>' или '&'. Без этого несбалансированный/неэкранированный символ
    может сломать парсинг на стороне Telegram и алерт не дойдёт вообще
    (см. send_via_telegram про fallback на обычный текст на этот случай)."""
    return _html.escape(str(value))


# Исследование 21 сентября 2026 (research/) считало только структуры с >= 500
# свечей истории -- см. research_engine.WARMUP. Ниже этого порога базовые
# частоты из agents/edge_stats.py не показываем.
MIN_HISTORY_BARS_FOR_EDGE = 500


def _fmt_price(x: float) -> str:
    """Цена для сообщения. Для цен >= 1 -- как раньше, 2 знака (ничего не
    меняется для акций/индексов). Для цен < 1 -- столько знаков, чтобы
    осталось 4 значащие цифры: раньше 1000PEPEUSDT и другие мелкие
    крипто-контракты печатались как "0.00" ("закрытие за 0.00" -- ориентир
    инвалидации без единой значимой цифры), найдено 21 сентября 2026 при
    прогоне реального пайплайна на кэше крипто-данных."""
    a = abs(x)
    if a >= 1 or a == 0:
        return f"{x:.2f}"
    decimals = min(10, max(2, 3 - math.floor(math.log10(a))))
    return f"{x:.{decimals}f}"


def _depth_bar(fraction: float, width: int = 12) -> str:
    """Текстовая полоска глубины коррекции юникод-блоками: 0% -- пусто
    (точка 2), 100% -- полностью залито (точка 1). Значение зажимается в
    [0, 1] только для рисования самой полоски -- реальная доля рядом в
    подписи (frac*100) остаётся точной, даже если цена ушла за пределы
    исходного диапазона (fraction < 0 или > 1)."""
    clamped = min(1.0, max(0.0, fraction))
    filled = round(clamped * width)
    return "▰" * filled + "▱" * (width - filled)


def _strip_html(text: str) -> str:
    """Аварийный fallback в обычный текст без разметки -- если Telegram не
    смог распарсить HTML (см. send_via_telegram), сигнал должен всё равно
    дойти, пусть и некрасиво, а не потеряться молча (регламент, раздел 2)."""
    return _html.unescape(re.sub(r"<[^>]+>", "", text))


@dataclass(frozen=True)
class Recipient:
    label: str  # для наших внутренних логов, например "Леонид"
    telegram_chat_id: str | None = None  # заполняется, когда участник напишет боту


@dataclass(frozen=True)
class AnalysisBundle:
    symbol: str
    source_tag: str
    timeframe: str
    period_desc: str
    structure: FiboStructure
    nearest: NearestLevelInfo
    recent_events: list[RecentEvent]
    checklist: ChecklistReport
    # 21 сентября 2026 (риск-блок, см. agents/risk_agent.py и agents/edge_stats.py).
    # Оба поля необязательные и стоят В КОНЦЕ -- все существующие вызовы
    # AnalysisBundle(...) (orchestrator, mtf_screener, тесты) продолжают работать
    # без изменений; без ATR строка риска просто не помечает "стоп внутри шума".
    atr14: float | None = None
    history_bars: int | None = None  # сколько свечей истории лежит под структурой; None -- неизвестно
    asset_kind: str = "stock"  # "crypto" -- отдельный класс базовых частот (см. edge_stats.py)


@dataclass(frozen=True)
class LevelWatchState:
    """Память между запусками для should_send_level_watch() ниже -- какой
    самый глубокий уровень уже был разослан для КАКОЙ структуры. Хранится и
    читается/пишется снаружи (оркестратором, в output/alert_state.json) --
    сам этот модуль остаётся без файлового I/O, как и раньше."""

    structure_id: str | None = None
    last_alerted_level: float | None = None


def format_message(
    bundle: AnalysisBundle,
    alert_level: float | None = None,
    display_name: str | None = None,
    consensus_note: str | None = None,
    intraday_note: str | None = None,
) -> str:
    """
    Формат раздела 24 регламента: инструмент, источник, таймфрейм, период,
    тип ФИБО, структура, точка 1, точка 2, основные уровни, текущая цена,
    положение цены -- все эти поля по-прежнему присутствуют, регламент
    задаёт СОДЕРЖАНИЕ, не форму (раздел 25 тут не задействован).

    Облегчённый редизайн от 20 сентября 2026, по прямому запросу Леонида
    ("не так развёрнуто, а облегчённо... чтобы это был помощник") --
    убрана полная моноширинная таблица всех уровней (0..текущий): она
    дублировала то, что и так нарисовано на прикреплённом графике
    (render_chart рисует те же линии уровней визуально) -- в тексте
    оставлена только ОДНА строка с уровнем, который реально сейчас важен
    (тестируемый / между какими двумя). Также убран блок "недавние реакции
    на уровни" (до 5 пунктов с примечаниями) -- историческая, не срочная
    информация, отвлекающая от главного в push-уведомлении; полная картина
    по-прежнему в консольном логе сервера (см. price_behavior_agent.py),
    просто больше не дублируется в каждом сообщении получателю.

    Уровни-расширения (1.414 и далее, см. правку 27 августа "уровни-
    расширения на сообщении") — по-прежнему показываются, но только когда
    реально нужны (цена ушла за исходный диапазон), одной строкой с
    ближайшей ещё не достигнутой целью, а не всей таблицей вперёд.

    Второй проход облегчения (21 сентября 2026, "можно ещё облегчить"):
    когда сработал alert_level, заголовок УЖЕ называет достигнутый уровень
    ("коррекция дошла до 0.618") -- строка "между 0.618 и 0.786, ближе к
    0.618" в этом случае просто повторяла то же самое другими словами.
    Теперь при алерте вместо неё -- одна короткая строка "следующий
    уровень", а если следующего нет (уровень 1.0 и дальше без расширения)
    -- строка вообще не показывается. Полная "между X и Y" формулировка
    сохранена для НЕЙТРАЛЬНОЙ сводки (alert_level не задан) -- там это
    единственное место, где вообще видно положение цены.

    Риск-блок (21 сентября 2026, итог исследования на ~650 инструментах, см.
    research/): две короткие строки -- "📐" (стоп/цель/R:R/объём позиции при
    риске 1% депозита, agents/risk_agent.py) и "📊" (как такие сигналы
    исторически заканчивались по сравнению со случайным входом,
    agents/edge_stats.py). Вторая строка -- только у настоящего алерта и
    честно говорит, если сетап НЕ лучше случайного входа (продажа отскока,
    крипта) -- сообщение не должно выглядеть увереннее, чем позволяют данные.

    alert_level -- если задан, это реальный триггер level-watch (не просто
    информационный дамп), заголовок оформляется как алерт ("коррекция
    дошла до X"), а не как нейтральная сводка.

    display_name -- человеко-читаемое имя инструмента (например "Meta"),
    если отличается от тикера в bundle.symbol (например "META") -- нужно
    скринеру, чтобы показывать оба сразу. Для точечного IBM-watch не
    передаётся -- там имя и тикер и так совпадают.

    consensus_note -- готовая строка от format_consensus_note()
    (verification_agent.py), добавляется отдельной строкой, если передана.
    С 20 сентября 2026 format_consensus_note() сама возвращает None в
    скучном случае "всё сошлось" (см. её докстринг) -- то есть строка
    здесь появляется, только когда есть что-то РЕАЛЬНО заметное
    (расхождение или недоступность сверки), а не как рутинное
    подтверждение на каждом сообщении.

    intraday_note -- готовая строка от build_intraday_note()
    (agents/intraday_agent.py), тот же принцип, что и у consensus_note --
    необязательная, добавляется своей строкой, если передана.
    """
    s = bundle.structure
    n = bundle.nearest
    frac = retracement_fraction(bundle)
    dir_emoji = _DIRECTION_EMOJI.get(s.direction.value, "🔹")
    symbol_esc = _esc(bundle.symbol)
    title = f"{_esc(display_name)} ({symbol_esc})" if display_name and display_name != bundle.symbol else symbol_esc
    lines: list[str] = []
    if alert_level is not None:
        lines.append(f"🔔 <b>{title}</b> — коррекция дошла до {alert_level:g}")
    else:
        lines.append(f"{dir_emoji} <b>{title}</b>")
    lines.append(f"📊 {_esc(bundle.source_tag)} · {_esc(bundle.timeframe)} · {_esc(bundle.period_desc)}")
    lines.append(
        f"{dir_emoji} {s.direction.value}, {s.scope.value}: "
        f"<b>{_fmt_price(s.point1.price)}</b> ({s.point1.dt}) → <b>{_fmt_price(s.point2.price)}</b> ({s.point2.dt})"
    )
    # 27 августа (по отзыву "брокера", см. project doc): ориентир инвалидации
    # -- закрытие ЗА точку 1 отменяет гипотезу "коррекция исчерпалась, дальше
    # снова движение в сторону точки 2". Сформулировано как гипотеза, а не
    # команда "стоп тут": бот не даёт торговых указаний, только структурный
    # ориентир. Текст короче, чем раньше, но фраза "Ориентир инвалидации" и
    # сама цена точки 1 -- намеренно на месте (см. test_format_message_shows_invalidation_price).
    lines.append(f"⚠️ Ориентир инвалидации: закрытие за <b>{_fmt_price(s.point1.price)}</b> (точка 1)")
    lines.append(f"Глубина коррекции: <b>{frac * 100:.1f}%</b> {_depth_bar(frac)}")
    # Уровни-расширения (27 августа) -- одна строка с ближайшей ещё не
    # достигнутой целью, только когда цена реально ушла за исходный
    # диапазон (frac > 1.0). В обычном диапазоне ничего не показываем --
    # см. докстринг выше про то, зачем убрана полная таблица.
    extension_levels_sorted = sorted(lv.level for lv in s.levels if lv.level > 1.0)
    showed_extension = frac > 1.0 and bool(extension_levels_sorted)
    if showed_extension:
        next_target = next((lv for lv in extension_levels_sorted if lv >= frac), extension_levels_sorted[-1])
        target_price = next(lv.price for lv in s.levels if lv.level == next_target)
        lines.append(f"🎯 Цена ушла за исходный диапазон — следующая цель {next_target:g} ({_fmt_price(target_price)})")
    lines.append(f"💰 Текущая цена: <b>{_fmt_price(n.current_price)}</b>")
    if alert_level is not None:
        # Заголовок уже назвал достигнутый уровень -- не повторяем его тут
        # же другими словами, только подсказываем, что дальше (если есть
        # куда, и это ещё не показано строкой расширения выше).
        # "Глубже" -- это БОЛЬШИЙ НОМЕР уровня, а не "выше по цене": у
        # восходящей структуры (точка 1 = LOW) более глубокий уровень лежит
        # НИЖЕ по цене. Раньше здесь стоял n.above_level (выше по цене) --
        # для восходящих структур он показывал уже ПРОЙДЕННЫЙ уровень
        # (найдено 21 сентября 2026 на реальных данных: 1810.HK, "следующий
        # уровень 0.618" при глубине 0.637). Уровень 1.0 не показываем --
        # это сама точка 1, она уже стоит строкой "Ориентир инвалидации".
        if not showed_extension:
            deeper = sorted((lv for lv in s.levels if frac + 1e-9 < lv.level < 1.0), key=lambda lv: lv.level)
            if deeper:
                lines.append(f"➡️ Следующий уровень: {deeper[0].level:g} ({_fmt_price(deeper[0].price)})")
    elif n.is_testing:
        lines.append(f"🎯 Тестирует уровень {n.nearest_level:g} ({_fmt_price(n.nearest_price)})")
    else:
        below = f"{n.below_level:g} ({_fmt_price(n.below_price)})" if n.below_level is not None else "—"
        above = f"{n.above_level:g} ({_fmt_price(n.above_price)})" if n.above_level is not None else "—"
        lines.append(f"Между {below} и {above}, ближе к {n.nearest_level:g} ({_fmt_price(n.nearest_price)})")
    # Риск-блок (21 сентября 2026): R:R, расстояние до стопа, объём при риске
    # 1% депозита. План строится, только когда цена между точками 1 и 2 (см.
    # risk_agent.build_risk_plan) -- иначе строки нет, а не выдуманные числа.
    plan = build_risk_plan(n.current_price, s.point1.price, s.point2.price, atr=bundle.atr14)
    if plan is not None:
        lines.append(format_risk_line(plan))
    # Историческая калибровка ожиданий -- только для настоящего алерта (там
    # известен уровень), не для нейтральной сводки. См. agents/edge_stats.py:
    # цифры измерены на ~650 инструментах и сравнены со случайным входом.
    # Базовые частоты измерены ТОЛЬКО на дневных структурах ("1D") -- для
    # недельных/месячных/внутридневных (mtf_screener.py, intraday) они не
    # применимы, и показывать их там было бы обманом. Риск-блок выше остаётся:
    # это чистая геометрия, от таймфрейма не зависит.
    if alert_level is not None and bundle.timeframe == "1D":
        sign = 1 if s.point2.price > s.point1.price else -1
        if bundle.history_bars is not None and bundle.history_bars < MIN_HISTORY_BARS_FOR_EDGE:
            # Статистика измерена на структурах минимум с 500 свечами истории
            # (~2 года) -- на короткой истории (свежее IPO) она неприменима, и
            # показывать цифры как будто применима было бы обманом.
            lines.append(
                f"⚠️ История всего {bundle.history_bars} свечей — структура ненадёжна, "
                f"базовая статистика сетапа здесь неприменима"
            )
        else:
            edge_line = format_edge_line(bundle.asset_kind, sign, alert_level)
            if edge_line:
                lines.append(_esc(edge_line))
    if consensus_note:
        lines.append(_esc(consensus_note))
    if intraday_note:
        lines.append(_esc(intraday_note))
    return "\n".join(lines)


_LOCAL_GRID_EMOJI = {
    "формирование": "🌱",
    "зафиксирована": "🔒",
    "завершена": "🏁",
}


def format_local_grid_message(symbol: str, global_grid, local, display_name: str | None = None) -> str:
    """
    Сообщение по одной локальной сетке (agents/local_grid_agent.py, коммит
    cbb2f71 — построена по PDF-спецификации Леонида, отдельная от боевого
    fibo_agent.py датамодель) — новый, явно помеченный тип алерта (14
    сентября 2026, часть broad_screener.py). Не путать с format_message()
    выше: та берёт FiboStructure/AnalysisBundle боевой глобальной сетки, эта
    -- GlobalGrid/LocalGrid из local_grid_agent.py. Стиль оформления (HTML,
    эмодзи-заголовок, моноширинная таблица уровней) намеренно тот же, что и
    у format_message() -- единообразие в чате получателя, не новый
    визуальный язык ради одной фичи.

    Какое именно событие это сообщение представляет ("сформирована"/
    "зафиксирована"/"завершена") определяется тем, что за LocalGrid сюда
    передал вызывающий код (broad_screener.py решает это через дедуп по
    (symbol, seq, local.state) -- здесь только форматирование уже готового
    объекта, без какой-либо памяти между вызовами).

    Облегчённый редизайн 20-21 сентября 2026 (см. format_message() выше
    про общий запрос "не так развёрнуто") -- точки 1/2 сведены в одну
    строку, пустые строки-разделители убраны, пояснение механики
    подтверждения ("двусвечное закрепление за локальными 50%") убрано --
    сама дата подтверждения важнее, чем напоминание как именно она
    получена. Таблица уровней сознательно ОСТАВЛЕНА (в отличие от
    format_message()) -- у локальных сеток нет прикреплённого графика
    (send_via_telegram, не send_photo_via_telegram), так что текст —
    единственное место, где эти числа вообще видны.
    """
    symbol_esc = _esc(symbol)
    title = f"{_esc(display_name)} ({symbol_esc})" if display_name and display_name != symbol else symbol_esc
    emoji = _LOCAL_GRID_EMOJI.get(local.state.value, "🔹")
    lines: list[str] = [
        f"{emoji} <b>{title}</b> — Локальная сетка №{local.seq} [{local.direction.value}]: {local.state.value}",
        f"🌐 Глобальная ({global_grid.direction.value}): "
        f"{_fmt_price(global_grid.point1.price)} ({global_grid.point1.dt}) → {_fmt_price(global_grid.point2.price)} ({global_grid.point2.dt})",
    ]
    if local.point2_final is not None:
        lines.append(
            f"Точка 1 (100%): <b>{_fmt_price(local.point1.price)}</b> ({local.point1.kind}, {local.point1.dt}) → "
            f"Точка 2 (0%): <b>{_fmt_price(local.point2_final.price)}</b> ({local.point2_final.kind}, {local.point2_final.dt})"
        )
        lines.append(f"Подтверждена: {local.confirmed_date}")
    else:
        p = local.point2_preliminary
        lines.append(
            f"Точка 1 (100%): <b>{_fmt_price(local.point1.price)}</b> ({local.point1.kind}, {local.point1.dt}) → "
            f"Точка 2 (0%, ПРЕДВАРИТЕЛЬНАЯ): <b>{_fmt_price(p.price)}</b> ({p.kind}, {p.dt})"
        )
    levels = local.levels()
    if levels:
        level_lines = [f"{r:>5.3f} = {_fmt_price(price):>12}" for r, price in sorted(levels.items())]
        lines.append("<pre>" + _esc("\n".join(level_lines)) + "</pre>")
    if local.exit_date is not None:
        lines.append(f"🚪 Выход: {local.exit_date}, через границу {local.exit_border}, цена {_fmt_price(local.exit_price)}")
        note = "продолжение в том же направлении (0%)" if local.exit_border == "0%" else "разворот (100%)"
        lines.append(f"<i>{note}</i>")
    return "\n".join(lines)


def should_send(bundle: AnalysisBundle, mode: str = "always") -> tuple[bool, str]:
    """
    Финальное решение "достаточно ли значимо, чтобы отправлять".
    mode="always"       -- как явно попросил Леонид 23 августа: слать всегда,
                            каждый цикл, независимо от того, изменилось ли что-то.
    mode="on_significant" -- альтернативный режим (изначально рекомендованный
                            Claude'ом) -- слать только при тесте/пробое/ретесте
                            и т.п. Оставлен в коде на случай, если решат
                            переключиться, если fixed-каденс окажется слишком шумным.
    """
    if not bundle.checklist.all_passed:
        failed = ", ".join(r.name for r in bundle.checklist.failed())
        return False, f"Заблокировано verification-агентом, не прошли проверки: {failed}"
    if mode == "always":
        return True, "Режим 'always' (выбран Леонидом 23 августа) -- отправляем каждый цикл."
    if mode == "on_significant":
        if bundle.nearest.is_testing or bundle.recent_events:
            return True, "Есть тест уровня или недавние события -- отправляем."
        return False, "Ничего значимого не произошло, режим on_significant -- пропускаем цикл."
    raise ValueError(f"Неизвестный режим: {mode}")


def retracement_fraction(bundle: AnalysisBundle) -> float:
    """
    Доля отката от точки 2 (0%) к точке 1 (100%) -- то есть ровно то же
    число, что и уровень ФИБО, на котором сейчас находится цена, но взятое
    как непрерывная величина, а не привязка к конкретной сетке уровней
    (0.236/0.382/...). Работает одинаково для восходящей и нисходящей
    структуры: знаменатель (point1.price - point2.price) сам меняет знак
    в зависимости от направления, так что "глубже коррекция" всегда
    означает "ближе к point1", независимо от того, вверх это или вниз.
    """
    s = bundle.structure
    return (bundle.nearest.current_price - s.point2.price) / (s.point1.price - s.point2.price)


def should_send_level_watch(
    bundle: AnalysisBundle,
    state: LevelWatchState,
    watch_levels: tuple[float, ...] = (0.618, 0.786, 1.0),
) -> tuple[bool, str, LevelWatchState]:
    """
    Альтернатива should_send(mode=...), по прямому запросу Леонида
    24 августа: не слать каждый цикл и не слать на каждый мелкий тест
    уровня (0.236/0.382 уже видели, не интересно) -- молчать, пока цена не
    дойдёт до значимого уровня глубокой коррекции. По умолчанию это 0.618
    и глубже (0.618, 0.786, 1.0) -- ровно то, что Леонид назвал примером
    ("более глубокая коррекция").

    Не шлёт повторно на каждый цикл, пока цена топчется у одного и того же
    уже разосланного уровня -- LevelWatchState.last_alerted_level помнит
    самый глубокий уровень, уже отправленный ДЛЯ ЭТОЙ ЖЕ структуры
    (structure_id = даты точки1/точки2 + направление). Если сформируется
    новый глобальный свинг (новый HIGH/LOW) -- structure_id изменится, и
    память сбрасывается сама собой, без явного сброса.
    """
    if not bundle.checklist.all_passed:
        failed = ", ".join(r.name for r in bundle.checklist.failed())
        return False, f"Заблокировано verification-агентом: {failed}", state
    s = bundle.structure
    structure_id = f"{s.point1.dt}|{s.point2.dt}|{s.direction.value}"
    frac = retracement_fraction(bundle)
    reached = [lv for lv in watch_levels if frac >= lv]
    if not reached:
        return (
            False,
            f"Коррекция ещё не дошла до {min(watch_levels):g} (сейчас {frac:.3f}) -- ждём",
            state,
        )
    deepest = max(reached)
    same_structure = state.structure_id == structure_id
    if same_structure and state.last_alerted_level is not None and deepest <= state.last_alerted_level:
        return (
            False,
            f"Уровень {deepest:g} для этой структуры уже был в алерте (последний был {state.last_alerted_level:g}) -- пропускаем",
            state,
        )
    new_state = LevelWatchState(structure_id=structure_id, last_alerted_level=deepest)
    return True, f"Коррекция дошла до {deepest:g} (глубина {frac:.3f}) -- отправляем алерт", new_state


def send_via_telegram(
    message: str,
    recipients: list[Recipient],
    bot_token: str | None,
    parse_mode: str | None = "HTML",
) -> dict:
    """
    bot_token is None -> DRY RUN (печатает, что было бы отправлено, ничего
    реально не уходит). Это режим по умолчанию везде, кроме продакшен-запуска
    (GitHub Actions / VPS), где TELEGRAM_BOT_TOKEN приходит из секретов.

    bot_token задан -> реальная отправка через Bot API. Из песочницы Claude
    сама сеть до api.telegram.org не доходит (проверено, 403 на уровне
    прокси) -- эта ветка физически может отработать только там, где есть
    обычный интернет (GitHub Actions runner, VPS). Каждый получатель
    отправляется независимо и с честным статусом (успех/ошибка/исключение)
    для каждого -- ни одна неудача не маскируется под успех.

    parse_mode="HTML" (по умолчанию, с 25 августа) -- format_message() теперь
    собирает сообщение с HTML-тегами (жирный, курсив, моноширинный блок).
    Если Telegram НЕ смог распарсить разметку (например, где-то закралась
    несбалансированная/неэкранированная сущность) -- это ошибка 400, и
    молча терять реальный торговый сигнал из-за красоты недопустимо
    (регламент, раздел 2: честно, не имитировать и не терять результат).
    Поэтому именно на 400 при заданном parse_mode делается ОДНА попытка
    аварийной отправки тем же текстом, но без разметки (_strip_html) и без
    parse_mode -- получатель в худшем случае увидит некрасивый текст с
    HTML-тегами внутри, но сигнал дойдёт. На тестах (test_pipeline.py)
    проверяется, что теги в format_message() всегда сбалансированы, так что
    этот fallback -- именно аварийная сетка, а не ожидаемый путь.
    """
    result = {"dry_run": bot_token is None, "sent_to": [], "message_preview": message}
    for r in recipients:
        if bot_token is None or r.telegram_chat_id is None:
            result["sent_to"].append({"recipient": r.label, "status": "SKIPPED (нет токена или chat_id)"})
            continue
        import requests  # локальный импорт: не нужен в dry-run/тестах, только для реальной отправки

        payload = {"chat_id": r.telegram_chat_id, "text": message}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json=payload,
                timeout=15,
            )
            data = resp.json() if resp.content else {}
            if resp.status_code == 200 and data.get("ok"):
                result["sent_to"].append({"recipient": r.label, "status": "sent"})
            elif parse_mode and resp.status_code == 400:
                fallback_resp = requests.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": r.telegram_chat_id, "text": _strip_html(message)},
                    timeout=15,
                )
                fb_data = fallback_resp.json() if fallback_resp.content else {}
                if fallback_resp.status_code == 200 and fb_data.get("ok"):
                    result["sent_to"].append(
                        {
                            "recipient": r.label,
                            "status": "sent (HTML не распарсился -- ушло обычным текстом, см. fallback)",
                        }
                    )
                else:
                    result["sent_to"].append(
                        {"recipient": r.label, "status": f"ERROR {resp.status_code}: {str(data)[:200]}"}
                    )
            else:
                result["sent_to"].append(
                    {"recipient": r.label, "status": f"ERROR {resp.status_code}: {str(data)[:200]}"}
                )
        except Exception as e:  # сеть недоступна, таймаут и т.п. -- фиксируем как есть, не молчим
            result["sent_to"].append({"recipient": r.label, "status": f"EXCEPTION: {e}"})
    return result


def send_photo_via_telegram(
    photo_bytes: bytes,
    recipients: list[Recipient],
    bot_token: str | None,
    caption: str | None = None,
    filename: str = "chart.png",
) -> dict:
    """
    Отправка PNG-графика (agents/chart_agent.py -- render_chart()) через Bot
    API sendPhoto -- multipart/form-data, НЕ JSON (в отличие от
    send_via_telegram выше). Та же логика dry-run / честного статуса на
    каждого получателя, что и у send_via_telegram:

    bot_token is None -> DRY RUN (ничего реально не уходит, только
    фиксируем размер картинки и то, что было бы отправлено). Как и у
    send_via_telegram, это дефолт везде, кроме продакшен-запуска.

    caption -- необязательный. У sendPhoto лимит подписи 1024 символа --
    заметно меньше, чем 4096 у обычного текста, поэтому сюда НЕ передаётся
    format_message() целиком. По решению Леонида (26 августа: "Фото + весь
    текущий текст") полное сообщение уходит ОТДЕЛЬНО, сразу следом, через
    send_via_telegram() -- caption здесь в лучшем случае короткая
    строка-заголовок, а не замена текстового сообщения. Подпись отправляется
    как простой текст, без parse_mode -- специально, чтобы не тащить сюда
    же риск несбалансированной HTML-разметки ради короткой строки (тот
    fallback имеет смысл для длинного форматированного текста, не здесь).

    Как и у send_via_telegram: каждый получатель обрабатывается независимо,
    ни одна неудача не маскируется под успех ни для кого другого.
    """
    result = {"dry_run": bot_token is None, "sent_to": [], "photo_bytes": len(photo_bytes)}
    for r in recipients:
        if bot_token is None or r.telegram_chat_id is None:
            result["sent_to"].append({"recipient": r.label, "status": "SKIPPED (нет токена или chat_id)"})
            continue
        import requests  # локальный импорт: не нужен в dry-run/тестах, только для реальной отправки

        data = {"chat_id": r.telegram_chat_id}
        if caption:
            data["caption"] = caption
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendPhoto",
                data=data,
                files={"photo": (filename, photo_bytes, "image/png")},
                timeout=30,
            )
            payload = resp.json() if resp.content else {}
            if resp.status_code == 200 and payload.get("ok"):
                result["sent_to"].append({"recipient": r.label, "status": "sent"})
            else:
                result["sent_to"].append(
                    {"recipient": r.label, "status": f"ERROR {resp.status_code}: {str(payload)[:200]}"}
                )
        except Exception as e:  # сеть недоступна, таймаут и т.п. -- фиксируем как есть, не молчим
            result["sent_to"].append({"recipient": r.label, "status": f"EXCEPTION: {e}"})
    return result


def send_document_via_telegram(
    document_bytes: bytes,
    recipients: list[Recipient],
    bot_token: str | None,
    caption: str | None = None,
    filename: str = "report.txt",
) -> dict:
    """
    Отправка текстового файла-вложения через Bot API sendDocument --
    добавлено 5 сентября 2026, по прямому запросу Леонида ("хочу, чтобы
    это объединилось в одно сообщение"): analyst_report.py/
    opportunity_scanner.py при большом числе карточек (десятки
    инструментов) физически не помещаются в лимит sendMessage (4096
    символов, см. build_digest_messages в analyst_report.py) даже после
    разбиения на много сообщений -- то же самое содержимое, но ОДНИМ
    сообщением с вложением, без обрезания текста и без разбиения на части.

    caption -- как и у send_photo_via_telegram, у sendDocument лимит
    подписи 1024 символа -- сюда идёт короткая сводка (например,
    "N возможностей"), а не сам разбор -- разбор целиком в файле.

    bot_token is None -> DRY RUN, тот же принцип, что и везде в этом
    модуле. Каждый получатель обрабатывается независимо, ни одна неудача
    не маскируется под успех ни для кого другого.
    """
    result = {"dry_run": bot_token is None, "sent_to": [], "document_bytes": len(document_bytes)}
    for r in recipients:
        if bot_token is None or r.telegram_chat_id is None:
            result["sent_to"].append({"recipient": r.label, "status": "SKIPPED (нет токена или chat_id)"})
            continue
        import requests  # локальный импорт: не нужен в dry-run/тестах, только для реальной отправки

        data = {"chat_id": r.telegram_chat_id}
        if caption:
            data["caption"] = caption
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendDocument",
                data=data,
                files={"document": (filename, document_bytes, "text/plain")},
                timeout=30,
            )
            payload = resp.json() if resp.content else {}
            if resp.status_code == 200 and payload.get("ok"):
                result["sent_to"].append({"recipient": r.label, "status": "sent"})
            else:
                result["sent_to"].append(
                    {"recipient": r.label, "status": f"ERROR {resp.status_code}: {str(payload)[:200]}"}
                )
        except Exception as e:  # сеть недоступна, таймаут и т.п. -- фиксируем как есть, не молчим
            result["sent_to"].append({"recipient": r.label, "status": f"EXCEPTION: {e}"})
    return result
