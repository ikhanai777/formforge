"""Bar containers: ordering, de-duplication, and the closed-bar guarantee.

These are the invariants every indicator silently depends on. An indicator fed bars in
the wrong order returns a number rather than an error, so the ordering guarantee has to be
enforced at construction and tested here -- this is the only place it can be caught.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from bullion_fixtures import make_series

from bullion.series import (
    Candle,
    Series,
    interval_seconds,
    percentile_rank,
    resample,
    stdev,
)

UTC = timezone.utc


def _candle(day: int, close: float = 100.0, **kwargs) -> Candle:
    base = {
        "ts": datetime(2026, 1, day, tzinfo=UTC),
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": 1.0,
    }
    base.update(kwargs)
    return Candle(**base)


class TestCandle:
    def test_rejects_naive_timestamps(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            Candle(ts=datetime(2026, 1, 1), open=1, high=1, low=1, close=1)

    def test_rejects_incoherent_bars(self):
        # A close outside the high/low range means the provider sent nonsense or the
        # parser mapped the wrong columns. Either way it must not reach an indicator.
        with pytest.raises(ValueError, match="incoherent bar"):
            Candle(ts=datetime(2026, 1, 1, tzinfo=UTC), open=10, high=11, low=9, close=12)

    def test_rejects_non_positive_prices(self):
        with pytest.raises(ValueError, match="positive finite price"):
            Candle(ts=datetime(2026, 1, 1, tzinfo=UTC), open=0, high=1, low=0, close=1)


class TestSeries:
    def test_sorts_and_dedupes_keeping_the_last_write(self):
        # Providers return newest-first, and a retried request can deliver a corrected
        # bar for a timestamp already seen. Last write wins.
        series = Series.of(
            "XAU/USD",
            "1day",
            [_candle(3, 300.0), _candle(1, 100.0), _candle(2, 200.0), _candle(1, 111.0)],
        )
        assert [c.close for c in series] == [111.0, 200.0, 300.0]

    def test_closed_drops_a_forming_bar(self):
        now = datetime(2026, 1, 4, 12, tzinfo=UTC)
        series = Series.of("XAU/USD", "1day", [_candle(1), _candle(2), _candle(3), _candle(4)])
        # The bar opening on the 4th closes on the 5th, so at noon on the 4th it is forming.
        closed = series.closed(now)
        assert len(closed) == 3
        assert closed.last.ts == datetime(2026, 1, 3, tzinfo=UTC)

    def test_slice_until_is_inclusive_and_after_is_exclusive(self):
        series = Series.of("XAU/USD", "1day", [_candle(d) for d in range(1, 6)])
        cut = datetime(2026, 1, 3, tzinfo=UTC)
        assert len(series.slice_until(cut)) == 3
        assert len(series.after(cut)) == 2
        # Together they partition the series exactly once -- this is the property that
        # keeps a backtest from scoring a bar it also grades against.
        assert len(series.slice_until(cut)) + len(series.after(cut)) == len(series)

    def test_age_seconds_measures_from_the_close_not_the_open(self):
        series = Series.of("XAU/USD", "1day", [_candle(1)])
        now = datetime(2026, 1, 3, tzinfo=UTC)
        # The bar opened on the 1st and closed on the 2nd, so it is one day old.
        assert series.age_seconds(now) == pytest.approx(86400)

    def test_gaps_reports_missing_bars(self):
        series = Series.of("XAU/USD", "1day", [_candle(1), _candle(2), _candle(6)])
        gaps = series.gaps()
        assert len(gaps) == 1
        assert gaps[0][0] == datetime(2026, 1, 3, tzinfo=UTC)

    def test_last_raises_on_an_empty_series(self):
        with pytest.raises(ValueError, match="no candles"):
            _ = Series.of("XAU/USD", "1day", []).last


class TestResample:
    def test_aggregates_ohlcv_correctly(self):
        hourly = [
            Candle(
                ts=datetime(2026, 1, 1, hour, tzinfo=UTC),
                open=100 + hour,
                high=110 + hour,
                low=90 + hour,
                close=105 + hour,
                volume=10,
            )
            for hour in range(8)
        ]
        series = Series.of("XAU/USD", "1h", hourly)
        four_hour = resample(series, "4h")
        assert len(four_hour) == 2
        first = four_hour[0]
        assert first.open == hourly[0].open
        assert first.close == hourly[3].close
        assert first.high == max(c.high for c in hourly[:4])
        assert first.low == min(c.low for c in hourly[:4])
        assert first.volume == sum(c.volume for c in hourly[:4])

    def test_refuses_to_downsample(self):
        series = make_series([100.0] * 10)
        with pytest.raises(ValueError, match="cannot resample"):
            resample(series, "1h")

    def test_unknown_interval_names_the_known_ones(self):
        with pytest.raises(ValueError, match="known:"):
            interval_seconds("3weeks")


class TestStats:
    def test_stdev_of_a_constant_is_zero(self):
        assert stdev([5.0] * 10) == 0.0
        assert stdev([5.0]) == 0.0

    def test_percentile_rank_bounds(self):
        values = list(range(100))
        assert percentile_rank(values, -1) == 0.0
        assert percentile_rank(values, 200) == 1.0
        assert percentile_rank(values, 49) == pytest.approx(0.5)
        # An empty history is neutral rather than an error: the COT scorer asks for a
        # percentile before it has three years of reports.
        assert percentile_rank([], 5) == 0.5
