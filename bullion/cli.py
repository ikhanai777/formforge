"""Command line: ``signal``, ``ingest``, ``grade``, ``backtest``, ``scorecard``, ``serve``.

Written so that the first command anyone runs works with no credentials and no database:
``bullion signal`` on a fresh checkout builds the synthetic world, scores it, and prints a
full explained call with a SYNTHETIC banner across the top. Getting from clone to "I can
see what this thing does" without a signup flow is worth more than any README section.

Every command that can mislead says so in its output. ``signal`` prints the synthetic
banner; ``backtest`` refuses to present fitted weights from generated data as a result;
``scorecard`` excludes synthetic rows unless asked.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

from . import scorecard as scorecard_module
from .config import ASSETS, GOLD, SILVER, Settings, load_settings, weights_to_json
from .disclosure import DISCLOSURE, SHORT_DISCLOSURE
from .providers import build as build_feeds
from .store import Store

log = logging.getLogger("bullion.cli")

BANNER = "=" * 78


def _settings(args: argparse.Namespace) -> Settings:
    settings = load_settings()
    if getattr(args, "store", None):
        settings = replace(settings, store_path=Path(args.store).expanduser())
    return settings


def _assets(args: argparse.Namespace) -> tuple[str, ...]:
    if getattr(args, "asset", None):
        return tuple(args.asset)
    return ASSETS


def _synthetic_banner(feeds) -> None:
    if not feeds.synthetic:
        return
    print(BANNER)
    print("  SYNTHETIC DATA — prices, macro, positioning and news are GENERATED.")
    print("  Nothing below reflects any real market. Set TWELVEDATA_API_KEY for live data.")
    print(BANNER)


# ---------------------------------------------------------------------------
# signal
# ---------------------------------------------------------------------------


def cmd_signal(args: argparse.Namespace) -> int:
    """Score every asset once and print the explained call. Read-only by default."""
    from .pipeline import run_cycle

    settings = _settings(args)
    feeds = build_feeds(settings)
    _synthetic_banner(feeds)

    store = Store(settings.store_path) if args.persist else None
    result = run_cycle(
        settings,
        feeds=feeds,
        store=store,
        assets=_assets(args),
        interval=args.interval,
        persist=args.persist,
    )

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, default=str))
        return 0

    for asset, outcome in result.results.items():
        print()
        if outcome.error:
            print(f"{asset}: FAILED — {outcome.error}")
            continue
        signal = outcome.signal
        assert signal is not None
        print(f"{asset}  {signal.state.value}")
        print(
            f"  confidence      {signal.confidence.value * 100:.0f}%  "
            f"(composite {signal.composite_score:+.2f})"
        )
        print(f"  price           {signal.price_at_signal:,.2f}")
        if signal.levels.stop is not None:
            print(
                f"  invalidation    {signal.levels.stop:,.2f}   "
                f"target {signal.levels.target:,.2f}   "
                f"R:R {signal.levels.risk_reward:.1f}"
            )
            print(f"  level basis     {signal.levels.basis}")
        print(f"  regime          {signal.regime.regime.value} — {signal.regime.reason}")
        print(f"  confidence from {signal.confidence.explain()}")
        print("  reasoning:")
        for name, line in signal.reasoning().items():
            weight = signal.weights.get(name)
            tag = f"[w={weight:.0%}]" if weight else "[dark]"
            print(f"    {name:<12} {tag:>9} {line}")
        for note in signal.notes:
            print(f"  note            {note}")
        if outcome.failures:
            for layer, why in outcome.failures.items():
                print(f"  ingest failure  {layer}: {why}")
        for alert in outcome.alerts:
            print(f"  ALERT           [{alert.kind}] {alert.title}")

    print()
    print(f"({SHORT_DISCLOSURE})")
    return 0


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


def cmd_ingest(args: argparse.Namespace) -> int:
    """Fetch and store bars without scoring. Used to build backtest history cheaply."""
    settings = _settings(args)
    feeds = build_feeds(settings)
    _synthetic_banner(feeds)
    store = Store(settings.store_path)

    total = 0
    for asset in _assets(args):
        series = feeds.prices.candles(asset, args.interval, args.bars)
        written = store.write_candles(series.closed())
        total += written
        print(f"{asset}: stored {written} {args.interval} bars (source {series.source})")
    print(f"store: {settings.store_path}")
    print(json.dumps(store.counts(), indent=2))
    return 0 if total else 1


# ---------------------------------------------------------------------------
# grade + scorecard
# ---------------------------------------------------------------------------


def cmd_grade(args: argparse.Namespace) -> int:
    """Resolve matured signals against stored prices. Safe to run on a schedule."""
    settings = _settings(args)
    store = Store(settings.store_path)
    counts = scorecard_module.grade_pending(store, _assets(args), args.interval)
    print(json.dumps(counts, indent=2))
    return 0


def cmd_scorecard(args: argparse.Namespace) -> int:
    """Print the live track record."""
    settings = _settings(args)
    store = Store(settings.store_path)
    card = scorecard_module.build(store, include_synthetic=args.include_synthetic)

    if args.json:
        print(json.dumps(card.as_dict(), indent=2))
        return 0

    def row(label: str, bucket) -> None:
        if bucket.trades == 0:
            print(f"  {label:<18} no graded signals")
            return
        low, high = bucket.win_rate_ci
        flag = "" if bucket.trades >= scorecard_module.MIN_DRIFT_SAMPLE else "  (thin sample)"
        print(
            f"  {label:<18} {bucket.win_rate * 100:5.1f}%  "
            f"[{low * 100:.0f}–{high * 100:.0f}%]  "
            f"n={bucket.trades:<4} expectancy {bucket.expectancy_r:+.2f}R{flag}"
        )

    print("TRACK RECORD (self-graded, stop-first on ties)")
    row("overall", card.overall)
    print(" by asset")
    for name, bucket in card.by_asset.items():
        row(name, bucket)
    print(" by strength")
    for name, bucket in card.by_strength.items():
        row(name, bucket)
    print(" by regime")
    for name, bucket in card.by_regime.items():
        row(name, bucket)
    print(" by confidence")
    for name, bucket in card.by_confidence.items():
        row(name, bucket)
    print(f"\n  pending (not yet matured): {card.pending}")
    if card.excluded_synthetic:
        print(f"  excluded synthetic rows:   {card.excluded_synthetic}")

    verdict = scorecard_module.drift_from_store(store)
    print(f"\nDRIFT: {verdict.verdict} — {verdict.detail}")
    print(f"\n({SHORT_DISCLOSURE})")
    return 0


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------


def cmd_backtest(args: argparse.Namespace) -> int:
    """Walk-forward backtest with per-fold weight fitting."""
    from . import backtest as bt

    settings = _settings(args)
    feeds = build_feeds(settings)
    _synthetic_banner(feeds)
    store = Store(settings.store_path) if args.persist else None

    fitted: dict[str, object] = {}
    for asset in _assets(args):
        print(f"\n{asset}: building frames...")
        history = (
            bt.history_from_store(store, asset, args.interval, args.bars)
            if args.from_store and store
            else bt.history_from_feeds(asset, feeds, args.interval, args.bars)
        )
        if not len(history.series):
            print(f"{asset}: no price history available")
            continue
        try:
            result = bt.walk_forward(
                history,
                settings,
                train_bars=args.train,
                test_bars=args.test,
                horizon=args.horizon,
                step=args.step,
            )
        except ValueError as exc:
            print(f"{asset}: {exc}")
            continue

        oos = result.oos_metrics
        print(f"{asset}: {len(result.folds)} folds, {oos['trades']} out-of-sample signals")
        if oos["trades"]:
            low, high = oos["win_rate_ci"]
            print(
                f"  OOS win rate     {oos['win_rate'] * 100:.1f}%  "
                f"[{low * 100:.0f}–{high * 100:.0f}%]"
            )
            print(f"  OOS expectancy   {oos['expectancy_r']:+.3f}R per signal")
            factor = oos["profit_factor"]
            print(f"  profit factor    {factor:.2f}" if factor else "  profit factor    n/a")
            print(f"  max drawdown     {oos['max_drawdown_r']:.2f}R")
            gap = result.overfit_gap
            if gap is not None:
                print(
                    f"  overfit gap      {gap:+.3f}R "
                    "(in-sample minus out-of-sample expectancy)"
                )
            print("  by strength:")
            for name, group in result.by_strength.items():
                if group["trades"]:
                    print(
                        f"    {name:<8} n={group['trades']:<4} "
                        f"win {group['win_rate'] * 100:.0f}%  "
                        f"expectancy {group['expectancy_r']:+.2f}R"
                    )
        print(f"  pooled weights   {result.pooled_weights.as_dict()}")
        fitted[asset] = result.pooled_weights

        if store is not None:
            bt.persist(store, result, args.interval)

        if args.json:
            print(json.dumps(result.as_dict(), indent=2, default=str))

    if fitted and not feeds.synthetic:
        print("\nTo adopt these weights, set:")
        print(f"  BULLION_WEIGHTS='{weights_to_json(fitted)}'  # noqa: fitted out-of-sample")
    elif fitted:
        print(
            "\nNot printing an adoptable weight set: these were fitted on SYNTHETIC data "
            "and describe the generator, not any market."
        )
    return 0


# ---------------------------------------------------------------------------
# serve / demo
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the API and dashboard."""
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Install the extra: pip install 'bullion[api]'")
        return 2
    from .api.app import create_app

    settings = _settings(args)
    app = create_app(settings)
    print(f"dashboard: http://{args.host}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Fill a store with synthetic history, signals and grades, then print the scorecard.

    What this is for: the dashboard's track-record view is the credibility anchor of the
    whole product, and an empty one demos badly. This populates it in a few seconds -- every
    row flagged synthetic, so the real scorecard stays empty until real signals are graded.
    """
    from . import backtest as bt
    from .pipeline import evaluate
    from .providers.synthetic import SyntheticWorld
    from .scoring.llm_news import LexiconScorer

    settings = _settings(args)
    store = Store(settings.store_path)
    world = SyntheticWorld(seed=args.seed, history_bars=max(600, args.bars + 300))
    feeds = build_feeds(replace(settings, twelvedata_key=None, allow_synthetic=True), world)
    _synthetic_banner(feeds)

    scorer = LexiconScorer()
    for asset in _assets(args):
        written = 0
        history = bt.history_from_feeds(asset, feeds, "1day", args.bars + 300)
        store.write_candles(history.series)
        # Walk the tape, scoring every nth bar, and store the actionable calls so the
        # grader and the scorecard have something real (if generated) to work with.
        bars = history.series.candles
        for index in range(260, len(bars) - 8, args.step):
            when = bars[index].ts
            data = history.as_of(when)
            try:
                signal = evaluate(asset, data, settings, scorer, None, now=when)
            except ValueError:
                continue
            if signal.direction == 0:
                continue
            store.write_signal(signal)
            written += 1
        print(f"{asset}: wrote {written} synthetic signals")

    # Headlines too, so the timeline strip has a sentiment channel to draw. They are
    # asset-agnostic, so one pull covers both cards.
    from datetime import timedelta as _timedelta

    from .pipeline import persist_headlines

    headlines = feeds.news.headlines(world.asof - _timedelta(days=90), 400)
    print(f"headlines: stored {persist_headlines(store, headlines, scorer)}")

    counts = scorecard_module.grade_pending(store, _assets(args))
    print(f"graded: {json.dumps(counts)}")
    print()
    return cmd_scorecard(
        argparse.Namespace(
            store=args.store, json=False, include_synthetic=True, asset=list(_assets(args))
        )
    )


def cmd_disclosure(args: argparse.Namespace) -> int:
    print(DISCLOSURE)
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bullion",
        description=(
            "Confidence-scored, explainable gold and silver signals. "
            "Informational only, not financial advice."
        ),
    )
    parser.add_argument(
        "--store", help="path to the sqlite store (default ~/.bullion/bullion.db)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--asset",
            action="append",
            choices=[GOLD, SILVER],
            help="restrict to one asset (repeatable)",
        )
        p.add_argument("--interval", default="1day", help="bar interval (default 1day)")

    signal = sub.add_parser("signal", help="score every asset now and explain the call")
    add_common(signal)
    signal.add_argument("--json", action="store_true")
    signal.add_argument(
        "--persist",
        action="store_true",
        help="write the signal and alerts to the store (off by default, so inspecting "
        "the engine cannot pollute the track record)",
    )
    signal.set_defaults(func=cmd_signal)

    ingest = sub.add_parser("ingest", help="fetch and store bars without scoring")
    add_common(ingest)
    ingest.add_argument("--bars", type=int, default=1200)
    ingest.set_defaults(func=cmd_ingest)

    grade = sub.add_parser("grade", help="resolve matured signals against stored prices")
    add_common(grade)
    grade.set_defaults(func=cmd_grade)

    card = sub.add_parser("scorecard", help="print the live track record")
    add_common(card)
    card.add_argument("--json", action="store_true")
    card.add_argument(
        "--include-synthetic",
        action="store_true",
        help="include signals generated from synthetic data (off by default)",
    )
    card.set_defaults(func=cmd_scorecard)

    back = sub.add_parser("backtest", help="walk-forward backtest with weight fitting")
    add_common(back)
    back.add_argument("--bars", type=int, default=1200)
    back.add_argument("--train", type=int, default=250, help="training bars per fold")
    back.add_argument("--test", type=int, default=60, help="out-of-sample bars per fold")
    back.add_argument("--horizon", type=int, default=7, help="bars before a timeout grade")
    back.add_argument("--step", type=int, default=1, help="score every nth bar")
    back.add_argument(
        "--from-store", action="store_true", help="replay stored bars, no network"
    )
    back.add_argument("--persist", action="store_true", help="store the run")
    back.add_argument("--json", action="store_true")
    back.set_defaults(func=cmd_backtest)

    serve = sub.add_parser("serve", help="run the API and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    demo = sub.add_parser(
        "demo", help="populate a store with synthetic signals and grade them"
    )
    add_common(demo)
    demo.add_argument("--bars", type=int, default=700)
    demo.add_argument("--step", type=int, default=3)
    demo.add_argument("--seed", type=int, default=7)
    demo.set_defaults(func=cmd_demo)

    disc = sub.add_parser("disclosure", help="print the full disclosure")
    disc.set_defaults(func=cmd_disclosure)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as exc:
        if args.verbose:
            raise
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("run with -v for a traceback", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
