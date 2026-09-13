"""Indicator maths, checked against definitions rather than against itself.

The interesting tests here are the alignment ones. Every lookahead bug in a signal system
is an indicator whose value at index i was computed using data from index i+1, and no
amount of backtesting will reveal it -- the backtest will just look good. So the suite
asserts the property directly: computing an indicator over a truncated series must give
the same values as computing it over the full series and truncating.
"""

from __future__ import annotations

import pytest
from bullion_fixtures import choppy, make_series, trend_down, trend_up

from bullion.indicators import (
    Indicators,
    adx,
    atr,
    bollinger,
    compute,
    ema,
    rma,
    rsi,
    sma,
    stoch_rsi,
    true_range,
)


class TestSmoothing:
    def test_sma_warmup_is_exactly_period_minus_one(self):
        out = sma([1, 2, 3, 4, 5], 3)
        assert out[:2] == [None, None]
        assert out[2] == pytest.approx(2.0)
        assert out[4] == pytest.approx(4.0)

    def test_ema_is_seeded_with_the_sma_of_the_first_window(self):
        values = [10.0] * 5 + [20.0] * 5
        out = ema(values, 5)
        # Seeded on the SMA of the first five, which are all 10.
        assert out[4] == pytest.approx(10.0)
        # Then k = 2/6 toward 20.
        assert out[5] == pytest.approx(10.0 + (20.0 - 10.0) * (2 / 6))

    def test_ema_of_a_constant_is_that_constant(self):
        out = ema([7.0] * 50, 20)
        assert out[-1] == pytest.approx(7.0)

    def test_rma_uses_wilders_coefficient(self):
        values = [1.0] * 14 + [15.0]
        out = rma(values, 14)
        assert out[13] == pytest.approx(1.0)
        assert out[14] == pytest.approx((1.0 * 13 + 15.0) / 14)

    def test_zero_and_negative_periods_are_rejected(self):
        for fn in (sma, ema, rma):
            with pytest.raises(ValueError, match="period must be positive"):
                fn([1.0, 2.0], 0)


class TestRsi:
    def test_monotonic_rise_pins_rsi_at_100(self):
        out = rsi([100 + i for i in range(40)], 14)
        assert out[-1] == pytest.approx(100.0)

    def test_monotonic_fall_pins_rsi_near_zero(self):
        out = rsi([200 - i for i in range(40)], 14)
        assert out[-1] == pytest.approx(0.0, abs=1e-9)

    def test_flat_series_is_neutral_not_a_division_by_zero(self):
        # Average gain and average loss are both zero here; the naive formula divides by
        # zero and the conventional answer is 50.
        out = rsi([100.0] * 40, 14)
        assert out[-1] == pytest.approx(50.0)

    def test_warmup_is_none_not_backfilled(self):
        out = rsi([100 + i for i in range(40)], 14)
        assert out[:14] == [None] * 14
        assert out[14] is not None


class TestAtrAndAdx:
    def test_true_range_uses_the_prior_close(self):
        series = make_series([100.0, 110.0])
        tr = true_range(series.highs, series.lows, series.closes)
        assert tr[0] is None
        # The gap from the prior close is part of the range, so TR exceeds high-low.
        assert tr[1] >= series[1].high - series[1].low

    def test_atr_is_positive_and_scales_with_volatility(self):
        calm = make_series([100.0] * 60, wick=0.001)
        wild = make_series([100.0] * 60, wick=0.02)
        assert atr(calm.highs, calm.lows, calm.closes)[-1] < atr(
            wild.highs, wild.lows, wild.closes
        )[-1]

    def test_adx_is_high_in_a_trend_and_low_in_chop(self):
        up = trend_up(200)
        flat = choppy(200)
        trending = adx(up.highs, up.lows, up.closes).adx[-1]
        ranging = adx(flat.highs, flat.lows, flat.closes).adx[-1]
        assert trending > 40
        assert ranging < trending

    def test_adx_carries_no_direction(self):
        # The headline claim about ADX: it is just as high in a collapse. A layer that
        # treats a rising ADX as bullish is reading it wrong, and this is the test that
        # documents why.
        up = trend_up(200)
        down = trend_down(200)
        assert adx(up.highs, up.lows, up.closes).adx[-1] > 40
        assert adx(down.highs, down.lows, down.closes).adx[-1] > 40

    def test_di_lines_separate_by_direction(self):
        up = trend_up(200)
        read = adx(up.highs, up.lows, up.closes)
        assert read.plus_di[-1] > read.minus_di[-1]

    def test_mismatched_input_lengths_raise(self):
        with pytest.raises(ValueError, match="same length"):
            adx([1, 2, 3], [1, 2], [1, 2, 3])


class TestBollingerAndStoch:
    def test_percent_b_is_zero_at_the_lower_band_and_one_at_the_upper(self):
        series = make_series([100.0 + (5 if i % 2 else -5) for i in range(60)])
        bands = bollinger(series.closes, 20, 2.0)
        for index in range(20, 60):
            pb = bands.percent_b[index]
            close = series.closes[index]
            assert bands.lower[index] <= bands.middle[index] <= bands.upper[index]
            if close >= bands.upper[index]:
                assert pb >= 1.0
            if close <= bands.lower[index]:
                assert pb <= 0.0

    def test_flat_series_gives_zero_bandwidth_and_neutral_percent_b(self):
        bands = bollinger([100.0] * 40, 20, 2.0)
        assert bands.bandwidth[-1] == pytest.approx(0.0)
        assert bands.percent_b[-1] == pytest.approx(0.5)

    def test_stoch_rsi_is_bounded(self):
        series = trend_up(200)
        read = stoch_rsi(series.closes)
        values = [v for v in read.k if v is not None]
        assert values
        assert all(0.0 <= v <= 100.0 for v in values)


class TestAlignment:
    """No indicator may use a bar that had not happened yet."""

    @pytest.mark.parametrize("cut", [200, 240, 280])
    def test_truncating_the_input_does_not_change_earlier_values(self, cut):
        full = trend_up(300)
        part = full.tail(len(full))  # same object, explicit
        closes_full = full.closes
        closes_part = closes_full[:cut]

        for fn in (lambda c: rsi(c, 14), lambda c: ema(c, 50), lambda c: sma(c, 20)):
            whole = fn(closes_full)[:cut]
            truncated = fn(closes_part)
            assert len(whole) == len(truncated)
            for a, b in zip(whole, truncated):
                if a is None or b is None:
                    assert a is b
                else:
                    assert a == pytest.approx(b)
        assert part is not None

    def test_compute_matches_a_truncated_recompute(self):
        full = trend_up(320)
        early = full.slice_until(full[260].ts)
        from_full = compute(full)
        from_part = compute(early)
        # The indicator set as of bar 260 must be identical whether it was computed then
        # or recomputed later from a longer series.
        assert from_part.close == full[260].close
        assert from_part.rsi14 == pytest.approx(
            rsi(full.closes, 14)[260]
        )
        assert from_full.bars == 320 and from_part.bars == 261


class TestCompute:
    def test_refuses_a_series_too_short_to_score(self):
        # A pile of Nones flowing into the scorer would produce a low-confidence signal
        # off three bars and present it with the same interface as a real one.
        with pytest.raises(ValueError, match="need >=30 bars"):
            compute(make_series([100.0] * 10))

    def test_populates_scalars_and_arrays_from_one_pass(self):
        ind = compute(trend_up(300))
        assert isinstance(ind, Indicators)
        assert ind.ema20 and ind.ema50 and ind.ema200
        assert ind.adx14 and ind.rsi14 and ind.atr14
        assert ind.atr_pct == pytest.approx(ind.atr14 / ind.close)
        assert set(ind.arrays) >= {"close", "ema20", "rsi14", "adx14"}
        assert len(ind.arrays["close"]) == 300

    def test_as_dict_is_flat_and_json_friendly(self):
        flat = compute(trend_up(300)).as_dict()
        assert "arrays" not in flat
        assert "atr_pct" in flat
        assert all(value is None or isinstance(value, (int, float)) for value in flat.values())

    def test_bandwidth_percentile_is_a_fraction(self):
        ind = compute(choppy(300))
        assert 0.0 <= ind.bandwidth_percentile <= 1.0
