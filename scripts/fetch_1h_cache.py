"""
fetch_1h_cache.py — 업비트 1h OHLCV 캐시 수집 스크립트.

fast_backtest_scalping.py 실행 전 1h 봉 데이터를 .cache/ohlcv/ 에 저장한다.

실행 예시:
  python scripts/fetch_1h_cache.py              # 거래대금 상위 30개, 200봉
  python scripts/fetch_1h_cache.py --top 50     # 상위 50개
  python scripts/fetch_1h_cache.py --candles 500
  python scripts/fetch_1h_cache.py --force      # 캐시 무시 강제 재수집
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import ccxt.async_support as ccxt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_ROOT     = Path(__file__).parent.parent
CACHE_DIR = _ROOT / ".cache" / "ohlcv"
TIMEFRAME = "1h"


def _cache_path(symbol: str) -> Path:
    safe_sym = symbol.replace("/", "_")
    return CACHE_DIR / f"{safe_sym}_{TIMEFRAME}.json"


def _load_cache(symbol: str, min_count: int) -> list[list] | None:
    path = _cache_path(symbol)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list) and len(data) >= min_count:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _save_cache(symbol: str, ohlcv: list[list]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol)
    path.write_text(json.dumps(ohlcv), encoding="utf-8")


async def fetch_top_symbols(exchange: ccxt.Exchange, top_n: int) -> list[str]:
    await exchange.load_markets()
    krw_symbols = [sym for sym in exchange.symbols if sym.endswith("/KRW")]
    logger.info("KRW 마켓 심볼 %d개 필터링 완료", len(krw_symbols))
    tickers = await exchange.fetch_tickers(krw_symbols)
    krw_tickers = {
        sym: t for sym, t in tickers.items()
        if sym.endswith("/KRW") and t.get("quoteVolume")
    }
    sorted_syms = sorted(
        krw_tickers,
        key=lambda s: krw_tickers[s]["quoteVolume"],
        reverse=True,
    )
    return sorted_syms[:top_n]


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="업비트 1h OHLCV 캐시 수집 (fast_backtest_scalping.py 전처리)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--top", type=int, default=30, help="거래대금 상위 N개 심볼")
    parser.add_argument("--candles", type=int, default=200, help="심볼당 수집할 봉 수")
    parser.add_argument("--force", action="store_true", help="캐시 무시 강제 재수집")
    args = parser.parse_args()

    exchange = ccxt.upbit({"enableRateLimit": True})

    try:
        logger.info("거래대금 상위 %d개 심볼 조회 중...", args.top)
        top_symbols = await fetch_top_symbols(exchange, args.top)
        logger.info("대상 심볼 %d개: %s", len(top_symbols), ", ".join(top_symbols[:5]) + " ...")

        _UPBIT_MAX = 200
        success = 0

        for i, sym in enumerate(top_symbols, 1):
            try:
                if not args.force:
                    cached = _load_cache(sym, args.candles)
                    if cached is not None:
                        logger.info("  %2d/%d  %-14s: %d봉 [캐시]", i, len(top_symbols), sym, len(cached))
                        success += 1
                        continue

                # 페이지네이션으로 수집
                collected: dict[int, list] = {}
                params: dict[str, str] = {}

                while len(collected) < args.candles:
                    chunk = await exchange.fetch_ohlcv(
                        sym, timeframe=TIMEFRAME, limit=_UPBIT_MAX, params=params
                    )
                    if not chunk:
                        break

                    new_count = 0
                    for candle in chunk:
                        if candle[0] not in collected:
                            collected[candle[0]] = candle
                            new_count += 1

                    if new_count == 0 or len(chunk) < _UPBIT_MAX:
                        break

                    oldest_ts_ms = min(collected.keys()) - 1
                    oldest_dt    = datetime.fromtimestamp(oldest_ts_ms / 1000, tz=timezone.utc)
                    params = {"to": oldest_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")}
                    await asyncio.sleep(0.1)

                ohlcv = sorted(collected.values(), key=lambda c: c[0])[-args.candles:]
                _save_cache(sym, ohlcv)
                logger.info("  %2d/%d  %-14s: %d봉 [수집 완료]", i, len(top_symbols), sym, len(ohlcv))
                success += 1

            except Exception as exc:
                logger.warning("  %2d/%d  %-14s: 수집 실패 — %s", i, len(top_symbols), sym, exc)

        logger.info("캐시 저장 완료: %d/%d개 → %s", success, len(top_symbols), CACHE_DIR)

    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
