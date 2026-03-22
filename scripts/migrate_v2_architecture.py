"""
migrate_v2_architecture.py — AI 트레이딩 코어 V2 아키텍처 DB 마이그레이션 스크립트.

기존 ai_trade_style(SNIPER/BEAST/SWING/SCALPING) 기반 유저 데이터를 V2 엔진 필드로 변환.

변환 규칙:
  SNIPER/SWING  → ai_engine_mode="SWING",    ai_swing_budget_krw=ai_budget_krw(0이면 1,000,000), ai_swing_weight_pct=20
  BEAST/SCALPING → ai_engine_mode="SCALPING", ai_scalp_budget_krw=ai_budget_krw(0이면 1,000,000), ai_scalp_weight_pct=70

실행 예시:
  python scripts/migrate_v2_architecture.py           # dry-run (실제 DB 수정 없음)
  python scripts/migrate_v2_architecture.py --apply   # 실제 마이그레이션 적용
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.user import User

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def migrate(dry_run: bool = True) -> None:
    """V2 엔진 필드를 기존 ai_trade_style 기반으로 초기화한다.

    Args:
        dry_run: True이면 변경 없이 로그만 출력, False이면 실제 DB 업데이트.
    """
    mode_str = "[DRY-RUN]" if dry_run else "[APPLY]"
    logger.info("%s V2 아키텍처 마이그레이션 시작", mode_str)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User))
        users: list[User] = result.scalars().all()

    logger.info("전체 유저 %d명 조회 완료", len(users))

    migrated = 0
    skipped  = 0

    for user in users:
        old_style  = (getattr(user, "ai_trade_style", "SWING") or "SWING").upper()
        engine_mode = getattr(user, "ai_engine_mode", None) or ""

        # 이미 V2 엔진 모드가 설정된 경우 스킵
        if engine_mode in ("SWING", "SCALPING", "BOTH"):
            logger.info(
                "  SKIP user_id=%s (이미 V2 설정: engine_mode=%s)",
                user.user_id, engine_mode,
            )
            skipped += 1
            continue

        # 기존 예산 (0이면 기본 100만 원으로 채움)
        old_budget = float(getattr(user, "ai_budget_krw", 0) or 0)
        default_budget = 1_000_000

        if old_style in ("BEAST", "SCALPING"):
            new_engine   = "SCALPING"
            new_s_budget = default_budget   # 스윙 엔진은 기본값
            new_s_weight = 20
            new_c_budget = int(old_budget) if old_budget > 0 else default_budget
            new_c_weight = 70              # 기존 BEAST 비중
        else:  # SNIPER / SWING (기본)
            new_engine   = "SWING"
            new_s_budget = int(old_budget) if old_budget > 0 else default_budget
            new_s_weight = 20              # 기존 SNIPER 비중
            new_c_budget = default_budget   # 스캘핑 엔진은 기본값
            new_c_weight = 20

        logger.info(
            "  MIGRATE user_id=%s  %s → engine_mode=%s  "
            "swing(budget=%d, weight=%d%%)  scalp(budget=%d, weight=%d%%)",
            user.user_id, old_style, new_engine,
            new_s_budget, new_s_weight,
            new_c_budget, new_c_weight,
        )

        if not dry_run:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(User).where(User.user_id == user.user_id)
                )
                db_user = result.scalar_one_or_none()
                if db_user:
                    db_user.ai_engine_mode       = new_engine
                    db_user.ai_swing_budget_krw  = new_s_budget
                    db_user.ai_swing_weight_pct  = new_s_weight
                    db_user.ai_scalp_budget_krw  = new_c_budget
                    db_user.ai_scalp_weight_pct  = new_c_weight
                    await db.commit()

        migrated += 1

    logger.info(
        "%s 완료: 마이그레이션=%d명, 스킵=%d명 (총 %d명)",
        mode_str, migrated, skipped, len(users),
    )
    if dry_run and migrated > 0:
        logger.info(
            "실제 적용하려면 --apply 옵션을 추가하세요: "
            "python scripts/migrate_v2_architecture.py --apply"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI 트레이딩 V2 아키텍처 DB 마이그레이션",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="실제 DB 마이그레이션 적용 (없으면 dry-run)",
    )
    args = parser.parse_args()

    asyncio.run(migrate(dry_run=not args.apply))


if __name__ == "__main__":
    main()
