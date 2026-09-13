"""Ingestion: cross-validation, parsers, budgets and the synthetic world.

Nothing here touches the network. The HTTP client is exercised against fakes and the
parsers against fixtures, because a test that needs a provider up is a test that goes red
for reasons unrelated to the code -- and because these parsers are exactly the code that
has to keep working when a provider quietly renames a column.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import ClassVar

import pytest
from bullion_fixtures import make_series

from bullion.config import GOLD, SILVER, Settings
from bullion.providers import build
from bullion.providers.base import (
    CotHistory,
    CotReport,
    NotConfigured,
    ProviderError,
    Quote,
    RateLimited,
    StaleData,
    freshness,
    next_fomc_dates,
    recency_weight,
)
from bullion.providers.http import MonthlyBudget
from bullion.providers.news import KEYWORDS, assets_mentioned, relevant
from bullion.providers.positioning import parse_rows
from bullion.providers.prices import CrossValidatedPrices
from bullion.providers.synthetic import SyntheticCross, SyntheticWorld, synthetic_cot_csv

UTC = timezone.utc


class _FakePrimary:
    name = "fake-primary"

    def __init__(self, series):
        self.series = series

    def candles(self, asset, interval="1day", limit=400):
        return self.series

    def quote(self, asset):
        return Quote(asset, self.series.last.close, datetime.now(UTC), self.name)


class _FakeCross:
    name = "fake-cross"

    def __init__(self, price, fail=False):
        self.price = price
        self.fail = fail

    def quote(self, asset):
        if self.fail:
            raise ProviderError("cross feed is down")
        return Quote(asset, self.price, datetime.now(UTC), self.name)


def _fresh_series(last_close: float = 2400.0, bars: int = 60):
    """A daily series whose newest bar is the one currently forming."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    closes = [last_close * (1 + 0.0005 * i) for i in range(bars)]
    series = make_series(closes, start=now - timedelta(days=bars - 1))
    return series


class TestCrossValidation:
    def test_agreement_is_high_when_feeds_match(self):
        series = _fresh_series()
        prices = CrossValidatedPrices(
            primary=_FakePrimary(series), cross=_FakeCross(series.last.close)
        )
        closed, quality = prices.validated(GOLD)
        assert quality.agreement == pytest.approx(1.0)
        assert quality.sources == ("fake-primary", "fake-cross")
        # Scoring uses closed bars; the forming bar is excluded from what gets scored.
        assert len(closed) == len(series) - 1

    def test_tolerance_widens_for_a_feed_that_only_serves_closed_bars(self):
        # The bug this guards: comparing a live spot quote against yesterday's close with a
        # fixed 25bp tolerance flags a permanent false alarm, because gold moves more than
        # that inside a session. When the primary's freshest bar is a full interval old,
        # the allowance has to include a typical bar's worth of movement.
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        closed_only = make_series(
            [2400.0 * (1 + 0.0005 * i) for i in range(60)], start=now - timedelta(days=60)
        )
        drifted = closed_only.last.close * 1.004  # 40bp away: one session's drift
        prices = CrossValidatedPrices(
            primary=_FakePrimary(closed_only), cross=_FakeCross(drifted)
        )
        _, quality = prices.validated(GOLD)
        assert quality.disagreement_bp == pytest.approx(40, abs=1)
        assert quality.agreement == pytest.approx(1.0)
        assert "agree within" in quality.note

    def test_tolerance_stays_tight_on_a_just_opened_bar(self):
        # The other half of the same rule: when the primary's freshest price is the bar
        # that just opened, the two feeds really are quoting the same moment, so a 40bp
        # gap is a feed problem and must cost confidence.
        series = _fresh_series()
        prices = CrossValidatedPrices(
            primary=_FakePrimary(series), cross=_FakeCross(series.last.close * 1.004)
        )
        _, quality = prices.validated(GOLD)
        assert quality.agreement < 0.8
        assert "disagree" in quality.note

    def test_a_real_disagreement_reduces_agreement_and_says_so(self):
        series = _fresh_series()
        prices = CrossValidatedPrices(
            primary=_FakePrimary(series), cross=_FakeCross(series.last.close * 1.08)
        )
        _, quality = prices.validated(GOLD)
        assert quality.agreement < 0.3
        assert "disagree" in quality.note

    def test_a_failing_cross_feed_degrades_rather_than_raising(self):
        series = _fresh_series()
        prices = CrossValidatedPrices(
            primary=_FakePrimary(series), cross=_FakeCross(0, fail=True)
        )
        _, quality = prices.validated(GOLD)
        assert quality.cross_price is None
        assert "unavailable" in quality.note
        assert quality.agreement == 0.8

    def test_a_single_feed_is_capped_below_a_confirmed_one(self):
        series = _fresh_series()
        solo = CrossValidatedPrices(primary=_FakePrimary(series), cross=None)
        _, quality = solo.validated(GOLD)
        assert quality.agreement == 0.8
        assert "no cross-check" in quality.note

    def test_stale_bars_raise_rather_than_scoring(self):
        old = make_series([2400.0] * 60, start=datetime.now(UTC) - timedelta(days=120))
        prices = CrossValidatedPrices(primary=_FakePrimary(old), cross=None)
        with pytest.raises(StaleData, match="old"):
            prices.validated(GOLD)

    def test_empty_series_raises(self):
        prices = CrossValidatedPrices(primary=_FakePrimary(make_series([2400.0])), cross=None)
        with pytest.raises(ProviderError):
            prices.validated(GOLD)


class TestBudget:
    def test_exhaustion_raises_rate_limited(self, tmp_path):
        budget = MonthlyBudget("goldapi", 3, tmp_path / "b.json")
        for _ in range(3):
            budget.spend()
        assert budget.remaining == 0
        with pytest.raises(RateLimited, match="monthly budget"):
            budget.spend()

    def test_counter_persists_across_instances(self, tmp_path):
        path = tmp_path / "b.json"
        MonthlyBudget("goldapi", 10, path).spend(4)
        # A redeploy must not hand the process a fresh allowance.
        assert MonthlyBudget("goldapi", 10, path).used == 4


class TestCotParser:
    HEADER_SNAKE: ClassVar[dict[str, str]] = {
        "market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.",
        "report_date_as_yyyy_mm_dd": "2026-09-08",
        "noncomm_positions_long_all": "250000",
        "noncomm_positions_short_all": "90000",
        "open_interest_all": "450000",
    }

    def test_parses_socrata_style_rows(self):
        history = parse_rows(GOLD, [self.HEADER_SNAKE])
        assert history.latest.net == 160_000
        assert history.latest.net_pct_oi == pytest.approx(160_000 / 450_000)

    def test_parses_the_csv_column_spelling_too(self):
        csv_style = {
            "Market_and_Exchange_Names": "GOLD - COMMODITY EXCHANGE INC.",
            "Report_Date_as_YYYY-MM-DD": "2026-09-08",
            "NonComm_Positions_Long_All": "250,000",
            "NonComm_Positions_Short_All": "90,000",
            "Open_Interest_All": "450,000",
        }
        history = parse_rows(GOLD, [csv_style])
        assert history.latest.noncomm_long == 250_000

    def test_other_markets_are_filtered_out(self):
        other = {
            **self.HEADER_SNAKE,
            "market_and_exchange_names": "WHEAT - CHICAGO BOARD OF TRADE",
        }
        with pytest.raises(ProviderError, match="no CFTC rows matched"):
            parse_rows(GOLD, [other])

    def test_a_renamed_column_fails_loudly_rather_than_scoring_zeros(self):
        broken = {k: v for k, v in self.HEADER_SNAKE.items() if "long" not in k}
        with pytest.raises(ProviderError, match="no CFTC rows matched"):
            parse_rows(GOLD, [broken])

    def test_reports_are_sorted_oldest_first_and_deduped(self):
        rows = [
            dict(self.HEADER_SNAKE, report_date_as_yyyy_mm_dd="2026-09-08"),
            dict(self.HEADER_SNAKE, report_date_as_yyyy_mm_dd="2026-09-01"),
            dict(self.HEADER_SNAKE, report_date_as_yyyy_mm_dd="2026-09-08"),
        ]
        history = parse_rows(GOLD, rows)
        assert len(history.reports) == 2
        assert history.reports[0].report_date < history.reports[1].report_date

    def test_the_synthetic_csv_round_trips_through_the_real_parser(self):
        import csv
        import io

        text = synthetic_cot_csv(GOLD, weeks=8)
        rows = list(csv.DictReader(io.StringIO(text)))
        history = parse_rows(GOLD, rows)
        assert len(history.reports) == 8


class TestCotHistory:
    def test_crowding_needs_enough_history(self):
        short = CotHistory(
            GOLD,
            tuple(
                CotReport(GOLD, datetime(2026, 1, 1).date() + timedelta(weeks=i), 1, 1, 100)
                for i in range(10)
            ),
        )
        assert short.crowding_percentile() is None


class TestNewsFilter:
    @pytest.mark.parametrize(
        "title",
        [
            "Gold hits a record as real yields fall",
            "FOMC minutes show a hawkish tilt",
            "Dollar index breaks down after soft CPI",
            "Central banks kept buying bullion in August",
        ],
    )
    def test_relevant_headlines_pass(self, title):
        assert relevant(title)

    @pytest.mark.parametrize(
        "title",
        [
            "Golden State Warriors win again",
            "Silver Lake raises a new fund",
            "Goldman names a new partner class",
        ],
    )
    def test_lookalikes_are_excluded(self, title):
        assert not relevant(title)

    def test_a_lookalike_that_also_names_the_market_is_kept(self):
        assert relevant("Goldman raises its bullion forecast on COMEX flows")

    def test_assets_mentioned_distinguishes_macro_from_metal_stories(self):
        assert assets_mentioned("Gold rallies") == (GOLD,)
        assert assets_mentioned("Silver squeezes higher") == (SILVER,)
        assert assets_mentioned("Gold and silver both gain") == (GOLD, SILVER)
        # A Fed story names no metal and must still be scored, against both.
        assert assets_mentioned("Fed holds rates steady") == ()

    def test_keyword_list_covers_the_macro_drivers_not_just_the_metals(self):
        for term in ("real yield", "fomc", "dollar index", "inflation"):
            assert term in KEYWORDS


class TestHelpers:
    def test_recency_weight_halves_at_the_half_life(self):
        assert recency_weight(0) == 1.0
        assert recency_weight(18, 18) == pytest.approx(0.5)
        assert recency_weight(36, 18) == pytest.approx(0.25)

    def test_freshness_is_one_while_on_time_then_decays_to_zero(self):
        assert freshness(0, 100) == 1.0
        assert freshness(100, 100) == 1.0
        assert freshness(250, 100) == pytest.approx(0.5)
        assert freshness(400, 100) == 0.0

    def test_fomc_calendar_returns_future_dates_only(self):
        events = next_fomc_dates(datetime(2026, 6, 1, tzinfo=UTC), count=3)
        assert len(events) == 3
        assert all(e.ts > datetime(2026, 5, 1, tzinfo=UTC) for e in events)
        assert events[0].kind == "fomc"

    def test_quote_spread_is_none_without_both_sides(self):
        assert Quote(GOLD, 2400.0, datetime.now(UTC), "x").spread_bp is None
        quoted = Quote(GOLD, 2400.0, datetime.now(UTC), "x", bid=2399.5, ask=2400.5)
        assert quoted.spread_bp == pytest.approx(10_000 * 1.0 / 2400.0)


class TestSyntheticWorld:
    def test_is_deterministic_for_a_seed(self):
        a = SyntheticWorld(seed=11, asof=datetime(2026, 9, 1, tzinfo=UTC))
        b = SyntheticWorld(seed=11, asof=datetime(2026, 9, 1, tzinfo=UTC))
        assert a.candles(GOLD, "1day", 50).closes == b.candles(GOLD, "1day", 50).closes

    def test_different_limits_slice_one_path(self):
        # The bug this guards: generating per-request made candles(120) and candles(400)
        # end at different prices, which silently broke cross-feed validation.
        world = SyntheticWorld(seed=3)
        short = world.candles(GOLD, "1day", 100)
        long_ = world.candles(GOLD, "1day", 400)
        assert short.last.close == long_.last.close
        assert short.closes == long_.closes[-100:]

    def test_prices_stay_in_a_plausible_range(self):
        world = SyntheticWorld(seed=5)
        gold = world.candles(GOLD, "1day", 1000).closes
        silver = world.candles(SILVER, "1day", 1000).closes
        assert min(gold) > 900 and max(gold) < 6000
        assert min(silver) > 8 and max(silver) < 90

    def test_silver_is_more_volatile_than_gold(self):
        from bullion.series import returns, stdev

        world = SyntheticWorld(seed=9)
        gold = stdev(returns(world.candles(GOLD, "1day", 600).closes))
        silver = stdev(returns(world.candles(SILVER, "1day", 600).closes))
        assert silver > gold

    def test_refuses_to_exceed_its_generated_history(self):
        world = SyntheticWorld(seed=1, history_bars=200)
        with pytest.raises(ProviderError, match="history_bars"):
            world.candles(GOLD, "1day", 500)

    def test_headlines_are_labelled_synthetic(self):
        world = SyntheticWorld(seed=1)
        headlines = world.headlines(world.asof - timedelta(days=3), 20)
        assert headlines
        assert all(h.title.startswith("[SYNTHETIC]") for h in headlines)
        assert all(h.provider == "synthetic" for h in headlines)

    def test_cot_history_degrades_rather_than_raising_on_a_short_world(self):
        world = SyntheticWorld(seed=1, history_bars=400)
        history = world.history(GOLD, 156)
        assert len(history.reports) >= 26

    def test_macro_cpi_is_monthly_not_daily(self):
        world = SyntheticWorld(seed=1)
        cpi = world.series("CPIAUCSL", 60)
        gap = (cpi.points[-1].day - cpi.points[-2].day).days
        assert gap > 20

    def test_the_cross_feed_is_close_but_not_identical(self):
        world = SyntheticWorld(seed=4)
        cross = SyntheticCross(world)
        spot = cross.quote(GOLD).price
        close = world.candles(GOLD, "1day", 100).last.close
        assert spot != close
        assert abs(spot - close) / close < 0.01

    def test_unknown_assets_and_series_raise(self):
        world = SyntheticWorld()
        with pytest.raises(ProviderError):
            world.candles("XPT/USD")
        with pytest.raises(ProviderError):
            world.series("NOPE")


class TestBuild:
    def test_no_keys_falls_back_to_synthetic_and_says_so(self):
        feeds = build(Settings(allow_synthetic=True))
        assert feeds.synthetic is True
        assert any("SYNTHETIC" in note for note in feeds.notes)
        assert feeds.available_layers == {"technical", "macro", "sentiment", "positioning"}

    def test_production_posture_refuses_to_invent_prices(self):
        with pytest.raises(NotConfigured, match="TWELVEDATA_API_KEY"):
            build(Settings(allow_synthetic=False))

    def test_a_missing_news_key_darkens_only_that_layer(self):
        feeds = build(Settings(twelvedata_key="x", allow_synthetic=False))
        assert feeds.synthetic is False
        assert feeds.news is None
        assert "sentiment" not in feeds.available_layers
        assert {"technical", "macro", "positioning"} <= feeds.available_layers
        assert any("news key" in note for note in feeds.notes)

    def test_describe_is_json_serialisable(self):
        json.dumps(build(Settings(allow_synthetic=True)).describe())
