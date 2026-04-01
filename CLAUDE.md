# Coincome — Claude Code 작업 가이드

## 세션 시작 시 자동 점검 (필수)

**새 세션을 시작할 때마다 Claude는 아래 순서로 점검한다:**

1. `.claude/sync_pending.md` 존재 여부 확인
   - 존재하면: 파일을 읽고 이전 세션에서 갱신되지 못한 항목을 에이전트 파일에 반영
   - 파일 처리 후 삭제
2. `git log --oneline -3` 으로 마지막 3개 커밋 확인
   - `.claude/sync_log.md`의 마지막 항목과 비교해 누락된 갱신이 있는지 확인
3. 누락 갱신이 있으면 즉시 에이전트 파일 업데이트 후 사용자에게 알림

> 이 점검은 30초 이내로 완료되어야 하며, 갱신이 없으면 조용히 넘어간다.

---

## 에이전트 오케스트레이션

이 프로젝트는 **PM → Coder → Tester** 3단계 에이전트 워크플로를 사용합니다.
사용자가 개발 명령을 내리면 **반드시 PM 에이전트가 전체 파이프라인을 총괄**합니다.

```
사용자 명령
    │
    ▼
  PM 에이전트
  ├─ 요구사항 분석 · 아키텍처 검토 · Task 분해
  │
  ├─ Coder 에이전트 위임
  │   └─ 클린 코드 구현 → 완료 보고
  │
  ├─ Tester 에이전트 위임
  │   └─ 문법·로직·회귀·엣지케이스 검증 → PASS/FAIL 보고
  │
  └─ 커밋 (Conventional Commits) + 문서 갱신 + 사용자 보고
```

### 에이전트 역할

| 에이전트 | 역할 | 사용 도구 |
|---|---|---|
| **PM** | 오케스트레이터. 요구사항 분석, Task 설계, Coder/Tester 위임, 커밋 | Read, Grep, Glob, Bash, Agent, Write, Edit |
| **Coder** | 구현 담당. PM 브리핑대로 클린 코드 작성 | Read, Grep, Glob, Bash, Edit, Write |
| **Tester** | QA 담당. 요구사항 대조 검증, 문법·회귀·엣지케이스 점검 후 PM 보고 | Read, Grep, Glob, Bash |

### 사용 방법

```
# 고수준 명령 → PM이 자동으로 전체 워크플로 수행
"Ghost Update 방지 로직에 단위 테스트 추가해줘"
"MAJOR 엔진에 새로운 필터 조건 추가해줘"
"잔고 조회 실패 시 에러 메시지 개선해줘"

# 특정 에이전트 직접 지정
"@agent-pm 이 요구사항 분석해줘"
"@agent-tester ai_manager.py 회귀 체크해줘"
```

---

## 프로젝트 핵심 정보

### 기술 스택
- **Runtime**: Python 3.12, asyncio
- **Discord**: discord.py 2.4
- **Web**: FastAPI 0.115
- **DB**: PostgreSQL 18 + SQLAlchemy 2.0 async + Alembic
- **거래소**: CCXT 4.4 (Upbit)
- **AI**: Anthropic claude-sonnet-4-6

### 브랜치 전략
- `dev` — 현재 작업 브랜치 (모든 커밋은 여기)
- `main` — 운영 배포 (PR 경유만)
- `main` 직접 push 금지

### 커밋 컨벤션
```
<type>(<scope>): <subject>

type : feat / fix / refactor / docs / chore / test
scope: ai / engine / report / prompt / review / worker / bot / db / docs / market / api
```

### V2 아키텍처 불변 원칙
1. **실전/모의 플래그 격리**: Paper 모달은 `is_major_enabled`, `ai_mode_enabled` 절대 수정 금지
1-B. **실전/모의 예산 컬럼 격리**: Paper 모달은 `ai_paper_engine_mode`, `ai_paper_swing_budget_krw`, `ai_paper_scalp_budget_krw`, `ai_paper_major_budget` 전용 컬럼만 쓰기. `ai_engine_mode`, `ai_swing_budget_krw`, `ai_scalp_budget_krw`, `major_budget` 실전 컬럼 수정 절대 금지
2. **Ghost Update 방지**: AI 리뷰 후 `_surviving_ids` IN 쿼리 재검증 항상 유지
3. **on-demand fetch**: 포지션 리뷰 시 캐시 미스 심볼 자동 fetch 유지
4. **에러 DM 알림**: 크리티컬 오류(force_sell 실패, DB 삽입 실패, 잔고 조회 실패)는 반드시 유저 DM
5. **View IDOR 방지**: BotSetting 조회 시 `BotSetting.user_id == user_id` AND 조건 필수. 단독 id 조회 금지
6. **View 중복 청산 방지**: `is_finished()` 선제 체크 후 `self.stop()`을 `defer()` 이전에 호출
7. **Dynamic Regime Filter**: SWING/SCALPING analyze_market 호출 전 `_fetch_btc_regime()`으로 BTC 4h EMA50 regime 계산 필수. MAJOR 엔진은 적용 제외.
8. **정기 리포트 View 미첨부**: ai_manager _process_user Step 4 DM 전송 시 ManualSellView 부착 금지. 수동 청산은 /내포지션 전용.
9. **Admin API 인증**: /api/admin/* 엔드포인트는 X-Admin-API-Key 헤더 인증 필수. settings.admin_api_key 기반.
10. **등급별 AI 엔진 제한**: `max_active_engines` 컬럼 (FREE=0/PRO=1/VIP=3). `/ai실전`·`/ai모의` 진입 시 `getattr(user, 'max_active_engines', 1)==0` 이면 즉시 차단. PRO=버튼 1개 택 1 View, VIP=토글 복수 선택 View + 동적 Modal.

### AI 트레이딩 변경 시 문서 갱신 대상
- `PROJECT_STATE.md` — 변경 이력, DB 모델, 오픈 이슈
- `docs/AI_TRADING_ARCHITECTURE.md` — 아키텍처, 엔진 매트릭스, 플로우

---

## 에이전트 정의 위치

```
.claude/agents/
├── pm.md      ← PM 오케스트레이터
├── coder.md   ← 구현 담당
└── tester.md  ← QA 검증 담당
```
