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
import re
from dataclasses import dataclass, field

from agents.fibo_agent import FiboStructure
from agents.price_behavior_agent import NearestLevelInfo, RecentEvent
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
    положение цены -- ровно тот же обязательный набор данных, что и раньше.
    Регламент задаёт СОДЕРЖАНИЕ сообщения, но ничего не говорит про то, как
    оно должно выглядеть визуально -- значит, ниже чистое оформление, а не
    смена методики (раздел 25 тут не задействован, по сути менять нечего).

    Визуальный редизайн от 25 августа, по прямому запросу Леонида ("чтобы
    это не выглядело просто как слова и цифры, а чтобы глазу было
    приятно"): HTML-разметка для Telegram (parse_mode="HTML" в
    send_via_telegram) -- жирный заголовок, моноширинная таблица уровней с
    меткой ближайшего/тестируемого уровня, текстовая полоска глубины
    коррекции, эмодзи вместо сухих слов "восходящий/нисходящий".

    alert_level -- если задан, это реальный триггер level-watch (не просто
    информационный дамп), заголовок оформляется как алерт ("коррекция
    дошла до X"), а не как нейтральная сводка.

    display_name -- человеко-читаемое имя инструмента (например "Meta"),
    если отличается от тикера в bundle.symbol (например "META") -- нужно
    скринеру, чтобы показывать оба сразу. Для точечного IBM-watch не
    передаётся -- там имя и тикер и так совпадают.

    consensus_note -- 27 августа, по отзыву "брокера" (project doc):
    готовая строка от format_consensus_note() (verification_agent.py),
    добавляется отдельной строкой в конце сообщения, если передана. Не
    вычисляется здесь заново -- вызывающий код (orchestrator.py/screener.py)
    уже считает независимую сверку для консоли, просто теперь передаёт тот
    же результат и сюда, чтобы получатель алерта тоже его видел.

    intraday_note -- 2 сентября 2026, раздел 7.3 регламента (многотайм-
    фреймовое подтверждение 1H/4H, отзыв "брокера" 27 августа): готовая
    строка от build_intraday_note() (agents/intraday_agent.py), тот же
    принцип, что и у consensus_note -- необязательная, добавляется своей
    строкой, если передана, ничего не блокирует и не подменяет.
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
        lines.append("<i>вот текущая картина:</i>")
    else:
        lines.append(f"{dir_emoji} <b>{title}</b>")
    lines.append(f"📊 {_esc(bundle.source_tag)} · {_esc(bundle.timeframe)} · {_esc(bundle.period_desc)}")
    lines.append(f"{dir_emoji} ФИБО: {s.direction.value}, {s.scope.value}")
    lines.append("")
    lines.append(f"Точка 1 (100%): <b>{s.point1.price:.2f}</b> ({s.point1.kind}, {s.point1.dt})")
    lines.append(f"Точка 2 (0%):   <b>{s.point2.price:.2f}</b> ({s.point2.kind}, {s.point2.dt})")
    lines.append("")
    # 27 августа (по отзыву "брокера", см. project doc): ориентир инвалидации
    # -- закрытие ЗА точку 1 отменяет гипотезу "коррекция исчерпалась, дальше
    # снова движение в сторону точки 2" (та же логика, что уже неявно жила в
    # structure_id -- новый экстремум за точкой 1 и так означал бы новую
    # структуру при следующем прогоне). Явно показываем это ЧИСЛОМ, а не
    # только подразумеваем -- сформулировано как гипотеза, а не команда
    # "стоп тут": бот не даёт торговых указаний, только структурный ориентир.
    lines.append(
        f"⚠️ Ориентир инвалидации (гипотеза «коррекция исчерпалась»): "
        f"закрытие за <b>{s.point1.price:.2f}</b> (точка 1)"
    )
    lines.append("")
    lines.append(f"Глубина коррекции: <b>{frac * 100:.1f}%</b>")
    lines.append(f"0% {_depth_bar(frac)} 100%")
    lines.append("")
    # 27 августа (по запросу Леонида -- "уровни-расширения на сообщении"):
    # расширения (1.414 и далее, уже посчитаны в fibo_agent.py, просто
    # раньше не показывались) включаются в таблицу, только когда цена
    # реально ушла за пределы исходного диапазона (frac > 1.0) -- иначе
    # они никому не нужны и просто загромождали бы обычный алерт.
    # Показываем не ВСЕ расширения сразу, а только до ближайшего ещё не
    # достигнутого ("следующая цель") -- чтобы таблица росла постепенно
    # вместе с движением цены, а не показывала сразу все 5 уровней вперёд.
    extension_levels_sorted = sorted(lv.level for lv in s.levels if lv.level > 1.0)
    if frac > 1.0 and extension_levels_sorted:
        next_target = next((lv for lv in extension_levels_sorted if lv >= frac), extension_levels_sorted[-1])
        show_up_to = next_target
    else:
        show_up_to = 1.0
    level_lines = []
    sorted_levels = sorted((lv for lv in s.levels if lv.level <= show_up_to), key=lambda x: x.level)
    for lv in sorted_levels:
        is_nearest = lv.level == n.nearest_level
        marker = "▶" if is_nearest else " "
        tag = ""
        if is_nearest:
            tag = "  ← тестирует" if n.is_testing else "  ← ближайший"
        level_lines.append(f"{marker} {lv.level:>5.3f} = {lv.price:>10.2f}{tag}")
    lines.append("<pre>" + _esc("\n".join(level_lines)) + "</pre>")
    lines.append(f"💰 Текущая цена: <b>{n.current_price:.2f}</b>")
    if n.is_testing:
        lines.append(f"🎯 Цена тестирует уровень {n.nearest_level:g} ({n.nearest_price:.2f})")
    else:
        below = f"{n.below_level:g} ({n.below_price:.2f})" if n.below_level is not None else "—"
        above = f"{n.above_level:g} ({n.above_price:.2f})" if n.above_level is not None else "—"
        lines.append(
            f"Цена между уровнями {below} и {above}, ближе к {n.nearest_level:g} ({n.nearest_price:.2f})"
        )
    if bundle.recent_events:
        lines.append("")
        lines.append("🔁 <b>Недавние реакции на уровни:</b>")
        for e in bundle.recent_events[-5:]:
            lines.append(f"• {e.dt}: {_esc(e.kind)} @ {e.level:g} ({e.level_price:.2f})")
            lines.append(f"  <i>{_esc(e.note)}</i>")
    else:
        lines.append("")
        lines.append("Недавних тестов/пробоев ключевых уровней не найдено в рассмотренном окне.")
    if consensus_note:
        lines.append("")
        lines.append(_esc(consensus_note))
    if intraday_note:
        lines.append("")
        lines.append(_esc(intraday_note))
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
