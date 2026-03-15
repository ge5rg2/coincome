"""
시간 관련 유틸리티 함수 모음.

서버 OS 타임존에 무관하게 zoneinfo.ZoneInfo("Asia/Seoul") 을 직접 사용하므로
배포 환경(UTC 서버)에서도 항상 KST 기준 시각을 정확히 계산한다.
"""
from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")

# 스윙 AI 펀드 매니저 동작 시각 (KST 시 단위, 오름차순)
# ai_manager.py 의 _SWING_HOURS 와 반드시 동기화 유지
_SWING_SCHEDULE_HOURS: tuple[int, ...] = (1, 5, 9, 13, 17, 21)


def get_next_ai_run_time() -> str:
    """현재 KST 기준으로 다음 스윙 AI 스케줄러 동작 시각을 사람이 읽기 쉬운 문자열로 반환한다.

    업비트 4h 봉 완성 정각(01/05/09/13/17/21시 KST)을 기준으로
    현재 시각 이후의 가장 빠른 시각을 찾는다.

    Returns:
        "오늘 HH:00" 또는 "내일 HH:00" 형식의 문자열.
        예: "오늘 13:00", "내일 01:00"
    """
    now = datetime.datetime.now(_KST)

    # 오늘 남은 스케줄 중 가장 빠른 시각 탐색
    for hour in _SWING_SCHEDULE_HOURS:
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now:
            return f"오늘 {hour:02d}:00"

    # 오늘 스케줄이 모두 지났으면 내일 첫 번째 시각
    first_hour = _SWING_SCHEDULE_HOURS[0]
    return f"내일 {first_hour:02d}:00"


def get_next_scalping_run_time() -> str:
    """현재 KST 기준으로 다음 단타 AI 스케줄러 동작 시각을 반환한다.

    단타(SCALPING) 모드는 매시 정각에 실행되므로 항상 1시간 이내 실행.

    Returns:
        "오늘 HH:00" 또는 "내일 00:00" 형식의 문자열.
    """
    now = datetime.datetime.now(_KST)
    next_hour = now.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)

    if next_hour.date() == now.date():
        return f"오늘 {next_hour.hour:02d}:00"
    return f"내일 {next_hour.hour:02d}:00"


def get_next_run_time_for_style(trade_style: str) -> str:
    """투자 성향에 맞는 다음 AI 실행 시각을 반환한다.

    Args:
        trade_style: "SWING" 또는 "SCALPING".

    Returns:
        "오늘 HH:00" 또는 "내일 HH:00" 형식의 문자열.
    """
    if trade_style == "SCALPING":
        return get_next_scalping_run_time()
    return get_next_ai_run_time()
