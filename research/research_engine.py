"""
Исследовательский движок (21 сентября 2026): проверка методики сигнала на
БОЛЬШОЙ выборке вместо 33 сигналов из backtest.py.

Принципы (все -- против самообмана):
  1. Никакого заглядывания вперёд: решение на баре t использует только
     данные [0..t]. Исход считается по барам ПОСЛЕ t.
  2. Один и тот же геометрический контракт сигнала, как в боте: точка 1 =
     начало движения (100%), точка 2 = конец (0%), f = глубина отката,
     тезис "откат исчерпан, движение продолжится к точке 2", инвалидация --
     закрытие за точкой 1, R = |вход - точка1|.
  3. Контрольная группа: случайные входы на тех же инструментах с ТОЙ ЖЕ
     геометрией (направление, риск в % цены, R-цель) -- иначе любой
     лонг-сигнал в бычьем десятилетии выглядел бы "работающим" просто из-за
     дрейфа рынка.
  4. Обучение/проверка по времени: конфигурацию выбираем на ранней части,
     судим по поздней (не участвовавшей в выборе).
  5. Доверительные интервалы -- бутстрэпом по ИНСТРУМЕНТАМ (сигналы внутри
     одного инструмента коррелированы, обычный SE был бы слишком оптимистичен).
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # корень репозитория (agents/, data/, backtest.py)
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "research_cache"

WARMUP = 500          # первые бары не торгуем (нужна история для окна/SMA200)
WATCH_PROD = (0.618, 0.786, 1.0)
WATCH_VALID = (0.618, 0.786)
H_E1 = 20             # как в backtest.py (MAX_HOLD_DAYS)
H_E2 = 40             # горизонт для сценария "цель = точка 2"
LOSS_FLOOR = -3.0     # проскальзывание/гэп: убыток в R не хуже -3R (документировано в отчёте)


# --------------------------------------------------------------------------
# данные
# --------------------------------------------------------------------------
@dataclass
class Inst:
    name: str
    kind: str  # "stock" | "index_cmdty" | "crypto"
    dates: list
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    atr14: np.ndarray = field(default=None)
    sma200: np.ndarray = field(default=None)

    @property
    def n(self) -> int:
        return len(self.c)


def _rolling_mean(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    cs = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def _atr(h, l, c, n=14):
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    return _rolling_mean(tr, n)


def load_inst(path: Path) -> Inst | None:
    kind_src, _, sym = path.stem.partition("__")
    rows = json.loads(path.read_text(encoding="utf-8"))
    if len(rows) < WARMUP + 100:
        return None
    dates = [r[0] for r in rows]
    a = np.array([[r[1], r[2], r[3], r[4], r[5]] for r in rows], dtype=float)
    o, h, l, c, v = a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    if kind_src == "binance":
        kind = "crypto"
    elif sym.startswith("^") or sym.endswith("=F"):
        kind = "index_cmdty"
    else:
        kind = "stock"
    inst = Inst(sym, kind, dates, o, h, l, c, v)
    inst.atr14 = _atr(h, l, c, 14)
    inst.sma200 = _rolling_mean(c, 200)
    return inst


def load_all() -> list[Inst]:
    out = []
    for p in sorted(CACHE.glob("*.json")):
        try:
            inst = load_inst(p)
        except Exception:  # noqa: BLE001
            inst = None
        if inst is not None:
            out.append(inst)
    return out


# --------------------------------------------------------------------------
# сигнал
# --------------------------------------------------------------------------
@dataclass
class Sig:
    inst: str
    kind: str
    t: int
    date: str
    level: float
    sign: int            # +1 -- тезис "вверх" (восходящая структура), -1 -- "вниз"
    p1: float
    p2: float
    p1_idx: int
    p2_idx: int
    entry: float
    risk: float          # |entry - p1|
    f: float
    atr: float
    trend_ok: bool = False
    confirm_ok: bool = False
    vol_ok: bool | None = None   # None -- объёма нет
    tags: dict = field(default_factory=dict)

    @property
    def risk_pct(self) -> float:
        return self.risk / self.entry

    @property
    def r_target(self) -> float:
        return abs(self.p2 - self.entry) / self.risk if self.risk > 0 else float("nan")


def _features(inst: Inst, s: Sig) -> Sig:
    t = s.t
    sma = inst.sma200[t]
    sma_prev = inst.sma200[t - 20] if t >= 20 else np.nan
    if not (np.isnan(sma) or np.isnan(sma_prev)):
        if s.sign > 0:
            s.trend_ok = bool(inst.c[t] > sma and sma > sma_prev)
        else:
            s.trend_ok = bool(inst.c[t] < sma and sma < sma_prev)
    # свеча-подтверждение разворота отката в сторону тезиса
    if s.sign > 0:
        s.confirm_ok = bool(inst.c[t] > inst.o[t] and inst.c[t] > inst.c[t - 1])
    else:
        s.confirm_ok = bool(inst.c[t] < inst.o[t] and inst.c[t] < inst.c[t - 1])
    # здоровый откат идёт на пониженном объёме относительно импульса
    a, b = min(s.p1_idx, s.p2_idx), max(s.p1_idx, s.p2_idx)
    imp = inst.v[a + 1:b + 1]
    pull = inst.v[b + 1:t + 1]
    if len(imp) >= 2 and len(pull) >= 2 and np.mean(imp) > 0 and np.mean(pull) > 0:
        s.vol_ok = bool(np.mean(pull) < np.mean(imp))
    else:
        s.vol_ok = None
    s.atr = float(inst.atr14[t]) if not np.isnan(inst.atr14[t]) else float("nan")
    return s


def _mk(inst: Inst, t: int, level: float, p1: float, p2: float, i1: int, i2: int) -> Sig | None:
    entry = float(inst.c[t])
    risk = abs(entry - p1)
    if risk <= 0 or p1 == p2:
        return None
    sign = 1 if p2 > p1 else -1
    f = (entry - p2) / (p1 - p2)
    s = Sig(inst.name, inst.kind, t, inst.dates[t], level, sign, p1, p2, i1, i2, entry, risk, f, float("nan"))
    return _features(inst, s)


# --------------------------------------------------------------------------
# генератор G0: как в проде / backtest.py -- экстремумы ОКНА (rolling W баров)
# --------------------------------------------------------------------------
def gen_global(inst: Inst, window: int | None = 1260, watch=WATCH_PROD, min_left: int = 5, start: int = WARMUP) -> list[Sig]:
    sigs: list[Sig] = []
    state_id, state_level = None, None
    h, l = inst.h, inst.l
    for t in range(start, inst.n):
        a = 0 if window is None else max(0, t + 1 - window)
        wh, wl = h[a:t + 1], l[a:t + 1]
        hi_rel, lo_rel = int(np.argmax(wh)), int(np.argmin(wl))
        hi_idx, lo_idx = a + hi_rel, a + lo_rel
        # подтверждение "не менее 5 свечей слева" -- буквально как _confirmed_left
        if np.sum(h[a:hi_idx] < h[hi_idx]) < min_left or np.sum(l[a:lo_idx] > l[lo_idx]) < min_left:
            continue
        if lo_idx < hi_idx:      # восходящая: точка 1 = LOW, точка 2 = HIGH
            p1, p2, i1, i2 = float(l[lo_idx]), float(h[hi_idx]), lo_idx, hi_idx
        else:                    # нисходящая
            p1, p2, i1, i2 = float(h[hi_idx]), float(l[lo_idx]), hi_idx, lo_idx
        if p1 == p2:
            continue
        f = (float(inst.c[t]) - p2) / (p1 - p2)
        reached = [lv for lv in watch if f >= lv]
        sid = (i1, i2)
        if not reached:
            continue
        deepest = max(reached)
        if state_id == sid and state_level is not None and deepest <= state_level:
            continue
        state_id, state_level = sid, deepest
        s = _mk(inst, t, deepest, p1, p2, i1, i2)
        if s is not None:
            sigs.append(s)
    return sigs


# --------------------------------------------------------------------------
# генератор G1: "последний значимый свинг" -- зигзаг на фракталах с порогом в ATR
# --------------------------------------------------------------------------
def _fractals(inst: Inst, k: int):
    h, l, n = inst.h, inst.l, inst.n
    ev = []   # (время подтверждения, индекс, тип)
    for i in range(k, n - k):
        left_h, right_h = h[i - k:i], h[i + 1:i + 1 + k]
        left_l, right_l = l[i - k:i], l[i + 1:i + 1 + k]
        if h[i] > left_h.max() and h[i] > right_h.max():
            ev.append((i + k, i, "H"))
        if l[i] < left_l.min() and l[i] < right_l.min():
            ev.append((i + k, i, "L"))
    ev.sort()
    return ev


def gen_swing(inst: Inst, k: int = 5, m_atr: float = 3.0, watch=WATCH_VALID, min_risk_atr: float = 1.0) -> list[Sig]:
    sigs: list[Sig] = []
    ev = _fractals(inst, k)
    ei = 0
    swings: list[tuple[int, str, float]] = []   # принятые: (idx, "H"/"L", price)
    state_id, state_level = None, None
    h, l, c = inst.h, inst.l, inst.c
    for t in range(0, inst.n):
        while ei < len(ev) and ev[ei][0] <= t:
            _ct, i, typ = ev[ei]
            ei += 1
            price = float(h[i] if typ == "H" else l[i])
            atr_i = inst.atr14[i]
            if np.isnan(atr_i):
                continue
            if not swings:
                swings.append((i, typ, price))
                continue
            li, lt, lp = swings[-1]
            if lt == typ:
                if (typ == "H" and price > lp) or (typ == "L" and price < lp):
                    swings[-1] = (i, typ, price)
            else:
                if abs(price - lp) >= m_atr * atr_i:
                    swings.append((i, typ, price))
        if t < WARMUP or not swings:
            continue
        si, st, sp = swings[-1]
        if t <= si:
            continue
        if st == "L":     # восходящая нога от L: точка 1 = L, точка 2 = максимум с тех пор
            seg = h[si + 1:t + 1]
            bi = si + 1 + int(np.argmax(seg))
            p1, p2, i1, i2 = sp, float(h[bi]), si, bi
        else:             # нисходящая нога от H
            seg = l[si + 1:t + 1]
            bi = si + 1 + int(np.argmin(seg))
            p1, p2, i1, i2 = sp, float(l[bi]), si, bi
        leg = abs(p2 - p1)
        atr_t = inst.atr14[t]
        if np.isnan(atr_t) or leg < m_atr * atr_t:
            continue
        f = (float(c[t]) - p2) / (p1 - p2)
        if f >= 1.0:
            continue      # тезис мёртв: цена вернулась к началу ноги
        reached = [lv for lv in watch if f >= lv]
        if not reached:
            continue
        deepest = max(reached)
        sid = (i1, i2)
        if state_id == sid and state_level is not None and deepest <= state_level:
            continue
        state_id, state_level = sid, deepest
        risk = abs(float(c[t]) - p1)
        if risk < min_risk_atr * atr_t:
            continue      # стоп внутри шума -- не сделка
        s = _mk(inst, t, deepest, p1, p2, i1, i2)
        if s is not None:
            sigs.append(s)
    return sigs


# --------------------------------------------------------------------------
# исходы
# --------------------------------------------------------------------------
def outcome_e1(c: np.ndarray, t: int, sign: int, entry: float, risk: float, h: int = H_E1, cap_pos: float = 5.0):
    """Как backtest.py: стоп -- закрытие за точкой 1 (r<=-1), потолок +5R, иначе
    mark-to-close на H-й день. None -- нет полного окна вперёд."""
    if t + h >= len(c):
        return None
    r = sign * (c[t + 1:t + 1 + h] - entry) / risk
    stop = np.where(r <= -1.0)[0]
    up = np.where(r >= cap_pos)[0]
    first_stop = stop[0] if len(stop) else 10 ** 9
    first_up = up[0] if len(up) else 10 ** 9
    if first_stop == 10 ** 9 and first_up == 10 ** 9:
        val = float(r[-1])
        kind = "timeout"
    elif first_stop < first_up:
        val = float(r[first_stop])
        kind = "stop"
    else:
        val = cap_pos
        kind = "target"
    return max(val, LOSS_FLOOR), kind


def outcome_e2(c: np.ndarray, t: int, sign: int, entry: float, risk: float, r_target: float, h: int = H_E2):
    """Тезис буквально: цель = точка 2 (лимитный исход ровно в R_target),
    стоп = закрытие за точкой 1, таймаут mark-to-close."""
    if t + h >= len(c) or not (r_target > 0) or math.isnan(r_target):
        return None
    r = sign * (c[t + 1:t + 1 + h] - entry) / risk
    stop = np.where(r <= -1.0)[0]
    tgt = np.where(r >= r_target)[0]
    first_stop = stop[0] if len(stop) else 10 ** 9
    first_tgt = tgt[0] if len(tgt) else 10 ** 9
    if first_stop == 10 ** 9 and first_tgt == 10 ** 9:
        val, kind = float(r[-1]), "timeout"
    elif first_stop < first_tgt:
        val, kind = float(r[first_stop]), "stop"
    else:
        val, kind = float(r_target), "target"
    return max(val, LOSS_FLOOR), kind


# --------------------------------------------------------------------------
# статистика
# --------------------------------------------------------------------------
def mean_se(xs: list[float]):
    n = len(xs)
    if n == 0:
        return float("nan"), float("nan"), 0
    m = sum(xs) / n
    if n < 2:
        return m, float("nan"), n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, math.sqrt(var / n), n


def cluster_bootstrap(groups: dict[str, list[float]], b: int = 1000, seed: int = 7):
    """CI 95% для среднего R, ресемплинг ИНСТРУМЕНТОВ целиком."""
    keys = [k for k, v in groups.items() if v]
    if not keys:
        return float("nan"), float("nan")
    rnd = random.Random(seed)
    sums = {k: (sum(groups[k]), len(groups[k])) for k in keys}
    means = []
    for _ in range(b):
        s = cnt = 0
        for _k in range(len(keys)):
            ss, nn = sums[keys[rnd.randrange(len(keys))]]
            s += ss
            cnt += nn
        means.append(s / cnt if cnt else float("nan"))
    means.sort()
    return means[int(0.025 * b)], means[int(0.975 * b)]
