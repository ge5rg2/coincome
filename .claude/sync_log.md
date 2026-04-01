# Agent Sync Log — Coincome

에이전트 파일(.claude/agents/*.md, CLAUDE.md) 자기 갱신 이력.
PM STEP 5에서 매 워크플로 완료 후 기록.

---

## 2026-04-01 — feat(engine): 실전/모의 예산 컬럼 격리 아키텍처 결함 수정
- 갱신 파일: pm.md, coder.md, tester.md, CLAUDE.md
- 갱신 내용:
  - pm.md V2 원칙에 "실전/모의 예산 컬럼 격리" 원칙 추가 (ai_paper_* 전용 컬럼 쓰기, 실전 컬럼 Paper 모달에서 절대 금지)
  - pm.md 핵심 파일 지도에 add_paper_budget_columns.py 추가
  - coder.md 실전/모의 플래그 격리 패턴 섹션을 "실전/모의 플래그 및 예산 컬럼 격리"로 확장 (paper 전용 컬럼 쓰기 예시, ai_manager.py paper_engine_mode/paper_run_swing/paper_run_scalp 분리 패턴 추가)
  - tester.md 4단계 회귀 체크에 "실전/모의 예산 컬럼 격리" 검증 항목 추가 (ai_paper_* 컬럼 사용, 실전 컬럼 수정 없음, ai_manager.py paper 변수 분리 확인)
  - CLAUDE.md V2 불변 원칙에 1-B번 (실전/모의 예산 컬럼 격리) 추가

## 2026-04-01 — fix(engine): 모의투자 매수금액 폭발 버그 및 AI available_krw 오산 수정
- 갱신 파일: tester.md
- 갱신 내용: 모의 예산 Cap 패턴 회귀 체크 항목 추가 — swing/scalp_paper_invested 합산, budget cap min() 적용, major_budget==0 시 0.0 처리, analyze_market available_krw max() 금지 조건식 검증 포인트 신설

---

## 2026-03-31 — feat(api): Admin API AUM/PnL 버그 수정 및 users 엔드포인트 신규 추가
- 갱신 파일: pm.md, coder.md, tester.md
- 갱신 내용:
  - coder.md에 "Admin API 집계 쿼리 패턴" 섹션 신설 (case() 조건부 집계, outerjoin GROUP BY N+1 방지, 배치 서브쿼리 open_positions 매핑, _serialize_trades 헬퍼 분리 패턴)
  - tester.md 4단계 회귀 체크에 "Admin API 집계 쿼리 패턴" 검증 항목 추가 (AUM 집계 기준 변경 확인, 실전/모의 PnL 분리, 신규 엔드포인트 N+1 방지, 404 처리)
  - pm.md 핵심 파일 지도 admin.py 설명에 users·users/{id}/stats 엔드포인트 추가

## 2026-03-30 — feat(bot): PRO/VIP 등급별 AI 트레이딩 UI 및 엔진 차등화 개편
- 갱신 파일: pm.md, coder.md, tester.md, CLAUDE.md
- 갱신 내용:
  - pm.md V2 원칙에 "등급별 AI 엔진 제한" 원칙 추가 (max_active_engines 기반)
  - pm.md 핵심 파일 지도에 add_engine_tier_columns.py 추가
  - coder.md "등급별 AI 엔진 제한 패턴" 섹션 신설 (FREE 차단·PRO/VIP 분기·토글 View·동적 Modal·_validate_budget_range 패턴)
  - tester.md 4단계 회귀 체크에 "등급별 AI 엔진 제한 패턴" 검증 항목 추가 (PRO/VIP 분기, 예산 범위, is_major_enabled 불변)
  - CLAUDE.md V2 불변 원칙 10번 추가 (등급별 AI 엔진 제한)

## 2026-03-30 — feat(api): Admin API P1+P2 — overview·trade-logs 신규, engines·close-types·slippage 고도화
- 갱신 파일: coder.md, tester.md
- 갱신 내용:
  - coder.md에 Admin API 동적 필터 패턴 섹션 신설 (조건 목록 누적, COUNT 서브쿼리, 페이징, func.date() 그룹핑, 빈 날짜 채우기, func.extract("epoch"...) 보유시간, Numeric→float 변환)
  - tester.md 4단계 회귀 체크에 Admin API 동적 필터 패턴 검증 항목 추가 (8개 체크포인트)

## 2026-03-29 — V2 Phase 4: Dynamic Regime Filter + Admin API + 리포트 UX
- 갱신 파일: pm.md, coder.md, tester.md, CLAUDE.md
- 갱신 내용:
  - pm.md 핵심 파일 지도에 app/api/routers/admin.py 추가, services/ai_trader.py regime 파라미터 주석 추가
  - pm.md V2 원칙에 Dynamic Regime Filter, 정기 리포트 View 미첨부, Admin API 인증 3개 항목 추가
  - pm.md 커밋 scope에 api 추가
  - coder.md Admin API 인증 패턴 + Dynamic Regime Filter 패턴 섹션 신설
  - tester.md 4단계 회귀 체크에 Regime Filter·정기 리포트 View 미첨부·Admin API 인증 항목 추가
  - CLAUDE.md V2 불변 원칙 7번(Regime Filter), 8번(View 미첨부), 9번(Admin API 인증) 추가
  - CLAUDE.md 커밋 scope에 api 추가

## 2026-03-28 — feat(db): Admin 분석용 TradeHistory·BotSetting 스키마 확장
- 갱신 파일: pm.md, coder.md, tester.md
- 갱신 내용:
  - pm.md 핵심 파일 지도에 trade_history.py, add_admin_analytics_columns.py 추가
  - pm.md V2 원칙에 "Admin 분석 태깅" 원칙 추가 (close_type/force_sell 파라미터 규칙)
  - coder.md에 "Admin 분석용 TradeHistory 태깅 패턴" 및 "마이그레이션 스크립트 패턴" 섹션 신규 추가
  - tester.md 4단계 회귀 체크에 "Admin 분석 태깅 패턴" 항목 추가

## 2026-03-26 — chore(agents): Tester 보안 점검(공격자 시점) 단계 추가
- 갱신 파일: tester.md
- 갱신 내용:
  - 6단계 "보안 점검: 공격자 시점" 신규 추가 (기존 6단계→7단계로 번호 이동)
  - 인증/권한 탈취, 자금 조작, DB 인젝션, 크리덴셜 노출, Discord 인터랙션 조작 5개 카테고리
  - CRITICAL/HIGH/MEDIUM/LOW 4단계 심각도 판정 기준 명시
  - 보고 형식에 보안 점검 섹션 추가 (카테고리별 테이블)

## 2026-03-26 — feat(bot): AI 리포트 수동 청산 Manual Override UI 추가
- 갱신 파일: pm.md, tester.md
- 갱신 내용:
  - pm.md 핵심 파일 지도에 app/bot/views/manual_sell_view.py 추가
  - pm.md V2 원칙에 "View 콜백 DB 재검증" 및 "순환 임포트 방지" 원칙 추가
  - tester.md 4단계 회귀 체크에 ManualSellView 콜백 패턴 체크 추가

## 2026-03-27 — fix(bot): ManualSellView IDOR·중복청산 보안 취약점 패치
- 갱신 파일: pm.md, coder.md, tester.md, CLAUDE.md
- 갱신 내용:
  - pm.md V2 원칙에 "View IDOR 방지" (BotSetting.user_id AND 조건 필수) 및 "View 중복 청산 방지" (is_finished() + self.stop() defer 이전) 추가
  - pm.md 핵심 파일 지도에 report.py (/내포지션 커맨드) 추가
  - coder.md 프로젝트 패턴에 "discord.ui.View 보안 패턴" 섹션 신규 추가 (IDOR 방지 + Race Condition 방지 코드 예시 포함)
  - tester.md 4단계 회귀 체크 ManualSellView 패턴 항목을 IDOR 방지 및 순서 조건 포함으로 갱신
  - CLAUDE.md V2 불변 원칙에 5번(View IDOR 방지), 6번(View 중복 청산 방지) 추가

## 2026-03-27 — feat(bot): /내포지션 슬래시 커맨드 신설
- 갱신 파일: 없음 (변경 없음)
- 갱신 내용: 기존 지역 import 원칙 및 IDOR 방지 패턴 적용 — 신규 원칙 없음

## 2026-03-25 — 에이전트 오케스트레이션 초기 세팅
- 갱신 파일: pm.md, coder.md, tester.md, CLAUDE.md (신규 생성)
- 갱신 내용: PM·Coder·Tester 3단계 에이전트 정의, STEP 5 자기 갱신 워크플로 추가

## 2026-03-31 — fix(report): AI 운용 총자산 AUM 오류 및 비활성 엔진 레이블 노출 수정
- 갱신 파일: 없음 (변경 없음)
- 갱신 내용: 기존 원칙(실전/모의 플래그 격리, is_major_on 기반 엔진 레이블 분기)의 버그픽스. 신규 아키텍처 원칙 없음.
  버그 1: /ai통계 real_total_asset = AI 예산 합산 + 코인 평가액 (업비트 전체 잔고 아님)
  버그 2: _build_unified_report_embed에 is_major_on 파라미터 추가 — MAJOR OFF 시 "2엔진" 레이블·MAJOR 대기 안내 미표시
