# Agent Sync Log — Coincome

에이전트 파일(.claude/agents/*.md, CLAUDE.md) 자기 갱신 이력.
PM STEP 5에서 매 워크플로 완료 후 기록.

---

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

## 2026-03-27 — feat(bot): /내포지션 슬래시 커맨드 신설
- 갱신 파일: 없음 (변경 없음)
- 갱신 내용: 기존 지역 import 원칙 및 IDOR 방지 패턴 적용 — 신규 원칙 없음

## 2026-03-25 — 에이전트 오케스트레이션 초기 세팅
- 갱신 파일: pm.md, coder.md, tester.md, CLAUDE.md (신규 생성)
- 갱신 내용: PM·Coder·Tester 3단계 에이전트 정의, STEP 5 자기 갱신 워크플로 추가
