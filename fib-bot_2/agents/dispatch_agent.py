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

from dataclasses import dataclass, field

from agents.fibo_agent import FiboStructure
from agents.price_behavior_agent import NearestLevelInfo, RecentEvent
from agents.verification_agent import ChecklistReport


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


def format_message(bundle: AnalysisBundle) -> str:
    """Формат раздела 24 регламента: инструмент, источник, таймфрейм, период,
    тип ФИБО, структура, точка 1, точка 2, основные уровни, текущая цена,
    положение цены."""
    s = bundle.structure
    n = bundle.nearest

    lines = [
        f"{bundle.symbol}",
        f"Источник: {bundle.source_tag}",
        f"Таймфрейм: {bundle.timeframe} | Период: {bundle.period_desc}",
        f"ФИБО: {s.direction.value}, {s.scope.value}",
        f"Точка 1 (100%): {s.point1.price:.2f} ({s.point1.kind}, {s.point1.dt})",
        f"Точка 2 (0%): {s.point2.price:.2f} ({s.point2.kind}, {s.point2.dt})",
        "Уровни: "
        + ", ".join(f"{lv.level:g}={lv.price:.2f}" for lv in sorted(s.levels, key=lambda x: x.level) if lv.level <= 1),
        f"Текущая цена: {n.current_price:.2f}",
    ]

    if n.is_testing:
        lines.append(f"Цена тестирует уровень {n.nearest_level:g} ({n.nearest_price:.2f})")
    else:
        below = f"{n.below_level:g} ({n.below_price:.2f})" if n.below_level is not None else "—"
        above = f"{n.above_level:g} ({n.above_price:.2f})" if n.above_level is not None else "—"
        lines.append(
            f"Цена между уровнями {below} и {above}, ближе к {n.nearest_level:g} ({n.nearest_price:.2f})"
        )

    if bundle.recent_events:
        lines.append("Недавние реакции на уровни:")
        for e in bundle.recent_events[-5:]:
            lines.append(f"  {e.dt}: {e.kind} @ {e.level:g} ({e.level_price:.2f}) -- {e.note}")
    else:
        lines.append("Недавних тестов/пробоев ключевых уровней не найдено в рассмотренном окне.")

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


def send_via_telegram(message: str, recipients: list[Recipient], bot_token: str | None) -> dict:
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
    """
    result = {"dry_run": bot_token is None, "sent_to": [], "message_preview": message}

    for r in recipients:
        if bot_token is None or r.telegram_chat_id is None:
            result["sent_to"].append({"recipient": r.label, "status": "SKIPPED (нет токена или chat_id)"})
            continue

        import requests  # локальный импорт: не нужен в dry-run/тестах, только для реальной отправки

        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": r.telegram_chat_id, "text": message},
                timeout=15,
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
