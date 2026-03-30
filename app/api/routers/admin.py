"""
Admin Dashboard 연동용 통계 API 라우터.

엔드포인트:
    GET /api/admin/overview            — KPI 지표 + 최근 7일 손익 추이
    GET /api/admin/trade-logs          — 거래 이력 페이징 조회
    GET /api/admin/stats/engines       — 엔진별 거래 통계 (총 거래 횟수, 승률, 누적 손익)
    GET /api/admin/stats/close-types   — 청산 사유별 발생 비율
    GET /api/admin/slippage            — 평균 슬리피지 (expected_price vs sell_price)

보안:
    모든 엔드포인트는 X-Admin-API-Key 헤더 인증 필수.
    settings.admin_api_key 가 설정되지 않았거나 헤더 값이 불일치하면 403 반환.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Security
from fastapi.security.api_key import APIKeyHeader
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.bot_setting import BotSetting
from app.models.trade_history import TradeHistory
from app.models.user import User

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
# GET /api/admin/overview  (P1 신규)
# ------------------------------------------------------------------


@router.get("/overview", dependencies=[Depends(get_api_key)])
async def get_overview(db: AsyncSession = Depends(get_db)) -> dict:
    """Admin Dashboard KPI 지표와 최근 7일 일별 손익 추이를 반환한다.

    KPI 항목:
        - active_users: AI 실전 또는 모의투자 활성 유저 수
        - aum_krw: 현재 실행 중인 포지션 투자 원금 합계 (KRW)
        - today_pnl_krw: 오늘(UTC 기준) 실전 거래 손익 합계 (KRW)
        - system_alerts: null 고정 (향후 확장용)

    daily_pnl:
        최근 7일 날짜별 실전 거래 손익 합계. 데이터 없는 날은 0.0으로 채운다.

    Returns:
        kpi: KPI 딕셔너리.
        daily_pnl: 최근 7일 날짜별 손익 목록 (오래된 날짜 → 최근 날짜 순).
    """
    # 1. active_users — AI 실전 또는 모의투자 활성 유저 COUNT
    active_result = await db.execute(
        select(func.count(User.user_id)).where(
            User.is_active.is_(True),
            (User.ai_mode_enabled.is_(True) | User.ai_paper_mode_enabled.is_(True)),
        )
    )
    active_users = int(active_result.scalar() or 0)

    # 2. aum_krw — 실행 중인 포지션의 투자 원금 합계
    aum_result = await db.execute(
        select(func.sum(BotSetting.buy_amount_krw)).where(
            BotSetting.is_running.is_(True),
            BotSetting.buy_price.is_not(None),
        )
    )
    aum_krw = float(aum_result.scalar() or 0.0)

    # 3. today_pnl_krw — 오늘(UTC) 실전 거래 손익 합계
    today_utc = datetime.now(timezone.utc).date()
    today_start = datetime(today_utc.year, today_utc.month, today_utc.day, tzinfo=timezone.utc)
    today_end = today_start + timedelta(days=1)

    today_pnl_result = await db.execute(
        select(func.sum(TradeHistory.profit_krw)).where(
            TradeHistory.is_paper_trading.is_(False),
            TradeHistory.created_at >= today_start,
            TradeHistory.created_at < today_end,
        )
    )
    today_pnl_krw = float(today_pnl_result.scalar() or 0.0)

    # 4. daily_pnl — 최근 7일 날짜별 실전 거래 손익 집계
    seven_days_ago = today_start - timedelta(days=6)  # 오늘 포함 7일

    daily_result = await db.execute(
        select(
            func.date(TradeHistory.created_at).label("trade_date"),
            func.sum(TradeHistory.profit_krw).label("pnl_krw"),
        )
        .where(
            TradeHistory.is_paper_trading.is_(False),
            TradeHistory.created_at >= seven_days_ago,
            TradeHistory.created_at < today_end,
        )
        .group_by(func.date(TradeHistory.created_at))
    )
    db_daily_map: dict[str, float] = {
        str(row.trade_date): float(row.pnl_krw or 0.0)
        for row in daily_result.all()
    }

    # 데이터 없는 날짜 0.0으로 채워 7일 목록 생성 (오래된 날짜 → 최근 순)
    daily_pnl: list[dict] = []
    for offset in range(6, -1, -1):
        target_date = today_utc - timedelta(days=offset)
        date_str = target_date.strftime("%Y-%m-%d")
        daily_pnl.append(
            {
                "date": date_str,
                "pnl_krw": round(db_daily_map.get(date_str, 0.0), 2),
            }
        )

    logger.info(
        "Admin overview 조회: active_users=%d aum_krw=%.0f today_pnl=%.0f",
        active_users,
        aum_krw,
        today_pnl_krw,
    )
    return {
        "kpi": {
            "active_users": active_users,
            "aum_krw": round(aum_krw, 2),
            "today_pnl_krw": round(today_pnl_krw, 2),
            "system_alerts": None,
        },
        "daily_pnl": daily_pnl,
    }


# ------------------------------------------------------------------
# GET /api/admin/trade-logs  (P1 신규)
# ------------------------------------------------------------------


@router.get("/trade-logs", dependencies=[Depends(get_api_key)])
async def get_trade_logs(
    page: int = Query(default=1, ge=1, description="페이지 번호 (1-based)"),
    page_size: int = Query(default=50, ge=1, le=200, description="페이지당 건수 (최대 200)"),
    user_id: Optional[str] = Query(default=None, description="특정 유저 ID 필터"),
    is_paper: Optional[bool] = Query(default=None, description="모의투자 여부 필터"),
    engine: Optional[str] = Query(default=None, description="엔진 필터 (trade_style 기준)"),
    from_date: Optional[date] = Query(default=None, description="시작 날짜 (UTC, inclusive)"),
    to_date: Optional[date] = Query(default=None, description="종료 날짜 (UTC, inclusive)"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """거래 이력을 페이징 조회한다.

    동적 WHERE 조건을 조합하여 필터링하며, created_at 내림차순으로 정렬한다.

    Args:
        page: 페이지 번호 (1-based).
        page_size: 페이지당 건수. 최대 200으로 clamp.
        user_id: Discord 유저 ID 필터 (정확한 일치).
        is_paper: True = 모의투자만 / False = 실전만 / None = 전체.
        engine: trade_style 컬럼 기준 엔진 필터 (예: "SWING", "SCALPING").
        from_date: 이 날짜 이후(UTC 00:00:00 포함) 거래만 조회.
        to_date: 이 날짜 이전(UTC 23:59:59 포함) 거래만 조회.
        db: 비동기 DB 세션.

    Returns:
        pagination: 페이지 메타 정보.
        trades: 거래 이력 목록.
    """
    # page_size 최대 200 clamp (Query le=200으로 이미 처리되지만 명시적 방어)
    page_size = min(page_size, 200)

    # 동적 WHERE 조건 조합
    conditions = []
    if user_id is not None:
        conditions.append(TradeHistory.user_id == user_id)
    if is_paper is not None:
        conditions.append(TradeHistory.is_paper_trading.is_(is_paper))
    if engine is not None:
        conditions.append(TradeHistory.trade_style == engine)
    if from_date is not None:
        from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=timezone.utc)
        conditions.append(TradeHistory.created_at >= from_dt)
    if to_date is not None:
        to_dt = datetime(to_date.year, to_date.month, to_date.day, 23, 59, 59, tzinfo=timezone.utc)
        conditions.append(TradeHistory.created_at <= to_dt)

    # COUNT 서브쿼리
    count_query = select(func.count(TradeHistory.id))
    if conditions:
        count_query = count_query.where(*conditions)
    count_result = await db.execute(count_query)
    total_count = int(count_result.scalar() or 0)

    # 데이터 쿼리 (LIMIT/OFFSET 페이징)
    offset = (page - 1) * page_size
    data_query = (
        select(TradeHistory)
        .order_by(TradeHistory.created_at.desc())
        .limit(page_size)
        .offset(offset)
    )
    if conditions:
        data_query = data_query.where(*conditions)
    data_result = await db.execute(data_query)
    rows = data_result.scalars().all()

    total_pages = max(1, (total_count + page_size - 1) // page_size)

    trades: list[dict] = []
    for row in rows:
        trades.append(
            {
                "id": row.id,
                "user_id": row.user_id,
                "symbol": row.symbol,
                "is_paper_trading": row.is_paper_trading,
                "engine": row.trade_style,
                "buy_price": row.buy_price,
                "sell_price": row.sell_price,
                "profit_pct": row.profit_pct,
                "profit_krw": row.profit_krw,
                "close_type": row.close_type,
                "bought_at": row.bought_at.isoformat() if row.bought_at else None,
                "sold_at": row.created_at.isoformat() if row.created_at else None,
                "ai_version": row.ai_version,
            }
        )

    logger.info(
        "Admin trade-logs 조회: page=%d page_size=%d total=%d",
        page,
        page_size,
        total_count,
    )
    return {
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "total_pages": total_pages,
        },
        "trades": trades,
    }


# ------------------------------------------------------------------
# GET /api/admin/stats/engines  (P2 고도화)
# ------------------------------------------------------------------


@router.get("/stats/engines", dependencies=[Depends(get_api_key)])
async def get_engine_stats(
    is_paper: Optional[bool] = Query(default=None, description="모의투자 여부 필터"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """엔진별 거래 통계를 반환한다.

    trade_style 컬럼을 기준으로 그룹화하며, is_ai_managed=True 레코드만 집계한다.
    승률은 profit_pct > 0 인 건수 / 전체 건수로 계산한다.

    Args:
        is_paper: True = 모의투자만 / False = 실전만 / None = 전체.
        db: 비동기 DB 세션.

    Returns:
        engines: 엔진별 통계 딕셔너리 목록.
            - engine: 엔진명 (SWING / SCALPING / MAJOR_TREND / 기타)
            - total_trades: 총 거래 횟수
            - win_rate_pct: 승률 (%)
            - total_profit_krw: 누적 손익 (KRW)
            - avg_profit_pct: 익절 거래 평균 수익률 (%)
            - avg_loss_pct: 손절 거래 평균 손실률 (%, 음수)
            - avg_hold_hours: 평균 보유 시간 (시간)
    """
    # 기본 조건 구성
    base_conditions = [TradeHistory.is_ai_managed.is_(True)]
    if is_paper is not None:
        base_conditions.append(TradeHistory.is_paper_trading.is_(is_paper))

    # 총 거래 수 + 누적 손익 쿼리
    result = await db.execute(
        select(
            TradeHistory.trade_style,
            func.count(TradeHistory.id).label("total_trades"),
            func.sum(TradeHistory.profit_krw).label("total_profit_krw"),
        )
        .where(*base_conditions)
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
                "avg_profit_pct": None,  # 하단 avg_profit 쿼리로 채움
                "avg_loss_pct": None,    # 하단 avg_loss 쿼리로 채움
                "avg_hold_hours": None,  # 하단 avg_hold 쿼리로 채움
            }
        )

    # 승률 쿼리
    win_result = await db.execute(
        select(
            TradeHistory.trade_style,
            func.count(TradeHistory.id).label("win_count"),
        )
        .where(
            *base_conditions,
            TradeHistory.profit_pct > 0,
        )
        .group_by(TradeHistory.trade_style)
    )
    win_map: dict[str, int] = {
        (row.trade_style or "UNKNOWN"): int(row.win_count or 0)
        for row in win_result.all()
    }

    # 익절 평균 수익률 쿼리 (profit_pct > 0)
    avg_profit_result = await db.execute(
        select(
            TradeHistory.trade_style,
            func.avg(TradeHistory.profit_pct).label("avg_profit_pct"),
        )
        .where(
            *base_conditions,
            TradeHistory.profit_pct > 0,
        )
        .group_by(TradeHistory.trade_style)
    )
    avg_profit_map: dict[str, float] = {
        (row.trade_style or "UNKNOWN"): float(row.avg_profit_pct or 0.0)
        for row in avg_profit_result.all()
    }

    # 손절 평균 손실률 쿼리 (profit_pct <= 0)
    avg_loss_result = await db.execute(
        select(
            TradeHistory.trade_style,
            func.avg(TradeHistory.profit_pct).label("avg_loss_pct"),
        )
        .where(
            *base_conditions,
            TradeHistory.profit_pct <= 0,
        )
        .group_by(TradeHistory.trade_style)
    )
    avg_loss_map: dict[str, float] = {
        (row.trade_style or "UNKNOWN"): float(row.avg_loss_pct or 0.0)
        for row in avg_loss_result.all()
    }

    # 평균 보유 시간 쿼리 (bought_at IS NOT NULL)
    avg_hold_result = await db.execute(
        select(
            TradeHistory.trade_style,
            func.avg(
                func.extract("epoch", TradeHistory.created_at - TradeHistory.bought_at) / 3600
            ).label("avg_hold_hours"),
        )
        .where(
            *base_conditions,
            TradeHistory.bought_at.is_not(None),
        )
        .group_by(TradeHistory.trade_style)
    )
    avg_hold_map: dict[str, float] = {
        (row.trade_style or "UNKNOWN"): float(row.avg_hold_hours or 0.0)
        for row in avg_hold_result.all()
    }

    # 집계 결과 병합
    for item in engines:
        eng = item["engine"]
        total = item["total_trades"]
        wins = win_map.get(eng, 0)
        item["win_rate_pct"] = round(wins / total * 100, 1) if total > 0 else 0.0
        item["avg_profit_pct"] = round(avg_profit_map.get(eng, 0.0), 2)
        item["avg_loss_pct"] = round(avg_loss_map.get(eng, 0.0), 2)
        item["avg_hold_hours"] = round(avg_hold_map.get(eng, 0.0), 2)

    logger.info("Admin stats/engines 조회: %d 엔진 is_paper=%s", len(engines), is_paper)
    return {"engines": engines}


# ------------------------------------------------------------------
# GET /api/admin/stats/close-types  (P2 고도화)
# ------------------------------------------------------------------


@router.get("/stats/close-types", dependencies=[Depends(get_api_key)])
async def get_close_type_stats(
    engine: Optional[str] = Query(default=None, description="엔진 필터 (trade_style 기준)"),
    is_paper: Optional[bool] = Query(default=None, description="모의투자 여부 필터"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """청산 사유별 발생 건수 및 비율을 반환한다.

    close_type 컬럼을 Group By 한다. NULL 은 "UNKNOWN" 으로 표시한다.

    Args:
        engine: trade_style 컬럼 기준 엔진 필터.
        is_paper: True = 모의투자만 / False = 실전만 / None = 전체.
        db: 비동기 DB 세션.

    Returns:
        close_types: 청산 사유별 통계 목록.
            - close_type: 사유 (TP_HIT / SL_HIT / AI_FORCE_SELL / MANUAL_OVERRIDE / UNKNOWN)
            - count: 발생 건수
            - ratio_pct: 전체 대비 비율 (%)
    """
    conditions = []
    if engine is not None:
        conditions.append(TradeHistory.trade_style == engine)
    if is_paper is not None:
        conditions.append(TradeHistory.is_paper_trading.is_(is_paper))

    query = (
        select(
            func.coalesce(TradeHistory.close_type, "UNKNOWN").label("close_type"),
            func.count(TradeHistory.id).label("count"),
        )
        .group_by(func.coalesce(TradeHistory.close_type, "UNKNOWN"))
        .order_by(func.count(TradeHistory.id).desc())
    )
    if conditions:
        query = query.where(*conditions)

    result = await db.execute(query)
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

    logger.info(
        "Admin stats/close-types 조회: 총 %d 건 engine=%s is_paper=%s",
        total_count,
        engine,
        is_paper,
    )
    return {"total_count": total_count, "close_types": close_types}


# ------------------------------------------------------------------
# GET /api/admin/slippage  (P2 고도화)
# ------------------------------------------------------------------


@router.get("/slippage", dependencies=[Depends(get_api_key)])
async def get_slippage_stats(
    engine: Optional[str] = Query(default=None, description="엔진 필터 (trade_style 기준)"),
    is_paper: Optional[bool] = Query(default=None, description="모의투자 여부 필터"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """예상 체결가(expected_price) 대비 실제 매도가(sell_price) 평균 슬리피지를 반환한다.

    expected_price 와 sell_price 가 모두 NULL 이 아닌 레코드만 집계한다.
    슬리피지(%) = (sell_price - expected_price) / expected_price × 100
    양수 = 예상보다 유리한 체결 (슬리피지 이득)
    음수 = 예상보다 불리한 체결 (슬리피지 손해)

    Args:
        engine: trade_style 컬럼 기준 엔진 필터.
        is_paper: True = 모의투자만 / False = 실전만 / None = 전체.
        db: 비동기 DB 세션.

    Returns:
        avg_slippage_pct: 평균 슬리피지 (%)
        sample_count: 집계에 사용된 레코드 수
    """
    conditions = [
        TradeHistory.expected_price.is_not(None),
        TradeHistory.sell_price.is_not(None),
        TradeHistory.expected_price > 0,
    ]
    if engine is not None:
        conditions.append(TradeHistory.trade_style == engine)
    if is_paper is not None:
        conditions.append(TradeHistory.is_paper_trading.is_(is_paper))

    result = await db.execute(
        select(
            func.avg(
                (TradeHistory.sell_price - TradeHistory.expected_price)
                / TradeHistory.expected_price
                * 100
            ).label("avg_slippage_pct"),
            func.count(TradeHistory.id).label("sample_count"),
        )
        .where(*conditions)
    )
    row = result.one_or_none()

    avg_slip = float(row.avg_slippage_pct or 0.0) if row else 0.0
    sample_n = int(row.sample_count or 0) if row else 0

    logger.info(
        "Admin slippage 조회: avg=%.4f%% sample=%d engine=%s is_paper=%s",
        avg_slip,
        sample_n,
        engine,
        is_paper,
    )
    return {
        "avg_slippage_pct": round(avg_slip, 4),
        "sample_count": sample_n,
    }
