"""
Admin Dashboard 연동용 통계 API 라우터.

엔드포인트:
    GET /api/admin/stats/engines     — 엔진별 거래 통계 (총 거래 횟수, 승률, 누적 손익)
    GET /api/admin/stats/close-types — 청산 사유별 발생 비율
    GET /api/admin/slippage          — 평균 슬리피지 (expected_price vs sell_price)

보안:
    모든 엔드포인트는 X-Admin-API-Key 헤더 인증 필수.
    settings.admin_api_key 가 설정되지 않았거나 헤더 값이 불일치하면 403 반환.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.trade_history import TradeHistory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# ------------------------------------------------------------------
# 인증 의존성
# ------------------------------------------------------------------

_API_KEY_HEADER = APIKeyHeader(name="X-Admin-API-Key", auto_error=False)


async def get_api_key(
    api_key: Annotated[str | None, Security(_API_KEY_HEADER)],
) -> str:
    """X-Admin-API-Key 헤더를 검증한다.

    Args:
        api_key: 요청 헤더에서 추출한 API 키.

    Raises:
        HTTPException 403: 키가 없거나 일치하지 않는 경우.

    Returns:
        검증된 API 키 문자열.
    """
    if not settings.admin_api_key:
        logger.error("Admin API: ADMIN_API_KEY 환경변수 미설정 — 모든 요청 거부")
        raise HTTPException(status_code=403, detail="Admin API key not configured.")

    if not api_key or api_key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Invalid or missing Admin API key.")

    return api_key


# ------------------------------------------------------------------
# GET /api/admin/stats/engines
# ------------------------------------------------------------------


@router.get("/stats/engines", dependencies=[Depends(get_api_key)])
async def get_engine_stats(db: AsyncSession = Depends(get_db)) -> dict:
    """엔진별 거래 통계를 반환한다.

    trade_style 컬럼을 기준으로 그룹화하며, is_ai_managed=True 레코드만 집계한다.
    승률은 profit_pct > 0 인 건수 / 전체 건수로 계산한다.

    Returns:
        engines: 엔진별 통계 딕셔너리 목록.
            - engine: 엔진명 (SWING / SCALPING / MAJOR_TREND / 기타)
            - total_trades: 총 거래 횟수
            - win_rate_pct: 승률 (%)
            - total_profit_krw: 누적 손익 (KRW)
    """
    result = await db.execute(
        select(
            TradeHistory.trade_style,
            func.count(TradeHistory.id).label("total_trades"),
            func.sum(TradeHistory.profit_krw).label("total_profit_krw"),
        )
        .where(TradeHistory.is_ai_managed.is_(True))
        .group_by(TradeHistory.trade_style)
    )
    rows = result.all()

    engines: list[dict] = []
    for row in rows:
        total = int(row.total_trades or 0)
        profit_krw = float(row.total_profit_krw or 0.0)
        engines.append(
            {
                "engine": row.trade_style or "UNKNOWN",
                "total_trades": total,
                "win_rate_pct": None,  # 하단 win_rate 쿼리로 채움
                "total_profit_krw": round(profit_krw, 2),
            }
        )

    # win_rate 별도 쿼리 (SQLAlchemy CASE 집계 방언 독립적 처리)
    win_result = await db.execute(
        select(
            TradeHistory.trade_style,
            func.count(TradeHistory.id).label("win_count"),
        )
        .where(
            TradeHistory.is_ai_managed.is_(True),
            TradeHistory.profit_pct > 0,
        )
        .group_by(TradeHistory.trade_style)
    )
    win_map: dict[str, int] = {
        (row.trade_style or "UNKNOWN"): int(row.win_count or 0)
        for row in win_result.all()
    }

    for item in engines:
        total = item["total_trades"]
        wins  = win_map.get(item["engine"], 0)
        item["win_rate_pct"] = round(wins / total * 100, 1) if total > 0 else 0.0

    logger.info("Admin stats/engines 조회: %d 엔진", len(engines))
    return {"engines": engines}


# ------------------------------------------------------------------
# GET /api/admin/stats/close-types
# ------------------------------------------------------------------


@router.get("/stats/close-types", dependencies=[Depends(get_api_key)])
async def get_close_type_stats(db: AsyncSession = Depends(get_db)) -> dict:
    """청산 사유별 발생 건수 및 비율을 반환한다.

    close_type 컬럼을 Group By 한다. NULL 은 "UNKNOWN" 으로 표시한다.

    Returns:
        close_types: 청산 사유별 통계 목록.
            - close_type: 사유 (TP_HIT / SL_HIT / AI_FORCE_SELL / MANUAL_OVERRIDE / UNKNOWN)
            - count: 발생 건수
            - ratio_pct: 전체 대비 비율 (%)
    """
    result = await db.execute(
        select(
            func.coalesce(TradeHistory.close_type, "UNKNOWN").label("close_type"),
            func.count(TradeHistory.id).label("count"),
        )
        .group_by(func.coalesce(TradeHistory.close_type, "UNKNOWN"))
        .order_by(func.count(TradeHistory.id).desc())
    )
    rows = result.all()

    total_count = sum(int(r.count or 0) for r in rows)
    close_types: list[dict] = []
    for row in rows:
        cnt = int(row.count or 0)
        close_types.append(
            {
                "close_type": row.close_type,
                "count": cnt,
                "ratio_pct": round(cnt / total_count * 100, 1) if total_count > 0 else 0.0,
            }
        )

    logger.info("Admin stats/close-types 조회: 총 %d 건", total_count)
    return {"total_count": total_count, "close_types": close_types}


# ------------------------------------------------------------------
# GET /api/admin/slippage
# ------------------------------------------------------------------


@router.get("/slippage", dependencies=[Depends(get_api_key)])
async def get_slippage_stats(db: AsyncSession = Depends(get_db)) -> dict:
    """예상 체결가(expected_price) 대비 실제 매도가(sell_price) 평균 슬리피지를 반환한다.

    expected_price 와 sell_price 가 모두 NULL 이 아닌 레코드만 집계한다.
    슬리피지(%) = (sell_price - expected_price) / expected_price × 100
    양수 = 예상보다 유리한 체결 (슬리피지 이득)
    음수 = 예상보다 불리한 체결 (슬리피지 손해)

    Returns:
        avg_slippage_pct: 평균 슬리피지 (%)
        sample_count: 집계에 사용된 레코드 수
    """
    result = await db.execute(
        select(
            func.avg(
                (TradeHistory.sell_price - TradeHistory.expected_price)
                / TradeHistory.expected_price
                * 100
            ).label("avg_slippage_pct"),
            func.count(TradeHistory.id).label("sample_count"),
        )
        .where(
            TradeHistory.expected_price.is_not(None),
            TradeHistory.sell_price.is_not(None),
            TradeHistory.expected_price > 0,
        )
    )
    row = result.one_or_none()

    avg_slip  = float(row.avg_slippage_pct or 0.0) if row else 0.0
    sample_n  = int(row.sample_count or 0)         if row else 0

    logger.info(
        "Admin slippage 조회: avg=%.4f%% sample=%d", avg_slip, sample_n
    )
    return {
        "avg_slippage_pct": round(avg_slip, 4),
        "sample_count": sample_n,
    }
