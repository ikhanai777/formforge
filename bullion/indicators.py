"""Technical indicators, computed here and not taken from a provider.

Spec section 3.2 is firm about this and it is the right call for two reasons that
are easy to conflate. The obvious one is auditability: when the app tells a user
"RSI 58, room to run", that number has to be reproducible from the OHLCV we
stored, or the explanation is theatre. The less obvious one is *consistency*: two
providers' RSI values differ (simple vs Wilder smoothing, different warmup, one
includes the forming bar), and a signal engine that silently switches between
them on provider failover will flip its own calls for no market reason.

Conventions that hold for every function here:

* Output lists are the same length as the input, with ``None`` through the warmup
  period. Nothing is back-filled and nothing is shifted -- index ``i`` of the
  output is always the value as of bar ``i``, which is what keeps the backtester
  honest.
* Wilder's smoothing (not a simple moving average) for RSI, ATR and ADX, because
  that is what the original definitions use and therefore what every charting
  package a user will cross-check against shows.
* Returns are plain floats, never rounded. Rounding belongs at the presentation
  edge; rounding here quietly moves a crossover by a bar.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .series import Series, stdev

Opt = float | None


# ---------------------------------------------------------------------------
# Smoothing primitives
# ---------------------------------------------------------------------------


def sma(values: Sequence[Opt], period: int) -> list[Opt]:
    """Simple moving average, emitted only where the whole window is present."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Opt] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sum(float(v) for v in window) / period  # type: ignore[arg-type]
    return out


def ema(values: Sequence[Opt], period: int) -> list[Opt]:
    """Exponential moving average, seeded with the SMA of the first full window.

    Seeding with the first value instead (as some libraries do) leaves a visible
    transient for several periods, which on a 200-period EMA means the first
    couple of hundred bars of any backfill are wrong. Seeding with the SMA
    confines the warmup to exactly ``period`` bars.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Opt] = [None] * len(values)
    k = 2.0 / (period + 1.0)
    prev: float | None = None
    buf: list[float] = []
    for i, raw in enumerate(values):
        if raw is None:
            continue
        value = float(raw)
        if prev is None:
            buf.append(value)
            if len(buf) == period:
                prev = sum(buf) / period
                out[i] = prev
        else:
            prev = value * k + prev * (1 - k)
            out[i] = prev
    return out


def rma(values: Sequence[Opt], period: int) -> list[Opt]:
    """Wilder's smoothing: an EMA with k = 1/period, seeded on the first window."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Opt] = [None] * len(values)
    prev: float | None = None
    buf: list[float] = []
    for i, raw in enumerate(values):
        if raw is None:
            continue
        value = float(raw)
        if prev is None:
            buf.append(value)
            if len(buf) == period:
                prev = sum(buf) / period
                out[i] = prev
        else:
            prev = (prev * (period - 1) + value) / period
            out[i] = prev
    return out


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MACD:
    line: list[Opt]
    signal: list[Opt]
    histogram: list[Opt]


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> MACD:
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    line: list[Opt] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_ema, slow_ema)
    ]
    signal_line = ema(line, signal)
    hist: list[Opt] = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(line, signal_line)
    ]
    return MACD(line=line, signal=signal_line, histogram=hist)


@dataclass(frozen=True)
class ADX:
    """Directional movement. ``adx`` is trend *strength* and carries no direction.

    Used as a gate rather than a signal (spec section 3.2: "filters out signals in
    choppy/no-trend conditions"). Treating ADX as bullish because it is rising is
    the classic misreading -- it rises just as hard in a collapse.
    """

    adx: list[Opt]
    plus_di: list[Opt]
    minus_di: list[Opt]


def adx(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> ADX:
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("highs, lows and closes must be the same length")

    tr: list[Opt] = [None] * n
    plus_dm: list[Opt] = [None] * n
    minus_dm: list[Opt] = [None] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    atr_ = rma(tr, period)
    plus_s = rma(plus_dm, period)
    minus_s = rma(minus_dm, period)

    plus_di: list[Opt] = [None] * n
    minus_di: list[Opt] = [None] * n
    dx: list[Opt] = [None] * n
    for i in range(n):
        a, p, m = atr_[i], plus_s[i], minus_s[i]
        if a is None or p is None or m is None or a == 0:
            continue
        plus_di[i] = 100.0 * p / a
        minus_di[i] = 100.0 * m / a
        total = plus_di[i] + minus_di[i]  # type: ignore[operator]
        if total:
            dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / total  # type: ignore[operator]

    return ADX(adx=rma(dx, period), plus_di=plus_di, minus_di=minus_di)


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------


def rsi(values: Sequence[float], period: int = 14) -> list[Opt]:
    n = len(values)
    gains: list[Opt] = [None] * n
    losses: list[Opt] = [None] * n
    for i in range(1, n):
        change = values[i] - values[i - 1]
        gains[i] = max(change, 0.0)
        losses[i] = max(-change, 0.0)
    avg_gain = rma(gains, period)
    avg_loss = rma(losses, period)
    out: list[Opt] = [None] * n
    for i in range(n):
        g, loss = avg_gain[i], avg_loss[i]
        if g is None or loss is None:
            continue
        if loss == 0:
            out[i] = 100.0 if g > 0 else 50.0
        else:
            out[i] = 100.0 - (100.0 / (1.0 + g / loss))
    return out


@dataclass(frozen=True)
class StochRSI:
    k: list[Opt]
    d: list[Opt]


def stoch_rsi(
    values: Sequence[float],
    rsi_period: int = 14,
    stoch_period: int = 14,
    smooth_k: int = 3,
    smooth_d: int = 3,
) -> StochRSI:
    """Stochastic RSI: where RSI sits within its own recent range.

    More responsive than RSI at turns, and correspondingly noisier -- which is
    why the technical layer only lets it contribute on a cross, and only when
    RSI itself is not already at an extreme.
    """
    base = rsi(values, rsi_period)
    n = len(values)
    raw: list[Opt] = [None] * n
    for i in range(n):
        window = base[max(0, i - stoch_period + 1) : i + 1]
        if len(window) < stoch_period or any(v is None for v in window):
            continue
        lo = min(float(v) for v in window)  # type: ignore[arg-type]
        hi = max(float(v) for v in window)  # type: ignore[arg-type]
        current = float(base[i])  # type: ignore[arg-type]
        raw[i] = 50.0 if hi == lo else 100.0 * (current - lo) / (hi - lo)
    k = sma(raw, smooth_k)
    return StochRSI(k=k, d=sma(k, smooth_d))


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------


def true_range(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[Opt]:
    out: list[Opt] = [None] * len(closes)
    for i in range(1, len(closes)):
        out[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    return out


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[Opt]:
    """Average true range. Also the unit stops are sized in (spec section 6)."""
    return rma(true_range(highs, lows, closes), period)


@dataclass(frozen=True)
class Bollinger:
    upper: list[Opt]
    middle: list[Opt]
    lower: list[Opt]
    # Where price sits in the band, 0 at the lower and 1 at the upper.
    percent_b: list[Opt]
    # Band width as a fraction of the middle band: the squeeze detector.
    bandwidth: list[Opt]


def bollinger(values: Sequence[float], period: int = 20, mult: float = 2.0) -> Bollinger:
    mid = sma(values, period)
    n = len(values)
    upper: list[Opt] = [None] * n
    lower: list[Opt] = [None] * n
    pb: list[Opt] = [None] * n
    bw: list[Opt] = [None] * n
    for i in range(n):
        if mid[i] is None:
            continue
        window = values[i - period + 1 : i + 1]
        sd = stdev(window)
        centre = float(mid[i])  # type: ignore[arg-type]
        upper[i] = centre + mult * sd
        lower[i] = centre - mult * sd
        span = upper[i] - lower[i]  # type: ignore[operator]
        pb[i] = 0.5 if span == 0 else (values[i] - lower[i]) / span  # type: ignore[operator]
        bw[i] = (span / centre) if centre else 0.0
    return Bollinger(upper=upper, middle=mid, lower=lower, percent_b=pb, bandwidth=bw)


# ---------------------------------------------------------------------------
# The bundle the scoring layer consumes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Indicators:
    """Every indicator value for one series, latest-bar scalars plus full arrays.

    The scalars are what the technical scorer reads; the arrays are what the
    dashboard plots and what the backtester replays. They come from one
    computation so the chart a user looks at is provably the data the call was
    made on -- recomputing for the UI is how a dashboard ends up disagreeing with
    its own signal.
    """

    asset: str
    interval: str
    close: float
    prev_close: float | None

    ema20: Opt
    ema50: Opt
    ema200: Opt
    ema20_prev: Opt
    ema50_prev: Opt

    macd_line: Opt
    macd_signal: Opt
    macd_hist: Opt
    macd_hist_prev: Opt

    rsi14: Opt
    rsi14_prev: Opt
    stoch_k: Opt
    stoch_d: Opt
    stoch_k_prev: Opt
    stoch_d_prev: Opt

    atr14: Opt
    adx14: Opt
    adx14_prev: Opt
    plus_di: Opt
    minus_di: Opt

    bb_upper: Opt
    bb_middle: Opt
    bb_lower: Opt
    percent_b: Opt
    bandwidth: Opt
    bandwidth_percentile: Opt

    volume: float
    volume_sma20: Opt

    bars: int
    arrays: dict[str, list[Opt]] = field(default_factory=dict, repr=False)

    @property
    def atr_pct(self) -> float | None:
        """ATR as a fraction of price -- the cross-asset volatility comparison."""
        if self.atr14 is None or not self.close:
            return None
        return self.atr14 / self.close

    def as_dict(self) -> dict[str, float | None]:
        """Flat scalar view, for the ``indicators`` table and the API."""
        skip = {"arrays", "asset", "interval"}
        out: dict[str, float | None] = {}
        for key, value in self.__dict__.items():
            if key in skip:
                continue
            out[key] = value
        out["atr_pct"] = self.atr_pct
        return out


def _at(values: Sequence[Opt], offset: int = 0) -> Opt:
    """Value ``offset`` bars back from the end, or None if out of range."""
    index = len(values) - 1 - offset
    return values[index] if 0 <= index < len(values) else None


def compute(series: Series) -> Indicators:
    """Compute the whole indicator set for a series.

    Raises on a series too short to produce a trend read at all. That is
    deliberate: the alternative is a pile of Nones flowing into the scorer, which
    then produces a low-confidence signal off three bars of data and presents it
    with the same interface as a real one. A caller that cannot get 30 bars should
    report a data problem, not a weak signal.
    """
    if len(series) < 30:
        raise ValueError(
            f"{series.asset} {series.interval}: need >=30 bars to compute indicators, "
            f"have {len(series)}"
        )

    closes = series.closes
    highs = series.highs
    lows = series.lows
    volumes = series.volumes

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    m = macd(closes)
    r = rsi(closes, 14)
    sr = stoch_rsi(closes)
    a = atr(highs, lows, closes, 14)
    dmi = adx(highs, lows, closes, 14)
    bb = bollinger(closes, 20, 2.0)
    vol_sma = sma(volumes, 20)

    # Bandwidth percentile over the trailing year of bars (or whatever exists):
    # a squeeze only means something relative to this market's own history.
    bw_history = [float(v) for v in bb.bandwidth[-252:] if v is not None]
    bw_now = _at(bb.bandwidth)
    bw_pct: Opt = None
    if bw_now is not None and bw_history:
        bw_pct = sum(1 for v in bw_history if v <= bw_now) / len(bw_history)

    return Indicators(
        asset=series.asset,
        interval=series.interval,
        close=closes[-1],
        prev_close=closes[-2] if len(closes) > 1 else None,
        ema20=_at(e20),
        ema50=_at(e50),
        ema200=_at(e200),
        ema20_prev=_at(e20, 1),
        ema50_prev=_at(e50, 1),
        macd_line=_at(m.line),
        macd_signal=_at(m.signal),
        macd_hist=_at(m.histogram),
        macd_hist_prev=_at(m.histogram, 1),
        rsi14=_at(r),
        rsi14_prev=_at(r, 1),
        stoch_k=_at(sr.k),
        stoch_d=_at(sr.d),
        stoch_k_prev=_at(sr.k, 1),
        stoch_d_prev=_at(sr.d, 1),
        atr14=_at(a),
        adx14=_at(dmi.adx),
        adx14_prev=_at(dmi.adx, 1),
        plus_di=_at(dmi.plus_di),
        minus_di=_at(dmi.minus_di),
        bb_upper=_at(bb.upper),
        bb_middle=_at(bb.middle),
        bb_lower=_at(bb.lower),
        percent_b=_at(bb.percent_b),
        bandwidth=bw_now,
        bandwidth_percentile=bw_pct,
        volume=volumes[-1],
        volume_sma20=_at(vol_sma),
        bars=len(series),
        arrays={
            "close": list(closes),
            "ema20": e20,
            "ema50": e50,
            "ema200": e200,
            "macd_hist": m.histogram,
            "rsi14": r,
            "atr14": a,
            "adx14": dmi.adx,
            "bb_upper": bb.upper,
            "bb_lower": bb.lower,
        },
    )
