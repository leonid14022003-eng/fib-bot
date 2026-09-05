"""
Chart Agent
============
Рендерит PNG-график (тёмная тема): свечи + сетка ФИБО -- для отправки через
sendPhoto, ДОПОЛНИТЕЛЬНО к текстовому сообщению (не вместо него -- по
прямому решению Леонида, 26 августа: "Фото + весь текущий текст", оба
сообщения на каждый реальный алерт).

Тёмный фон -- по прямому запросу Леонида, 26 августа ("задний фон изменим
с белого на чёрный чтобы было удобнее глазу"). Фон и общая цветовая логика
чарта (свечи, сетка, текст) взяты в духе привычного трейдерского тёмного
интерфейса -- знакомый трейдерам вид, а не выдумано с нуля.

Цветовая схема уровней ФИБО (вариант "B" -- выбран Леонидом 26 августа при
сравнении двух вариантов, см. project doc):
  -- 0.786 = зелёный -- ЗАКРЕПЛЕНО регламентом (claude/fibonacci-reglament.md,
     раздел 16 -- единственный уровень, для которого регламент прямо
     называет цвет).
  -- 0.618 -- золотой/"внимание" (акцент): это и есть порог, при котором бот
     вообще присылает алерт (should_send_level_watch, watch_levels), поэтому
     на графике он специально выделен сильнее всех -- цвет + более толстая
     линия + звёздочка у подписи (см. is_highlighted ниже).
  -- 0.236 / 0.382 / 0.5 -- НАМЕРЕННО приглушены (монотонный синий, от
     тусклого к чуть ярче) -- идея варианта B: меньше "радуги", взгляд
     сразу падает на 0.618 и 0.786, а не распыляется по всем 4 уровням
     поровну (это и был выбор Леонида между вариантом A "все яркие
     поровну" и вариантом B "акцент на важном" -- увидел оба, выбрал B).
     РАБОЧАЯ схема, выбранная Claude (26 августа, по прямому разрешению
     Леонида: "Подбери сам под тёмный фон"), явно ВРЕМЕННАЯ -- раздел 16
     регламента не называет цвета для этих уровней, точный список так и не
     прислан (открытый вопрос, см. README.md и project doc).
  -- 0 и 1 (границы диапазона, не "уровень" в смысле раздела 16) --
     нейтральный приглушённый серый, отдельно от уровневой схемы.

Все цвета (уровни + фон) прогнаны через scripts/validate_palette.js из
skill'а dataviz -- контраст к фону #131722 у каждого >= 3:1 (самый тусклый,
0.236, -- 3.32:1), критичный переход 0.5 -> 0.618 -> 0.786 (единственные
"настоящие" по яркости соседи в этой схеме) прошёл CVD/нормальное зрение с
большим запасом (худшая пара 0.786<->0.618 ΔE 21.3 protan / 33.7 нормальное
зрение, при порогах 8 и 15).
"""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt

from agents.data_agent import Candle
from agents.fibo_agent import FiboStructure

# --- Палитра (тёмная тема, см. докстринг файла) -----------------------------
BG = "#131722"  # фон графика
GRID = "#232733"  # едва заметная сетка -- третий план, не спорит с данными
INK = "#d1d4dc"  # основной текст/подписи (контраст к фону 12.1:1)
MUTED = "#898781"  # приглушённый -- границы диапазона 0/1, НЕ "уровень" раздела 16
CANDLE_UP = "#0ca30c"  # свеча вверх (close >= open) -- обычная торговая конвенция, не раздел 16
CANDLE_DOWN = "#d03b3b"  # свеча вниз -- то же самое, конвенция

LEVEL_COLORS: dict[float, str] = {
    0.0: MUTED,
    0.236: "#256abf",  # тусклый синий -- намеренно приглушён (вариант B)
    0.382: "#2a78d6",  # синий чуть ярче
    0.5: "#3987e5",  # синий ещё чуть ярче -- ближе всех "неважных" к акценту
    0.618: "#fab219",  # золотой "внимание" -- порог алерта, главный акцент графика
    0.786: "#008300",  # зелёный -- ЗАКРЕПЛЕНО регламентом, раздел 16
    1.0: MUTED,
}

_PLACEHOLDER_LEVELS = {0.236, 0.382, 0.5, 0.618}  # раздел 16 не называет для них цвет


def render_chart(
    structure: FiboStructure,
    candles: list[Candle],
    current_price: float,
    symbol: str,
    timeframe: str,
    alert_level: float | None = None,
    display_name: str | None = None,
) -> bytes:
    """
    Возвращает готовые PNG-байты (тёмная тема, свечи + сетка ФИБО) для
    send_photo_via_telegram(). Ничего не пишет на диск -- вызывающий код сам
    решает, сохранять ли (см. __main__ ниже -- ручная проверка глазами).

    structure/candles/current_price/symbol/timeframe -- те же данные, что
    AnalysisBundle несёт для format_message(), но AnalysisBundle не хранит
    сырые свечи, поэтому candles передаются отдельно (см. вызывающий код в
    orchestrator.py/screener.py -- там series.candles уже в области видимости).

    alert_level -- если задан, именно этот уровень выделяется на графике
    визуально сильнее (толще линия, звёздочка у подписи, поверх остальных
    линий по z-order) -- он и есть причина, почему график вообще
    отправляется. Если не задан (нейтральный вариант без триггера), эту
    роль по умолчанию играет 0.618 -- основной уровень наблюдения (как и в
    исходном мокапе от 26 августа).

    display_name -- см. format_message() в dispatch_agent.py, тот же смысл
    (человеко-читаемое имя инструмента для скринера, например "Meta" при
    symbol="META").
    """
    if display_name and display_name != symbol:
        title_name = f"{display_name} ({symbol})"
    else:
        title_name = symbol

    highlight_level = alert_level if alert_level is not None else 0.618

    fig, ax = plt.subplots(figsize=(11, 7), dpi=160)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    for i, c in enumerate(candles):
        color = CANDLE_UP if c.close >= c.open else CANDLE_DOWN
        ax.plot([i, i], [c.low, c.high], color=color, linewidth=0.8, solid_capstyle="round", zorder=2)
        body_bottom = min(c.open, c.close)
        # Минимальная высота тела свечи -- относительная (0.1% цены), а не
        # фиксированное число: у screener'а инструменты очень разного масштаба
        # цены (акции ~$50-500, индексы ~тысячи, товары ~2000-2500) -- см.
        # screener.py INSTRUMENTS.
        body_height = max(abs(c.close - c.open), c.close * 0.001)
        ax.add_patch(
            mpatches.Rectangle(
                (i - 0.32, body_bottom), 0.64, body_height, facecolor=color, edgecolor=color, zorder=3
            )
        )

    # 27 августа (по запросу Леонида -- "уровни-расширения на графике"):
    # расширения (1.414 и далее) рисуются, только когда цена реально ушла
    # за пределы исходного диапазона (0..1) -- иначе они не нужны и просто
    # растянули бы шкалу цены без пользы. Формула -- та же, что и
    # retracement_fraction() в dispatch_agent.py, но посчитана прямо здесь
    # (render_chart принимает structure/current_price напрямую, а не
    # AnalysisBundle -- см. докстринг файла) -- показываем только до
    # ближайшей ещё не достигнутой цели, не все 5 расширений разом.
    frac = (current_price - structure.point2.price) / (structure.point1.price - structure.point2.price)
    extension_levels_sorted = sorted(lv.level for lv in structure.levels if lv.level > 1.0)
    if frac > 1.0 and extension_levels_sorted:
        show_up_to = next((lv for lv in extension_levels_sorted if lv >= frac), extension_levels_sorted[-1])
    else:
        show_up_to = 1.0

    n = len(candles)
    sorted_levels = sorted(structure.levels, key=lambda lv: lv.level)
    for lv in sorted_levels:
        if lv.level > show_up_to:
            continue  # либо расширение, либо ещё не релевантная более далёкая цель -- не рисуем
        # 27 августа (по отзыву "брокера" -- см. project doc): точка 1 (1.0)
        # заодно и есть ориентир инвалидации гипотезы "коррекция исчерпалась"
        # (см. dispatch_agent.py format_message) -- но только когда график
        # сопровождает РЕАЛЬНЫЙ алерт (alert_level задан), не нейтральный
        # вариант без триггера. Не рисуем отдельную вторую линию поверх той
        # же цены -- просто перекрашиваем и переподписываем уже существующую
        # линию уровня 1.0, чтобы не плодить визуальный шум на одной высоте.
        is_invalidation = lv.level == 1.0 and alert_level is not None
        color = CANDLE_DOWN if is_invalidation else LEVEL_COLORS.get(lv.level, MUTED)
        is_highlighted = lv.level == highlight_level
        lw = 2.2 if (is_highlighted or is_invalidation) else 1.1
        z = 2.5 if (is_highlighted or is_invalidation) else 1
        linestyle = "-." if is_invalidation else "--"
        ax.axhline(lv.price, color=color, linewidth=lw, linestyle=linestyle, alpha=0.9, zorder=z)
        # ⚠, не ⛔ -- тот же символ, что уже используется в тексте алерта
        # (dispatch_agent.py, "Ориентир инвалидации"). ⛔ (U+26D4) отсутствует
        # в DejaVu Sans (шрифт matplotlib по умолчанию) -- при рендере
        # молча превращался бы в "квадратик" вместо символа; поймано при
        # смоук-тесте превью-скрипта 27 августа (UserWarning про
        # отсутствующий глиф), а не на глаз -- ⚠ явно проверен через
        # FT2Font.get_char_index, глиф есть.
        tag = ("  ★" if is_highlighted else "") + ("  ⚠ инвалидация" if is_invalidation else "")
        ax.text(
            n - 0.5,
            lv.price,
            f"  {lv.level:g} = {lv.price:.2f}{tag}",
            va="center",
            ha="left",
            fontsize=9,
            color=color,
            fontweight="bold" if (is_highlighted or is_invalidation) else "normal",
            bbox=dict(facecolor=BG, edgecolor="none", pad=1.5),
            zorder=5,
        )

    ax.axhline(current_price, color=INK, linewidth=1.0, linestyle=":", alpha=0.7, zorder=1)
    ax.plot(n - 1, current_price, marker="o", markersize=6, color=INK, zorder=4)
    ax.text(
        n - 1,
        current_price,
        f"  тек. цена {current_price:.2f}",
        va="bottom",
        ha="left",
        fontsize=8.5,
        color=INK,
        bbox=dict(facecolor=BG, edgecolor="none", pad=1.0),
        zorder=5,
    )

    ax.set_xlim(-2, n + 16)
    dir_word = structure.direction.value
    if alert_level is not None:
        chart_title = f"{title_name} — коррекция дошла до {alert_level:g} ({dir_word}, {structure.scope.value})"
    else:
        chart_title = f"{title_name} — {dir_word} ФИБО ({structure.scope.value}), {timeframe}"
    ax.set_title(chart_title, fontsize=12, fontweight="bold", color=INK, pad=14)
    ax.set_xticks([])
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(GRID)
    ax.tick_params(colors=MUTED)
    ax.yaxis.grid(True, color=GRID, linewidth=0.6, zorder=0)
    ax.set_ylabel("Цена", color=MUTED)

    fig.text(
        0.5,
        0.005,
        "Цвета уровней 0.236/0.382/0.5/0.618 — рабочая схема (не финал, раздел 16 регламента "
        "не называет цвета). 0.786 — зелёный по регламенту.",
        ha="center",
        fontsize=7.5,
        color=MUTED,
    )

    fig.tight_layout(rect=(0, 0.02, 1, 1))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


if __name__ == "__main__":
    from pathlib import Path

    from agents.data_agent import load_ibm_demo_daily
    from agents.fibo_agent import build_global_fibo

    series = load_ibm_demo_daily(Path(__file__).resolve().parent.parent / "data" / "ibm_daily_raw.txt")
    structure = build_global_fibo(series)
    price = series.candles[-1].close

    png_neutral = render_chart(structure, series.candles, price, series.symbol, series.timeframe)
    png_alert = render_chart(
        structure, series.candles, price, series.symbol, series.timeframe, alert_level=0.618
    )

    out_dir = Path(__file__).resolve().parent.parent / "output"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "chart_preview_neutral.png").write_bytes(png_neutral)
    (out_dir / "chart_preview_alert.png").write_bytes(png_alert)
    print(f"neutral: {len(png_neutral)} байт -> {out_dir / 'chart_preview_neutral.png'}")
    print(f"alert:   {len(png_alert)} байт -> {out_dir / 'chart_preview_alert.png'}")
