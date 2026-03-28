# CoinCome — 프로젝트 현황 (PROJECT STATE)

> **기준일**: 2026-03-28 (최종 수정: 2026-03-28 — Admin 분석용 TradeHistory·BotSetting 스키마 확장, close_type/bought_at/ai_version/expected_price 추가)
> **현재 작업 브랜치**: `dev`
> **최신 안정 브랜치**: `main` (커밋 `d11a0fd`)

---

## 1. 프로젝트 개요

업비트(Upbit) 기반 Discord 자동 매매 봇 MVP.
Discord 슬래시 커맨드로 봇 설정·구독·리포트를 제어하고, FastAPI 서버가 결제 콜백을 처리한다.
AI 펀드 매니저(Anthropic claude-sonnet-4-6 기반)가 시장을 자동 분석하고 코인을 픽해 실전·모의투자를 병행 운영한다.

### 기술 스택

| 분류             | 기술                                                    |
| ---------------- | ------------------------------------------------------- |
| **언어**         | Python 3.12                                             |
| **API 서버**     | FastAPI 0.115 + Uvicorn                                 |
| **Discord 봇**   | discord.py 2.4                                          |
| **DB**           | PostgreSQL 18 (SQLAlchemy 2.0 async + asyncpg)          |
| **마이그레이션** | Alembic                                                 |
| **거래소 연동**  | CCXT 4.4 (upbit)                                        |
| **AI 분석**      | Anthropic `claude-sonnet-4-6` (운영, 벤치마크 1위 채택) |
| **결제**         | TossPayments                                            |
| **배포**         | Docker + docker-compose                                 |
| **로케일/TZ**    | `ko_KR.UTF-8` / `Asia/Seoul`                            |

---

## 2. 아키텍처 개요

```
┌──────────────────────────────────────────────────────┐
│                    main.py (진입점)                   │
│  ┌─────────────────────┐  ┌──────────────────────┐  │
│  │   FastAPI Thread    │  │  Discord Bot asyncio │  │
│  │  (Uvicorn + ASGI)   │  │  (discord.py)        │  │
│  └──────────┬──────────┘  └──────────┬───────────┘  │
└─────────────┼────────────────────────┼──────────────┘
              │                        │
   ┌──────────▼───────┐    ┌───────────▼─────────────┐
   │  app/api/routers │    │  app/bot/cogs/           │
   │  - payments.py   │    │  - settings.py           │
   │  - web.py        │    │  - subscription.py       │
   └──────────────────┘    │  - report.py             │
                            │  - ai_trading.py         │
                            │  - paper_trading.py      │
                            └───────────┬─────────────┘
                                        │
              ┌─────────────────────────▼────────────────────┐
              │              app/services/                    │
              │  ┌──────────────────────────────────────────┐│
              │  │ MarketDataManager   (market_data.py)      ││
              │  │ 1시간 주기 갱신 · Top10 KRW 코인 스크리닝  ││
              │  │ 4h / 1h / 15m 봉 RSI·MA·ATR 계산         ││
              │  └──────────────────────────────────────────┘│
              │  ┌──────────────────────────────────────────┐│
              │  │ AITraderService      (ai_trader.py)       ││
              │  │ Anthropic claude-sonnet-4-6 호출          ││
              │  │ SWING/SCALPING/BOTH 모듈형 엔진 분기      ││
              │  │ analyze_market(engine_type, weight_pct)   ││
              │  │ review_positions() · _CORE_SWING/SCALPING ││
              │  └──────────────────────────────────────────┘│
              │  ┌──────────────────────────────────────────┐│
              │  │ WorkerRegistry + TradingWorker            ││
              │  │ (trading_worker.py)                       ││
              │  │ 0.5초 폴링 · 실전·모의투자 병행            ││
              │  │ 익절/손절 자동 실행 · Discord DM 알림      ││
              │  └──────────────────────────────────────────┘│
              │  ┌──────────────────────────────────────────┐│
              │  │ ExchangeService      (exchange.py)        ││
              │  │ CCXT upbit 추상화 · async 지원             ││
              │  └──────────────────────────────────────────┘│
              └──────────────────┬───────────────────────────┘
                                 │
              ┌──────────────────▼───────────────────────────┐
              │            PostgreSQL DB                      │
              │  users / bot_settings / payments /           │
              │  trade_histories                              │
              └──────────────────────────────────────────────┘
```

### AI 펀드 매니저 루프 (`app/bot/tasks/ai_manager.py`) — V2 모듈형 엔진

> ⚠️ `BOTH` 모드는 `ALL`로 레거시 마이그레이션 완료 (2026-03-25)

```
매시 정각 실행 (00:00~23:00, 24회/일)
  ai_engine_mode 분기:
    SWING    유저: 01·05·09·13·17·21시 KST에만 처리 (6회/일, 4h 봉)
    SCALPING 유저: 매시 정각 처리 (24회/일, 1h 봉)
    MAJOR    유저: 스윙 시간대 처리 (BTC/ETH 등 8종 메이저 코인, EMA200·정배열·BB 3중 필터)
    ALL      유저: SWING + SCALPING + MAJOR 독립 실행

  ① 연착륙 체크 (ai_is_shutting_down → 신규 매수 차단)
     - 종료 시 is_major_enabled = False 포함 (전체 실전 엔진 종료 보장)
  ② Step 1·2: 기존 포지션 리뷰 (_review_existing_positions)
     - _group_by_engine()으로 trade_style별 분리 리뷰 (SWING/SCALPING/MAJOR_TREND)
     - 캐시 미스 심볼 on-demand fetch (MAJOR 코인 등 Top N 외 종목 지표 보장)
     - SELL 판정 시 force_sell() → 실패 시 유저 DM 수동 확인 요청
  ③ SWING 엔진 (is_swing_hour=True 시):
     - 예산: min(KRW 잔고, ai_swing_budget_krw - swing_invested)
     - analyze_market(engine_type="SWING", weight_pct=ai_swing_weight_pct)
     - score≥90 픽 → _buy_new_coins(engine_type="SWING")
  ④ SCALPING 엔진 (매 사이클):
     - 예산: min(KRW 잔고, ai_scalp_budget_krw - scalp_invested)
     - analyze_market(engine_type="SCALPING", weight_pct=ai_scalp_weight_pct)
     - score≥90 픽 → _buy_new_coins(engine_type="SCALPING")
     - SCALPING: stop ≤ 2.0% 하드 상한, R:R ≥ 1.3 강제 보정
  ⑤ MAJOR 엔진 (is_swing_hour=True, is_major_on=True 시):
     - BTC/ETH/XRP/BNB/SOL/DOGE/ADA/SUI 8종 대상
     - 3중 기계적 필터: Close>EMA200 AND EMA20>EMA50 AND Close>BB상단
     - 통과 종목 없을 때도 DM에 "전체 관망" 한 줄 표시
     - 실전(is_major_enabled) / 모의(engine_mode in MAJOR·ALL) 별도 제어
  ⑥ 동전주 하드가드 (100 KRW 미만 진입 차단)
  ⑦ safe_trade_amount = int(trade_amount × 0.999)  # 수수료 버퍼
  ⑧ 실전·모의 동시 실행 (ai_mode + ai_paper_mode)
  ⑨ DM 리포트 전송 (엔진별 섹션 통합 임베드, engine_tag 정확 표시)
```

### 실전/모의 플래그 격리 원칙 (2026-03-25 확립)

| 플래그 | 실전 제어 | 모의 제어 |
|---|---|---|
| `ai_mode_enabled` | `/ai실전` 모달 전용 | 절대 건드리지 않음 |
| `is_major_enabled` | `/ai실전` MAJOR 모달 전용 | 절대 건드리지 않음 |
| `ai_paper_mode_enabled` | — | `/ai모의` 모달 |
| `ai_engine_mode` | — | `/ai모의` 엔진 선택 |

> Paper 모달(PaperSwing/Scalp/Major/AllEnginesModal)은 `is_major_enabled`, `ai_mode_enabled` 필드를 절대 수정하지 않는다.

---

## 3. 구독 등급 (Subscription Tier)

| 등급     | 최대 코인 수 | 최대 1회 투자   | AI 실전 모드        | AI 모의 모드              |
| -------- | ------------ | --------------- | ------------------- | ------------------------- |
| **FREE** | 2개          | 100,000 KRW     | ✗                   | ✓ (ai_paper_mode_enabled) |
| **PRO**  | 999개        | 100,000,000 KRW | ✗                   | ✓ (ai_paper_mode_enabled) |
| **VIP**  | 999개        | 100,000,000 KRW | ✓ (ai_mode_enabled) | ✓ (ai_paper_mode_enabled) |

> - 최대 코인 수: FREE=2개, PRO/VIP=999개 (코드 기준 `max_coins` 프로퍼티)
> - AI 실전 모드: `VIP AND ai_mode_enabled=True` 조건 동시 충족 필요
> - AI 모의 모드: 구독 등급 무관, `ai_paper_mode_enabled=True`이면 활성화
> - 결제: TossPayments `/confirm` (서버 승인) + `/callback` (웹훅)
> - 구독 만료 알림: `app/services/subscription.py` 백그라운드 루프

---

## 4. DB 모델 요약

### `users`

| 컬럼                                    | 타입            | 기본값     | 설명                                                       |
| --------------------------------------- | --------------- | ---------- | ---------------------------------------------------------- |
| `user_id`                               | String(255) PK  | —          | Discord 사용자 ID                                          |
| `upbit_access_key` / `upbit_secret_key` | EncryptedString | NULL       | AES-256(Fernet) 암호화 저장                                |
| `subscription_tier`                     | String(50)      | `"FREE"`   | FREE / PRO / VIP                                           |
| `sub_expires_at`                        | DateTime(tz)    | NULL       | 구독 만료일 (NULL=영구 또는 미결제)                        |
| `is_active`                             | Boolean         | True       | 계정 활성화 여부                                           |
| `report_enabled`                        | Boolean         | True       | 정기 수익률 보고 DM 활성화                                 |
| `report_interval_hours`                 | Integer         | 1          | 보고 주기 (허용값: 1/3/6/12/24)                            |
| `last_report_sent_at`                   | DateTime(tz)    | NULL       | 마지막 보고 전송 시각 (주기 계산용)                        |
| `ai_mode_enabled`                       | Boolean         | False      | AI 실전 매매 ON/OFF (VIP 전용, SWING·SCALPING·ALL 엔진)    |
| `ai_trade_amount`                       | Integer         | 10,000     | AI 1회 매수 금액 KRW (모드 비중 자동 산정)                 |
| `ai_max_coins`                          | Integer         | 3          | AI 동시 보유 최대 코인 수                                  |
| `ai_paper_mode_enabled`                 | Boolean         | False      | AI 모의투자 ON/OFF (등급 무관)                             |
| `ai_trade_style`                        | String(20)      | `"SWING"`  | 하위 호환용 — V2에서는 `ai_engine_mode` 우선               |
| `virtual_krw`                           | Float           | 10,000,000 | 모의투자 가상 KRW 잔고                                     |
| `ai_budget_krw`                         | Float           | 0.0        | V1 AI 운용 예산 한도 (하위 호환, V2는 엔진별 필드 사용)    |
| `ai_is_shutting_down`                   | Boolean         | False      | 연착륙 모드 (신규 매수 중단, 포지션 전량 청산 후 자동 OFF) |
| **V2 엔진 모드 필드**                   |                 |            | **AI 트레이딩 코어 V2 (2026-03-22 추가)**                  |
| `ai_engine_mode`                        | String(10)      | `"SWING"`  | 가동 엔진: SWING / SCALPING / MAJOR / ALL (구: BOTH)       |
| `ai_swing_budget_krw`                   | Integer         | 1,000,000  | 스윙 엔진 운용 예산 한도 (KRW)                             |
| `ai_swing_weight_pct`                   | Integer         | 20         | 스윙 1회 진입 비중 (10~100%)                               |
| `ai_scalp_budget_krw`                   | Integer         | 1,000,000  | 스캘핑 엔진 운용 예산 한도 (KRW)                           |
| `ai_scalp_weight_pct`                   | Integer         | 20         | 스캘핑 1회 진입 비중 (10~100%)                             |
| **MAJOR 엔진 필드**                     |                 |            | **2026-03-25 추가**                                        |
| `is_major_enabled`                      | Boolean         | False      | 실전 MAJOR 엔진 ON/OFF (VIP 전용, /ai실전 전용 설정)        |
| `major_budget`                          | Float           | 0.0        | MAJOR 엔진 운용 예산 한도 (KRW)                            |
| `major_trade_ratio`                     | Float           | 10.0       | MAJOR 1회 진입 비중 (%)                                    |

### `bot_settings`

| 컬럼                                  | 타입          | 설명                                                        |
| ------------------------------------- | ------------- | ----------------------------------------------------------- |
| `id`                                  | Integer PK    | autoincrement                                               |
| `user_id`                             | String FK     | → users.user_id                                             |
| `symbol`                              | String(20)    | 코인 심볼 (예: BTC/KRW)                                     |
| `buy_amount_krw`                      | Numeric(14,2) | 매수 금액 (KRW)                                             |
| `target_profit_pct` / `stop_loss_pct` | Numeric(6,2)  | 익절·손절 기준 (%)                                          |
| `is_running`                          | Boolean       | 워커 실행 여부                                              |
| `buy_price` / `amount_coin`           | Float         | 매수 체결 단가·수량 (서버 재시작 시 포지션 복구용)          |
| `is_paper_trading`                    | Boolean       | 모의투자 여부 (False=실거래)                                |
| `is_ai_managed`                       | Boolean       | AI 자동 생성 포지션 여부 (False=수동 /설정)                 |
| `trade_style`                         | String(20)    | AI 매수 당시 엔진 타입 (SWING / SCALPING, 하위 호환: SNIPER/BEAST) |
| `ai_score`                            | Integer       | AI 부여 종목 점수 (0~100)                                   |
| `ai_reason`                           | Text          | AI 매수 근거 텍스트                                         |
| `bought_at`                           | DateTime(tz)  | 매수 체결 시각 (UTC, 청산 시 TradeHistory로 이관)           |
| `ai_version`                          | String(20)    | AI 전략 버전 태그 (기본값: "v2.0")                          |

---

## 5. 핵심 파일 목록

```
coincome/
├── main.py                          # 진입점 (FastAPI + Discord 단일 루프)
├── requirements.txt                 # 의존성
├── Dockerfile / docker-compose.yml  # 배포 환경
│
├── app/
│   ├── config.py                    # pydantic-settings 환경변수
│   ├── database.py                  # SQLAlchemy async 엔진·세션
│   ├── utils/
│   │   ├── crypto.py                # AES-256(Fernet) API 키 암복호화
│   │   ├── format.py                # format_krw_price() 가격 포맷 유틸
│   │   └── time.py                  # KST 시간 유틸
│   ├── models/
│   │   ├── user.py                  # User, SubscriptionTier
│   │   ├── bot_setting.py           # BotSetting (포지션 상태 영속)
│   │   ├── payment.py               # Payment
│   │   └── trade_history.py         # TradeHistory
│   ├── services/
│   │   ├── market_data.py           # MarketDataManager (시장 데이터 캐시)
│   │   ├── ai_trader.py             # AITraderService (OpenAI 호출)
│   │   ├── trading_worker.py        # TradingWorker + WorkerRegistry
│   │   ├── exchange.py              # ExchangeService (CCXT 추상화)
│   │   └── subscription.py          # 구독 연장·만료 알림 루프
│   ├── api/routers/
│   │   ├── payments.py              # TossPayments 콜백·승인
│   │   └── web.py                   # 웹 대시보드
│   └── bot/
│       ├── cogs/
│       │   ├── settings.py          # /설정 커맨드
│       │   ├── subscription.py      # /구독 커맨드
│       │   ├── report.py            # /리포트 커맨드
│       │   ├── ai_trading.py        # /ai 커맨드 (실전 AI 모드)
│       │   └── paper_trading.py     # /모의 커맨드 + AI 모의투자
│       └── tasks/
│           └── ai_manager.py        # AI 펀드 매니저 스케줄러
│
└── scripts/
    ├── backtester.py                # AI 백테스팅 파이프라인 (backtest 브랜치)
    ├── fast_backtest.py             # 로컬 캐시 기반 초고속 퀀트 전략 백테스트 — 추세 돌파 모멘텀 v7 (backtest 브랜치)
    ├── fast_backtest_reversal.py    # 역추세 낙폭과대 반등 스나이핑 백테스트 v1 (backtest 브랜치)
    └── fast_backtest_reversal_v2.py # 역추세 백테스트 v2 — SL 2.5% / RSI < 25 튜닝 (backtest 브랜치)
```

---

## 6. 환경변수 (`.env`)

| 변수                              | 설명                                     |
| --------------------------------- | ---------------------------------------- |
| `DATABASE_URL`                    | PostgreSQL 연결 문자열                   |
| `DISCORD_BOT_TOKEN`               | Discord 봇 토큰                          |
| `DISCORD_GUILD_ID`                | 서버 ID (슬래시 커맨드 동기화용)         |
| `UPBIT_ACCESS_KEY` / `SECRET_KEY` | 서버 공용 업비트 키 (시장 데이터용)      |
| `TOSS_CLIENT_KEY` / `SECRET_KEY`  | TossPayments 키                          |
| `OPENAI_API_KEY`                  | AI 매매 분석용                           |
| `ENCRYPTION_KEY`                  | Fernet 키 (API 키 암호화)                |
| `SECRET_KEY`                      | JWT·세션 서명용                          |
| `SERVER_IP`                       | 업비트 IP 화이트리스트 등록 서버 공인 IP |
| `DASHBOARD_BASE_URL`              | 결제 콜백·리다이렉트 기준 URL            |

> 백테스터 전용 추가 환경변수:
> `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`

---

## 7. 브랜치 역할

| 브랜치     | 상태          | 역할                                                                                 |
| ---------- | ------------- | ------------------------------------------------------------------------------------ |
| `main`     | **운영**      | 실제 서버에 배포된 최신 안정 코드. PR merge 후에만 업데이트.                         |
| `dev`      | **현재 작업** | 모든 기능 브랜치가 합류하는 스테이징 브랜치. CI 검증 후 `main`으로 PR.               |
| `backtest` | 병합 완료     | AI 백테스팅 파이프라인. PR #35 (2026-03-18) · PR #39 (2026-03-18) 병합. 비활성 보관. |
| `feat`     | 보류          | 과거 기능 개발 브랜치 (병합 완료, 현재 비활성).                                      |
| `feat-new` | 보류          | 과거 기능 개발 브랜치 (병합 완료, 현재 비활성).                                      |

### Git Flow 규칙

```
기능 개발 브랜치  →  dev  →  main
                   (PR)      (PR)
```

- 커밋 컨벤션: `<type>(<scope>): <subject>` (Conventional Commits)
- 1 커밋 = 1 논리적 변경 (Atomic Commits 원칙)
- `main` 직접 push 금지 — 반드시 `dev` 경유 PR

---

## 8. 최근 주요 변경 이력 (dev 기준)

| 커밋        | 날짜       | 내용                                                                                                                                                                                                                                                                     |
| ----------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `136164c`   | 2026-03-28 | feat(db): Admin 분석용 TradeHistory·BotSetting 스키마 확장 — bought_at/close_type/ai_version/expected_price 추가, TP_HIT/SL_HIT/AI_FORCE_SELL/MANUAL_OVERRIDE 태깅, force_sell() close_type 파라미터 추가, idempotent 마이그레이션 스크립트 신설 |
| `4044201`   | 2026-03-27 | fix(bot): ManualSellView IDOR·중복청산 보안 취약점 패치 — BotSetting.user_id AND 조건(L174·L381), is_finished() 선제 체크 + self.stop() defer() 이전 이동으로 Race Condition 차단 |
| `17bf07b`   | 2026-03-27 | feat(bot): /내포지션 슬래시 커맨드 신설 — DB 조회 + ManualSellView ephemeral 응답, 포지션 없으면 안내 메시지 |
| `a91c673`   | 2026-03-26 | feat(bot): AI 리포트 하단 수동 청산(Manual Override) UI 추가 — ManualSellView (Select Menu + 즉시 청산 버튼), Race Condition 방지 DB 재검증, 실전/모의 분기, _send_dm_embed view 파라미터 확장 |
| (이전)      | 2026-03-25 | fix(review): Ghost Update 방지 — AI 리뷰 응답 직후 생존 포지션 IN 쿼리 재검증, 워커가 청산한 포지션의 DB UPDATE·DM 리포트 완전 차단, SELL 성공 시 _surviving_ids 즉시 제거 |
| `35aa249`   | 2026-03-25 | fix(engine): MAJOR 엔진 리뷰 완전성 보장 + 에러 알림 강화 — MarketDataManager on-demand fetch, force_sell·DB삽입·잔고조회 실패 DM 알림, 연착륙 종료 is_major_enabled=False, _review_existing_positions 캐시 미스 자동 보완 |
| `9e36275`   | 2026-03-25 | fix(prompt): SWING·SCALPING 언행일치 규칙 + 리스크 감점 + Self-Check 강제 주입 |
| `70e0803`   | 2026-03-25 | feat(report): 비스윙 시간대 메이저 트렌드 엔진 대기 안내 추가 |
| `526ec64`   | 2026-03-25 | feat(report): MAJOR 엔진 가동 시 항상 DM에 분석 요약 표시 (전체 관망 시에도) |
| (V2 개편)   | 2026-03-24 | feat(ai-v2): SWING/SCALPING/MAJOR/ALL 4엔진 Exclusive 드롭다운, BothSettingsModal→AllEnginesModal, 실전/모의 플래그 완전 격리, DM 리포트 엔진명 둔갑 버그 수정 (_group_by_engine + trade_style 전파) |
| (PR 예정)   | 2026-03-22 | feat(ai): AI 트레이딩 코어 V2 — SWING/SCALPING/BOTH 모듈형 엔진, 엔진별 독립 예산·비중, fast_backtest_scalping v1(승률 51.6%), migrate_v2_architecture.py                                                                                                                |
| `072a2ce`   | 2026-03-18 | fix(services): 백엔드 서비스 레이어 전면 리팩토링 및 엣지 케이스 버그 픽스 — 실전 매수 체결가 정확화(fill_price), asyncio 3.10+ 호환, API 타임아웃 10s, review_positions MA50 주입, 0.0 falsy 버그, 모의투자 수수료 차감, 빈 WebSocket 구독 방어, subscription 즉시 체크 |
| `037b7a8`   | 2026-03-18 | fix(market): market_data.py 4h MA50 지표 계산 누락 버그 픽스 — OHLCV_LIMIT 60→100, MA50 계산·캐싱 추가                                                                                                                                                                   |
| `952ff42`   | 2026-03-18 | fix(prompt): 프롬프트 내 BTC 명시 추가 제거                                                                                                                                                                                                                              |
| `7dc13ce`   | 2026-03-18 | fix(ai): BTC 환각 버그 픽스 · 스나이퍼/비스트 레이블 전면 교체                                                                                                                                                                                                           |
| `e509e0a`   | 2026-03-18 | docs(state): UI/UX 피드백 반영 — Embed 정제 + 모달 입력 최적화 기록                                                                                                                                                                                                      |
| `28a8f39`   | 2026-03-18 | refactor(bot): Embed 정제 + AI 실전 모달 입력 단순화 (1회 매수금액 자동 산정)                                                                                                                                                                                            |
| `c7c696e`   | 2026-03-18 | feat(bot): SNIPER/BEAST 듀얼 모드 Embed UI — v7 백테스트 결과 Discord 봇 동기화                                                                                                                                                                                          |
| PR #39 병합 | 2026-03-18 | backtest → dev: SNIPER/BEAST 이중 지갑 백테스트 + /잔고 태그 표시                                                                                                                                                                                                        |
| `9532ad5`   | 2026-03-18 | feat(ai): ai_trader.py — 추세 돌파 스나이퍼 v7 (알트코인 전용) 이식                                                                                                                                                                                                      |
| `1402e49`   | 2026-03-18 | feat(backtest): fast_backtest v7 — 메이저 코인 블랙리스트 필터                                                                                                                                                                                                           |
| `dadff11`   | 2026-03-18 | feat(backtest): fast_backtest v6 — MA50 장기 추세 필터 + RSI 55~70                                                                                                                                                                                                       |
| PR #35 병합 | 2026-03-18 | backtest → dev: fast_backtest.py + backtester.py v7/v8 전략 완성본                                                                                                                                                                                                       |
| `ab115e7`   | 2026-03-17 | refactor(ai_trader): SWING/SCALPING 폐기 → SNIPER/BEAST 단일 프롬프트 통합                                                                                                                                                                                               |
| `6f722dd`   | 2026-03-17 | feat(backtest): 고변동성 잡알트 원천 차단 — stop_loss_pct 9% 상한 하드 룰 (v8)                                                                                                                                                                                           |
| `a6c01a8`   | 2026-03-17 | feat(backtest): TIME_EXIT 72h 연장 + 역추세 스나이핑 진입 조건 v7                                                                                                                                                                                                        |
| `cd1ae2e`   | 2026-03-17 | feat(ai_trader): OpenAI gpt-4o-mini → Anthropic claude-sonnet-4-6 마이그레이션                                                                                                                                                                                           |
| `cfdd41e`   | 2026-03-17 | feat(backtest): 반익반손 Partial TP + 트레일링 스탑 + 고정 비중 20% (v5)                                                                                                                                                                                                 |
| `7303f58`   | 2026-03-17 | feat(backtest): 스마트 청산 로직 v4 (BREAKEVEN + TIME EXIT)                                                                                                                                                                                                              |
| `dc634d9`   | 2026-03-17 | feat(backtest): 스나이퍼 v2 — BTC 국면 필터·손절 7%·Score 90 강화                                                                                                                                                                                                        |
| `3015e6a`   | 2026-03-17 | feat(backtest): 고승률 스나이퍼 전략 + 가상 시드 잔고 추적                                                                                                                                                                                                               |
| `beadd02`   | 2026-03-17 | feat(backtest): AI 퀀트 매니저 백테스팅 파이프라인 구축                                                                                                                                                                                                                  |
| PR #33-34   | 2026-03-16 | fix(db): bot_settings AI 컬럼 DEFAULT 마이그레이션 + CI 배포 추가                                                                                                                                                                                                        |
| PR #31-32   | 2026-03-16 | feat(db): AI 메타데이터 파이프라인 (trade_style / ai_score / ai_reason)                                                                                                                                                                                                  |
| PR #29-30   | 2026-03-16 | feat(ai): ATR 기반 동적 리스크 관리 + 15m 봉 진입 타점 필터                                                                                                                                                                                                              |
| PR #27-28   | 2026-03-16 | feat(ai통계): 총자산 계산 + 포트폴리오 비중 텍스트 차트                                                                                                                                                                                                                  |
| PR #25-26   | 2026-03-15 | fix(ai_manager): 수수료 버퍼 차감·정수화 + 최소 주문 금액 방어                                                                                                                                                                                                           |
| PR #23-24   | 2026-03-15 | fix(worker): 모의투자 기억상실 방어 + AI 불필요 복구 알림 억제                                                                                                                                                                                                           |
| PR #21-22   | 2026-03-15 | feat(ai): AI 전용 예산 한도(ai_budget_krw) + 연착륙/즉시 종료 출구 전략                                                                                                                                                                                                  |
| PR #19-20   | 2026-03-15 | feat(trade): 동전주(100 KRW 미만) 하드 필터 이중 구현                                                                                                                                                                                                                    |
| PR #17-18   | 2026-03-15 | feat(ai): 포트폴리오 슬롯 관리 + score/weight 기반 퀀트 고도화 + SELL 긴급 청산                                                                                                                                                                                          |

---

## 9. 현재 오픈 이슈 / 다음 작업 후보

| 우선순위 | 항목                                                                                                                                               | 관련 브랜치      |
| -------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| 🔴 높음  | **`dev` → `main` PR 생성** — V2 MAJOR 엔진 + Manual Override UI 배포 (`a91c673` 기준)                                                              | `dev` → `main`   |
| 🟡 보통  | **ManualSellView 검증** — DM View 동작 확인, 봇 재시작 후 timeout 처리 검증, 실전/모의 청산 로그 확인                                               | `dev`            |
| 🔴 높음  | **Forward Testing 실시간 검증** — SWING/SCALPING/MAJOR 엔진 모의투자 가동 후 수익률·엔진 태그 정확도 확인                                          | `dev`            |
| 🟡 보통  | **MAJOR 엔진 Alembic 마이그레이션 검증** — `is_major_enabled`, `major_budget`, `major_trade_ratio` 컬럼 운영 DB 반영 확인                           | `dev`            |
| 🟡 보통  | AI 매매 성과 리포트 (실전 이력 집계 → Discord DM, trade_history 테이블 활용)                                                                       | 신규 브랜치 필요 |
| 🟡 보통  | **언행일치 Self-Check 효과 모니터링** — DM 리포트에서 summary "관망" + picks≥90 모순 패턴 재발 여부 확인                                           | `dev`            |
| 🟢 낮음  | `feat`, `feat-new`, `backtest` 브랜치 정리(삭제)                                                                                                   | —                |

---

## 10. 백테스팅 파이프라인 (`backtest` 브랜치)

`scripts/backtester.py` — OpenAI / Anthropic / Gemini 3종 LLM 성능 비교

### 모델 구성

| 어댑터             | 모델 ID                  | JSON 강제 방식                                                 |
| ------------------ | ------------------------ | -------------------------------------------------------------- |
| `OpenAIAdapter`    | `gpt-5.4`                | `response_format={"type": "json_object"}`                      |
| `AnthropicAdapter` | `claude-sonnet-4-6`      | 프롬프트 JSON 지시 + fallback 파서                             |
| `GeminiAdapter`    | `gemini-3.1-pro-preview` | `GenerateContentConfig(response_mime_type="application/json")` |

### 테스트 절차

```bash
# 1. 환경변수 설정 (.env 파일에 있으면 자동 로드됨)
export OPENAI_API_KEY=...
export ANTHROPIC_API_KEY=...
export GEMINI_API_KEY=...

# 2. 단기 검증 (빠른 실행 — 전략 동작 확인용)
python scripts/backtester.py --model all --candles 100 --step 12 --top 20

# 3. 정식 백테스트 (3개 모델 비교 — 전략 성능 측정용)
python scripts/backtester.py --model all --candles 200 --step 6 --top 30

# 4. 단일 모델 실행
python scripts/backtester.py --model gemini --candles 200 --step 6

# 5. 가상 시드 조정
python scripts/backtester.py --model all --candles 200 --budget 500000
```

### CLI 파라미터

| 파라미터           | 기본값      | 설명                                              |
| ------------------ | ----------- | ------------------------------------------------- |
| `--model`          | `anthropic` | 사용 모델 (openai / anthropic / gemini / **all**) |
| `--top`            | `30`        | 분석 대상 상위 코인 수 (거래대금 기준)            |
| `--candles`        | `200`       | 지표 계산용 과거 4h 봉 수                         |
| `--future-candles` | `30`        | 시뮬레이션용 미래 4h 봉 수 (120시간 = 5일)        |
| `--step`           | `6`         | AI 분석 사이클 간격 (4h 봉 수, 기본 6 = 24시간)   |
| `--budget`         | `1_000_000` | 가상 시드 (KRW)                                   |

### 출력

- **콘솔**: 모델별 총 매매·승률·평균PnL·가상 잔고 ROI·AI 토큰 비용
- **`.result/backtest_results_YYYYMMDD_HHMMSS.csv`**: 실행마다 신규 파일 생성

### CSV 컬럼

`Timestamp / Model / Symbol / Score / Weight_Pct / Entry_Price / Target_Profit_Pct / Stop_Loss_Pct / Reason / Sim_Result / Sim_PnL_Pct / Candles_Held / Invested_KRW / PnL_KRW / Balance_KRW / Input_Tokens / Output_Tokens / Estimated_Cost_USD`

---

## 11. 백테스트 실패 분석 & 전략 개선 이력

### 🔴 1차 실패 (승률 20%대) — 원인 분석

| 실패 원인            | 내용                                                                           |
| -------------------- | ------------------------------------------------------------------------------ |
| **손절폭 과소 설정** | stop_loss_pct 2.0~4.5%로 너무 좁아 일반적인 가격 변동(휩쏘)에도 즉시 손절 발동 |
| **BTC 하락장 무시**  | BTC가 하락/횡보 국면임에도 알트코인에 무차별 진입 → 시장 흐름 역행             |
| **진입 문턱 낮음**   | score 80 기준이 너무 낮아 확신 없는 픽도 다수 포함                             |
| **과도한 투입 비중** | weight_pct 50~60%로 단일 픽에 과도한 자금 집중                                 |
| **ERROR 결과 오염**  | 신규 상장 코인 등 미래 데이터 부족 시 PnL 0.0% 기록으로 통계 왜곡              |

### ✅ 스나이퍼 v2 전략 적용 내용 (2026-03-17)

| 항목                 | 변경 전                            | 변경 후                                           |
| -------------------- | ---------------------------------- | ------------------------------------------------- |
| Score 임계값         | 80 (parse_picks 하드 차단)         | **90** (극도 보수적 진입)                         |
| stop_loss_pct 최솟값 | 3.5%                               | **7.0%** (ATR × 2~3배)                            |
| weight_pct 상한      | 없음                               | **30%** (투입 비중 제한)                          |
| BTC 필터 — 강제 관망 | 프롬프트 룰만 존재                 | **유저 프롬프트에 ⛔ 태그 명시** → AI가 즉시 인지 |
| BTC 필터 — 극도 주의 | 없음                               | **유저 프롬프트에 ⚠️ 태그 명시** → score 95+ 강제 |
| JSON 예시 앵커링     | stop 4.5, target 6.0, weight 55    | **stop 7.5, target 12.0, weight 25**              |
| ERROR 결과 처리      | CSV에 PnL 0.0으로 기록 (통계 오염) | **SKIP 처리 — CSV 제외, 잔고 변동 없음**          |
| 미래 봉 검증         | 없음                               | **MIN_FUTURE_CANDLES=5 미달 시 조기 경고 + SKIP** |
| 비정상 봉 필터       | 없음                               | **high<0, low<0, high<low 봉 자동 필터링**        |

### ✅ 스마트 청산 로직 v4 적용 (2026-03-17)

**배경**: 승률 57~71%로 향상됐지만 평균 익절 +5%, 평균 손절 -8%의 역 손익비로 ROI 부진.

| 항목                  | 변경 전                          | 변경 후                                                                       |
| --------------------- | -------------------------------- | ----------------------------------------------------------------------------- |
| 시뮬레이션 결과 타입  | WIN / LOSS / TIMEOUT / SKIP      | WIN / LOSS / **BREAKEVEN** / TIMEOUT / SKIP                                   |
| 본절 이동 (BREAKEVEN) | 없음                             | **High +3.5% 달성 시 플래그 ON → Low +0.5% 하락 시 즉시 청산 (pnl +0.5%)**    |
| 시간 청산 (TIME EXIT) | 없음                             | **12봉(48시간) 경과 후 WIN/BREAKEVEN 미달 시 12번째 close 기준 강제 TIMEOUT** |
| 장기 손절 위험 차단   | 30봉 만기까지 -8% 손절 위험 유지 | **48시간 이후 방향성 없는 포지션 조기 청산**                                  |
| 통계 리포트           | WIN / LOSS / 타임아웃 3가지      | **WIN / LOSS / 본절(BE) / 타임아웃 4가지**                                    |

**신규 상수** (`scripts/backtester.py`):

| 상수                    | 값    | 설명                                         |
| ----------------------- | ----- | -------------------------------------------- |
| `BREAKEVEN_TRIGGER_PCT` | `3.5` | 본절 이동 발동 기준 (진입가 대비 High +3.5%) |
| `BREAKEVEN_EXIT_PCT`    | `0.5` | 본절 청산 레벨 (진입가 대비 +0.5%)           |
| `TIME_EXIT_CANDLES`     | `12`  | 시간 청산 봉 수 (4h × 12 = 48시간)           |

**청산 우선순위** (동일 봉 내 복수 조건 충족 시):

```
[1] WIN       — target_price 도달 (최우선)
[2] BREAKEVEN — breakeven 활성화 후 +0.5% 이하 하락 (LOSS 대체)
[3] LOSS      — 원래 손절선 도달 (breakeven 미활성 구간에서만)
[4] TIMEOUT   — 12봉 경과 (방향성 미결정 조기 청산)
```

### ✅ 수익 극대화 고도화 v5 (2026-03-17)

**배경**: v4 적용 후 평균 PnL 양수 전환 성공. 잃지 않는 구조를 넘어 수익금 극대화를 위해
반익반손 전략 및 포트폴리오 고정 비중 룰 도입.

| 항목                    | 변경 전                                  | 변경 후                                                          |
| ----------------------- | ---------------------------------------- | ---------------------------------------------------------------- |
| 익절 방식               | 목표가 도달 시 100% 전량 즉시 익절 (WIN) | **목표가 도달 시 50% 부분 익절 → 나머지 50% 트레일링 스탑 관리** |
| 나머지 50% 스탑         | N/A                                      | **트레일링 스탑 = 진입가 +0.5% (본절가) 고정**                   |
| 최종 PnL 계산           | `target_pct` 그대로 반환                 | **(첫 50% target_pct + 나머지 50% 청산 PnL) / 2**                |
| 포지션 투입 비중        | 최대 30% 이하 (AI 재량)                  | **무조건 20.0% 고정 (하드 룰, AI 응답값 무시)**                  |
| `parse_picks` weight    | `min(raw_weight, 30.0)`                  | **`weight_pct = 20.0` 항상 고정**                                |
| `_SYSTEM_PROMPT` 섹션 4 | "보수적 비중 최대 30% 이하"              | **"고정 20% 비중 절대 하드 룰"**                                 |

**반익반손 청산 흐름** (`simulate_trade_from_data` v5):

```
[전량 포지션 구간]
  high >= target_price → partial_tp_done = True, partial_tp_pnl = target_pct (50% 익절)
    ↓ 남은 50% 트레일링 스탑 = entry_price × 1.005 로 상향
[반익 완료 후 구간]
  low  <= entry × 1.005 → result="WIN",  pnl = (target_pct + 0.5)   / 2
  i == 11 (TIME EXIT)  → result="WIN",  pnl = (target_pct + close%) / 2
  30봉 소진             → result="WIN",  pnl = (target_pct + last_close%) / 2
```

### ✅ 최종 상용화 로직 v6 확정 (2026-03-17)

**배경**: 3종 LLM 벤치마크 완료. Claude 압도적 우위 확인. 반익반손이 횡보장에서 오히려 수익을 반토막 내는 역효과 확인 → 폐기.

| 항목                        | 변경 전 (v5)                        | 변경 후 (v6)                                     |
| --------------------------- | ----------------------------------- | ------------------------------------------------ |
| 운영 AI 모델                | OpenAI `gpt-4o-mini`                | **Anthropic `claude-sonnet-4-6`** (벤치마크 1위) |
| backtester 기본 모델        | `--model openai`                    | **`--model anthropic`**                          |
| 익절 방식                   | 50% 반익 + 나머지 50% 트레일링 스탑 | **100% 즉시 전량 익절 (수익 극대화)**            |
| BREAKEVEN 방어막            | 유지                                | **유지** (High +3.5% 후 Low +0.5% 하락 시 본절)  |
| 시간 청산 (TIME EXIT)       | 유지                                | **유지** (12봉=48h 경과 시 강제 TIMEOUT)         |
| 고정 비중 20%               | 유지                                | **유지** (AI 응답 무시, parse_picks 하드 고정)   |
| `app/config.py`             | `openai_api_key` 전용               | **`anthropic_api_key` 추가** (운영 키)           |
| `app/services/ai_trader.py` | `AsyncOpenAI` + `gpt-4o-mini`       | **`AsyncAnthropic` + `claude-sonnet-4-6`**       |

**최종 청산 우선순위** (`simulate_trade_from_data` v6):

```
[1] WIN       — target_price 도달 즉시 100% 전량 익절 (트레일링 없음)
[2] BREAKEVEN — High +3.5% 달성 후 Low +0.5% 이하 하락 시 본절 청산
[3] LOSS      — 원래 손절선 도달 (breakeven 미활성 구간만)
[4] TIMEOUT   — 12봉(48h) 경과 강제 청산
```

**`.env` 추가 필요 키:**

```
ANTHROPIC_API_KEY=sk-ant-...    # /ai실전, /ai모의 운영 AI
OPENAI_API_KEY=sk-...           # 백테스트 비교용 (선택)
```

### ✅ 스나이퍼 전략 v7 — 시간 청산 연장 + 역추세 스나이핑 진입 조건 (2026-03-17)

**배경**: 12봉(48h) TIME EXIT가 너무 빠르게 작동해 스윙 파동 완성 전 "가랑비에 옷 젖는" 조기 청산
손실이 누적. 또한 이미 많이 오른 종목 추격 매수로 되돌림 손실 과다 발생.

| 항목                         | 변경 전 (v6)                      | 변경 후 (v7)                                                      |
| ---------------------------- | --------------------------------- | ----------------------------------------------------------------- |
| `TIME_EXIT_CANDLES`          | `12` (48시간)                     | **`18` (72시간)** — 스윙 파동 형성 충분한 인내심 확보             |
| `_SYSTEM_PROMPT` 섹션 5 헤더 | "진입 조건 (모두 충족 시에만 픽)" | **"진입 타점 — 역추세 스나이핑 (낙폭 과대/눌림목 포착)"**         |
| RSI14 진입 구간              | `35~60` (중립~반등 구간)          | **`30~45` 낙폭 과대/눌림목 구간 최우선 탐색**                     |
| 추격 매수 제한               | 과매수(RSI > 65) 금지만 명시      | **MA20 위 RSI 55+ 상승 종목 추격 매수 자제 강력 지시 추가**       |
| 역추세 타점 지시             | 없음                              | **RSI 40 부근 바닥 다지고 반등 조짐 — 역추세 스나이핑 타점 명시** |
| 시스템 프롬프트 섹션 주석    | 스나이퍼 전략 v3                  | **스나이퍼 전략 v4**                                              |

**v7 최종 TIME EXIT 상수** (`scripts/backtester.py`):

| 상수                    | 값       | 설명                                  |
| ----------------------- | -------- | ------------------------------------- |
| `BREAKEVEN_TRIGGER_PCT` | `3.5`    | 본절 이동 발동 기준 (유지)            |
| `BREAKEVEN_EXIT_PCT`    | `0.5`    | 본절 청산 레벨 (유지)                 |
| `TIME_EXIT_CANDLES`     | **`18`** | 4h × 18 = **72시간** (48h → 72h 연장) |

**v7 역추세 스나이핑 진입 조건 핵심**:

```
[최우선] RSI14 30~45 구간:
  → 낙폭 과대 또는 눌림목에서 반등 신호가 있는 종목 최우선 탐색
  → RSI 40 부근 바닥 다지고 반등 조짐 = 이상적 역추세 타점

[추격 매수 자제]:
  → MA20 위에서 RSI 55+ 상승 중인 종목은 픽 자제
  → "이미 많이 오른 종목은 목표가 도달 전 되돌림 위험 크다"

[유지 조건]:
  → score 90 이상 절대 기준 유지
  → 과매수(RSI > 65) 진입 금지 유지
  → 24h 거래대금 50억 KRW 이상 유지
```

### 🎉 상용화 마지막 안전장치 v8 — 고변동성 잡알트 원천 차단 (2026-03-17)

**배경**: v7 적용 결과 — 조기 청산 시간 연장(72h) + 역추세 낙폭 과대 타점 교정이 완벽하게
맞아떨어져 **프로젝트 최초로 양수(+) ROI 달성 성공**.
상용화 마지막 안전장치로, 손익비 구조를 붕괴시키는 고변동성 잡알트를 시스템적으로 차단.

| 항목                         | 변경 전 (v7)                | 변경 후 (v8)                                                    |
| ---------------------------- | --------------------------- | --------------------------------------------------------------- |
| `_SYSTEM_PROMPT` 섹션 2 헤더 | "손절폭 — 휩쏘 방어 (유지)" | **"손절폭 — 범위 제한 (7%~9% 구간)"**                           |
| stop_loss_pct 상한           | 없음 (ATR × 2~3배 무제한)   | **9.0% 하드 상한 — 초과 시 절대 진입 금지**                     |
| 고변동성 차단 방식           | 없음                        | **이중 방어**: 프롬프트 지시 + `parse_picks()` 코드 레벨 백스톱 |
| 딥 손절(-10%+) 위험          | 미차단                      | **완전 차단** (한 번에 큰 손실 입는 잡알트 스킵)                |

**이중 방어 구조** (`parse_picks()` 코드 레벨 백스톱):

```python
stop_loss_pct = max(raw_stop, 7.0)          # 하드 하한: 7% 미만 강제 보정
if stop_loss_pct > 9.0:                     # 하드 상한: 9% 초과 강제 스킵
    continue                                 # AI 지시 위반 시 코드 레벨에서 차단
```

**v8 손절폭 허용 범위**:

```
stop_loss_pct 최솟값: 7.0%  (하드 하한 — 좁은 손절 = 휩쏘 직격 방지)
stop_loss_pct 최댓값: 9.0%  (하드 상한 — 딥 손절 = 손익비 구조 붕괴 방지)
허용 구간: [7.0%, 9.0%]
```

**최종 손절 구조 요약**:

```
stop_loss_pct < 7.0%  → 프롬프트 금지 + 코드에서 7.0%로 강제 상향 보정
stop_loss_pct 7~9%    → 정상 허용 구간 (ATR 3%대 기준)
stop_loss_pct > 9.0%  → 프롬프트 금지 + 코드에서 강제 스킵 (고변동성 잡알트 차단)
```

---

### 📊 fast_backtest.py 퀀트 전략 검증 (2026-03-18 ~ )

`scripts/fast_backtest.py` — 로컬 OHLCV 캐시 기반 초고속 순수 퀀트 전략 백테스트
(LLM 호출 없음 · pandas 불필요 · 표준 라이브러리만 사용)

#### ❌ v1 결과 — 추세 돌파 스나이퍼 v5 대응 (2026-03-18)

**파라미터**: 4h봉 / Close>MA20 / RSI 50~65 / TP +8.0% / SL -4.0% (R:R 2.0:1)

| 항목          | 결과                                      |
| ------------- | ----------------------------------------- |
| 총 거래 횟수  | **1,063회**                               |
| 승률          | **33.0%** ❌ (목표 40% 미달)              |
| 기대값 (EV)   | +0.03% (극소 양수 — 실용 불가)            |
| 🛡️ SNIPER ROI | 미미한 양수 수준                          |
| 🔥 BEAST ROI  | **-46.53%** ❌ (MDD -94.9% — 사실상 파산) |

**주요 실패 원인 — 가짜 돌파(Whipsaw/휩쏘)**:

| 코인     | 승률 | 원인                                     |
| -------- | ---- | ---------------------------------------- |
| BTC/KRW  | ~15% | MA20 돌파 직후 되돌림 반복 (대형 매물대) |
| ETH/KRW  | ~18% | 메이저 코인 공통 — 변동폭 내 가짜 돌파   |
| XRP/KRW  | ~12% | 뉴스/고래 매도에 의한 휩쏘 빈발          |
| DOGE/KRW | ~20% | 밈코인 특성 — 모멘텀 지속성 낮음         |

**⚠️ Pain Point — 추세 돌파 전략의 메이저 코인 취약성**:

- `Close > MA20` 단순 조건은 장기 추세 확인 없이 단기 고점 돌파에 진입 → 되돌림 즉시 손절
- RSI 50~65 구간은 상승 모멘텀 "시작" 구간이지만 MA20 위에서도 중기 하락 추세일 수 있음
- TP +8% 목표는 달성 확률이 낮아 실질 승률 저하의 주요 원인

#### ✅ v6 결과 — MA50 추세 필터 도입 성공 (2026-03-18)

**파라미터**: 4h봉 / Close>MA20 AND Close>MA50 / RSI 55~70 / TP +6.0% / SL -4.0% (R:R 1.5:1)

| 항목         | 결과                                  |
| ------------ | ------------------------------------- |
| 승률         | **40.4%** ✅ (목표 40% 달성!)         |
| 🔥 BEAST ROI | **+20.98%** ✅ (ROI 우상향 전환 성공) |
| 🔥 BEAST MDD | **-88.6%** ❌ (실전 투입 불가 수준)   |

**v6 Pain Point — 메이저 코인 휩쏘로 인한 MDD 과대**:

- 알트코인에서 우수한 성과 → 전체 승률 40.4% / ROI 우상향 달성
- 단, BTC/ETH/XRP/DOGE/ADA/SOL 등 메이저 코인이 승률 20%대로 알트 수익 잠식
- BEAST MDD -88.6%: 실전 투입 시 원금 90% 이상 손실 위험 → 즉시 실전 투입 불가

| 항목          | v1             | v6 (개선)                         | 목적                                           |
| ------------- | -------------- | --------------------------------- | ---------------------------------------------- |
| 추세 필터     | Close > MA20만 | **Close > MA20 AND Close > MA50** | MA50 장기 상승 추세 내 진입만 허용 → 휩쏘 방어 |
| RSI 진입 하한 | 50.0           | **55.0**                          | 모멘텀 "확인" 구간으로 상향 (50~54 중립 제외)  |
| RSI 진입 상한 | 65.0           | **70.0**                          | 상승 모멘텀 유효 구간 확장 (+5 포인트)         |
| TP (익절률)   | +8.0%          | **+6.0%**                         | 목표 도달 확률 향상 → 실질 승률 개선           |
| R:R           | 2.0:1          | **1.5:1**                         | 타이트한 목표로 승률 우선 전략                 |

#### ✅ v7 결과 — 블랙리스트 알트코인 집중 전략 (2026-03-18)

**파라미터**: 4h봉 / Close>MA20 AND Close>MA50 / RSI 55~70 / TP +6.0% / SL -4.0% (R:R 1.5:1)
**블랙리스트**: BTC/ETH/XRP/DOGE/ADA/SOL/SUI/PEPE (8개 메이저 코인 제외)

| 항목         | 결과                                |
| ------------ | ----------------------------------- |
| 승률         | **44.6%** ✅ (목표 40% 초과 달성)   |
| 🔥 BEAST ROI | **+91.4%** ✅ (ROI 대폭 우상향)     |
| 🔥 BEAST MDD | **-19.1%** ✅ (실전 투입 가능 수준) |

**v7 성과 요약**: 블랙리스트 8개 코인 제거로 MDD -88.6% → **-19.1%** (69.5%p 개선)

#### ✅ v7 → `ai_trader.py` 이식 완료 (2026-03-18)

v7 전략(백테스트 검증 완료)을 `app/services/ai_trader.py`의 시스템 프롬프트 + Python 로직에 완벽 이식.

| 변경 항목          | 이전 (v5)              | 이후 (v7)                                                     |
| ------------------ | ---------------------- | ------------------------------------------------------------- |
| **전략 버전**      | 추세 돌파 스나이퍼 v5  | **추세 돌파 스나이퍼 v7 (알트코인 전용)**                     |
| **블랙리스트**     | 없음                   | **`_BLACKLIST` frozenset 8개 코인 — AI에 데이터 미전달**      |
| **RSI 진입 하한**  | 50                     | **55**                                                        |
| **MA50 조건**      | 없음                   | **현재가 > 4h MA50 절대 필수 조건 추가**                      |
| **TP 기본값**      | AI 재량                | **6.0% (백테스트 v7 기준값 명시)**                            |
| **SL 기본값**      | AI 재량 (하드 상한 5%) | **4.0% (백테스트 v7 기준값 명시, 하드 상한 5% 유지)**         |
| **MA50 지표 주입** | 없음                   | **`data.get("ma50")` → 프롬프트에 `MA50={ma50_4h_str}` 포함** |
| **JSON 예시 심볼** | BTC/KRW                | **LINK/KRW** (알트코인 전용 명확화)                           |

**3중 방어 구조** (AI 오판 최소화):

```
[1단계] Python 블랙리스트: 8개 심볼은 AI에 데이터 자체 미전달 (원천 차단)
[2단계] 프롬프트 규칙:    RSI 55+, Close>MA50 절대 조건 명시 (AI 지시)
[3단계] 코드 검증:        stop > 5% 스킵 / R:R < 1.5 자동 보정 (파이썬 안전망)
```

**다음 단계**: Paper Trading 모드(`/ai모의`)로 v7 전략 실시간 Forward Testing 시작

---

#### ✅ v7 백테스트 결과 → Discord 봇 UI/UX 동기화 완료 (2026-03-18)

v7 백테스트 결과를 바탕으로 디스코드 봇 UI/UX 동기화 완료. SNIPER/BEAST 듀얼 모드 선택 유지 및 각 모드별 예상 리스크(MDD) 명시.

**변경 파일**: `app/bot/cogs/paper_trading.py`, `app/bot/cogs/ai_trading.py`

| 항목               | 변경 전                        | 변경 후                              |
| ------------------ | ------------------------------ | ------------------------------------ |
| 투자 성향 드롭다운 | 📊 SWING / ⚡ SCALPING         | 🛡️ SNIPER (20%) / 🔥 BEAST (70%)     |
| 완료 Embed 제목    | "AI 모의투자 설정 완료" (고정) | 모드별 동적 제목 (SNIPER/BEAST 분기) |
| MDD 리스크 고지    | 없음                           | SNIPER: -19% / BEAST: -53% 명시      |
| 공통 엔진 설명     | 없음                           | v7 전략·손익비·필터링 필드 추가      |

**SNIPER 모드**: 가용 시드 20% 투입, MDD -19% 목표 (백테스트 실측)<br>
**BEAST 모드**: 가용 시드 70% 투입, MDD -53% 감수 (하이리스크 하이리턴)<br>
**공통 엔진**: v7 알트코인 4h 모멘텀 돌파 (MA50 상승장 + RSI 55~70), 손익비 1.5:1 강제, BTC·ETH 등 메이저 코인 거래 차단

---

#### ✅ UI/UX 피드백 반영 — Embed 정제 + 입력 폼 최적화 (2026-03-18)

UI/UX 피드백 반영: Embed 메시지에서 MDD 수치(-19%, -53%) 제거 및 '전략' 용어로 순화. 설정 모달에서 불필요한 '1회 매수금액' 입력을 제거하고 모드별 비중(SNIPER=20%, BEAST=70%) 자동 계산 로직 적용 완료.

**변경 파일**: `app/bot/cogs/paper_trading.py`, `app/bot/cogs/ai_trading.py`

| 항목              | 변경 전                                   | 변경 후                            |
| ----------------- | ----------------------------------------- | ---------------------------------- |
| SNIPER 설명       | "MDD를 최소화(-19%)하며..."               | "MDD를 최소화하며..." (수치 제거)  |
| BEAST 설명        | "높은 MDD(-53%)를 감수하고..."            | "리스크를 감수하고..." (수치 제거) |
| 전략 필드명       | "⚙️ 공통 엔진 — v7 알트코인 전략"         | "📋 전략"                          |
| AI 실전 모달 입력 | 1회 매수금액 + 최대종목 + 예산 (3개 필드) | 최대종목 + 예산 (2개 필드)         |
| 1회 매수금액      | 사용자 직접 입력                          | 예산 × 모드 비중 자동 산정         |

**자동 산정 공식**: `1회 매수금액 = 총 운용 예산 × weight_pct / 100`

- SNIPER: 예산의 **20%** → 예: 500,000 KRW → 1회 100,000 KRW
- BEAST : 예산의 **70%** → 예: 500,000 KRW → 1회 350,000 KRW

---

#### ✅ BTC 환각(Hallucination) 버그 픽스 + 스나이퍼/비스트 레이블 전면 교체 (2026-03-18)

**배경**: 모의투자 가동 중 AI가 "BTC 4h RSI가 45~55 구간에 위치하며 시장 국면이 중립적이라 관망"이라는 이유로 매수를 하지 않는 버그 발생.
Python `_BLACKLIST`로 BTC 데이터를 AI에 미전달하고 있음에도, `_CORE_SNIPER_PROMPT`에 "BTC 시장 국면 필터" 룰이 남아 있어 AI가 BTC 수치를 **지어내고(hallucinate)** 과도하게 방어적으로 행동하는 것이 원인이었음.

**변경 파일**: `app/services/ai_trader.py`, `app/bot/tasks/ai_manager.py`, `app/bot/cogs/paper_trading.py`, `app/bot/cogs/ai_trading.py`

| 항목                         | 변경 전                                                   | 변경 후                                                                              |
| ---------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------------ | --------------- | ---------------- | ------------------------------------- | ----------------------------- |
| `_CORE_SNIPER_PROMPT` 1번 룰 | "BTC 시장 국면 필터 (최우선)" — BTC RSI/MA 기준 관망·차단 | **완전 삭제** → 개별 알트코인 모멘텀 돌파 집중                                       |
| 번호 재배정                  | 1(BTC필터)/2(진입)/3(손절)/4(목표가)/5(일반)              | **1(진입)/2(손절)/3(목표가)/4(일반)**                                                |
| 진입 타점 섹션               | "확인된 모멘텀 돌파"                                      | **"개별 알트코인의 확인된 모멘텀 돌파"** + "시장 전반과 무관하게 공격적으로 픽" 강조 |
| BTC 거시 지수 고려           | AI 재량                                                   | **"BTC 등 거시 지수와 무관, 제공 데이터는 알트코인 전용" 안내 추가**                 |
| DM 리포트 style_label        | `"⚡ 단타 (1h 봉)"` / `"📊 스윙 (4h 봉)"`                 | `"🔥 야수 모드 (BEAST)"` / `"🛡️ 스나이퍼 모드 (SNIPER)"`                             |
| 드롭다운 description         | `"MDD -19%                                                | 4h 모멘텀 돌파                                                                       | 알트코인 집중"` | `"4h 모멘텀 돌파 | 알트코인 집중                         | 안정 우상향"` (MDD 수치 제거) |
| 드롭다운 description         | `"MDD -53%                                                | 동일 v7 전략, 고비중 투입"`                                                          | `"동일 v7 전략  | 고비중 투입      | 하이리스크 하이리턴"` (MDD 수치 제거) |
| 리뷰 프롬프트 주석           | `(SWING·SCALPING 공용)`                                   | `(SNIPER·BEAST 공용)`                                                                |

---

### 📉 Phase 7 — 하락장 대응 역추세 전략 검증 + 듀얼 엔진 통합 (2026-03-22)

#### 배경 및 도입 목적

v7 추세 돌파 스나이퍼 전략은 하락/횡보장에서 완벽한 시드 방어(전액 현금 관망)를 보여주었으나,
극단적 혼조세에서 이틀간 진입 횟수가 0회에 수렴하며 **자금 회전율이 극도로 저하**되는 현상 발생.

봇의 '쉬는 시간'을 최소화하고 하락장에서도 수익을 창출하기 위해,
기존의 상승 추세(MA50 상단) 돌파 전략과 완전히 반대되는
**낙폭과대 반등장 스나이핑(MA50 하단 + RSI 과매도)** 서브 전략 도입 결정.

#### 역추세 전략 기본 설계

| 항목            | 추세 돌파 모멘텀 v7 (기존)           | 역추세 Oversold Reversal (신규)     |
| --------------- | ------------------------------------ | ----------------------------------- |
| **진입 조건**   | Close > MA50 (상승 추세) + RSI 55~70 | Close < MA50 (하락 추세) + RSI < 25 |
| **목표**        | 상승 모멘텀 지속 수익                | 기술적 반등 단기 수익               |
| **장세 적합성** | 상승장 / 강세 모멘텀 장              | 하락장 / 급락 후 공황 저점 구간     |
| **전략 철학**   | 추세 추종                            | 역추세 (Contrarian)                 |

#### 역추세 전략 (Oversold Reversal) 개발 이력

| 커밋      | 날짜       | 내용                                                                                     |
| --------- | ---------- | ---------------------------------------------------------------------------------------- |
| `27ca33d` | 2026-03-22 | feat(backtest): fast_backtest_reversal v1 — 낙폭과대 스나이핑 전략 기초 테스트           |
| `d815fbc` | 2026-03-22 | feat(backtest): fast_backtest_reversal v2 — MDD 방어용 파라미터 튜닝 (SL 2.5%, RSI < 25) |
| `d11a0fd` | 2026-03-22 | feat(ai): 듀얼 엔진 통합 — `ai_trader.py` 시스템 프롬프트 전면 재작성 (전략A + 전략B)    |

#### 📊 fast_backtest_reversal.py — Oversold Reversal v1 (2026-03-22)

**파라미터**: 4h봉 / Close < MA50 (하락 추세 확인) / RSI < 30 (극단적 과매도) / TP +3.0% / SL -3.0% (R:R 1.0:1)

| 항목         | 결과                                         |
| ------------ | -------------------------------------------- |
| 승률         | **51.5%** ✅ (Edge 확인 — 수익 구조 존재)    |
| 🔥 BEAST MDD | **-60.9%** ❌ (하락장 특성상 연속 손절 집중) |

**분석**: 승률 50% 초과로 통계적 Edge는 확인됨. 단, 하락장 특성상 MDD가 지나치게 높음 → v2 파라미터 개선.

#### 📊 fast_backtest_reversal_v2.py — Oversold Reversal v2 Tuned (2026-03-22)

**파라미터**: 4h봉 / Close < MA50 / **RSI < 25** (더 깊은 과매도) / TP +3.0% / **SL -2.5%** (R:R 1.2:1)

| 항목          | v1    | v2 (Tuned) | 변경 의도                                         |
| ------------- | ----- | ---------- | ------------------------------------------------- |
| RSI 진입 상한 | 30.0  | **25.0**   | 더 깊은 공포/패닉 구간만 진입 → 반등 폭 증가 기대 |
| SL (손절률)   | 3.0%  | **2.5%**   | 손절 타이트화 → MDD 억제                          |
| R:R           | 1.0:1 | **1.2:1**  | 기대값 개선                                       |
| 승률 목표     | ≥50%  | **≥55%**   | RSI 극값 필터로 승률 상향 기대                    |

#### ✅ 듀얼 엔진 통합 완료 (2026-03-22, 커밋 `d11a0fd`)

v2 백테스트 검증 완료 후 `ai_trader.py`의 `_CORE_SNIPER_PROMPT`를 전면 재작성하여
**[상승장 = 전략A 모멘텀 돌파 / 하락장 = 전략B 낙폭과대 스나이핑]** 이중 엔진으로 통합 완료.

| 항목          | 통합 전 (단일 엔진)    | 통합 후 (듀얼 엔진)                                   |
| ------------- | ---------------------- | ----------------------------------------------------- |
| 전략 수       | 1개 (추세 돌파 v7)     | **2개 (전략A + 전략B)**                               |
| BTC 필터      | 하락 시 전면 관망      | 하락 시 전략A 금지, **전략B RSI<25 허용**             |
| 손절 섹션     | 전략 불문 4% / 상한 5% | 전략A: **4% / 상한 5%** / 전략B: **2.5% / 상한 3%**   |
| 목표가 섹션   | TP 6% (R:R ≥ 1.5)      | 전략A: **6% (R:R 1.5)** / 전략B: **3% (R:R 1.2)**     |
| R:R 코드 하한 | `stop × 1.5`           | **`stop × 1.2`** (전략B 수용)                         |
| reason 태그   | 없음                   | **`[전략A 추세돌파]` / `[전략B 역추세]` 명시 의무화** |

```
[듀얼 엔진 작동 구조]
  전략A (상승장): Close > MA50 + RSI 55~70 → TP 6% / SL 4% (R:R 1.5:1)
  전략B (하락장): Close < MA50 + RSI < 25  → TP 3% / SL 2.5% (R:R 1.2:1)
  → 시장 국면 불문 항상 수익 기회 탐색
```

---

## 12. AI 트레이딩 봇 개발 여정 (커밋 이력 기반)

> 2026-03-12 착수 → 2026-03-22 기준. git log 전수 조회 결과를 7단계로 정리.

---

### Phase 1 — 기반 인프라 구축 (2026-03-12~13)

| 커밋      | 내용                                                                           |
| --------- | ------------------------------------------------------------------------------ |
| `07785db` | feat(bot): 온보딩 Embed 업그레이드 + /키등록 보안 가이드 추가                  |
| `793a810` | feat(scripts): 평문 API 키 → Fernet 암호화 1회성 마이그레이션                  |
| `6b70a60` | fix(trade): 매도 전 실제 잔고 확인 → insufficient_funds_ask 방지               |
| `77e7e1c` | feat(report): 1시간 주기 정기 수익률 보고 DM 기능 추가                         |
| `3c83d6a` | feat(model): 정기 보고 설정 컬럼 추가 (report_enabled / report_interval_hours) |
| `b2a0494` | feat(report): /보고설정 커맨드 + 사용자별 보고 주기 설정 반영                  |

**완성된 기반**: Discord 슬래시 커맨드, 업비트 API 연동, Fernet 암호화, 정기 보고 DM, TossPayments 결제 콜백

---

### Phase 2 — AI 트레이딩 코어 구현 (2026-03-14)

| 커밋      | 내용                                                                             |
| --------- | -------------------------------------------------------------------------------- |
| `b644166` | feat(market): MarketDataManager 신규 생성 — AI 트레이딩용 Top N 시장 데이터 캐싱 |
| `a5dc58c` | feat(ai): Task 1 — AI 환경 설정 및 DB 컬럼 추가                                  |
| `ff60bee` | feat(bot): Task 2 — /ai설정 슬래시 커맨드 추가 (VIP 전용)                        |
| `564bd80` | feat(ai): Task 3+4 — AITraderService + AIFundManagerTask (4h 스케줄러)           |
| `98b5938` | feat(ai): AI 포지션 고도화 — 리뷰·슬롯 관리·워커 DB 동기화                       |
| `9f100d2` | feat(ai): 업비트 4h 봉 정각 KST 동기화 + 10초 롤오버 대기                        |
| `47b4131` | feat(ux): 다음 AI 분석 예정 시간 안내 추가                                       |

**완성된 기반**: `MarketDataManager` → `AITraderService(OpenAI gpt-4o-mini)` → `AIFundManagerTask` 4h 스케줄러 파이프라인. VIP 전용 /ai설정 커맨드.

---

### Phase 3 — AI 고도화 + 모의투자 (2026-03-15~16)

| 커밋      | 내용                                                                          |
| --------- | ----------------------------------------------------------------------------- |
| `531ae9a` | feat(paper-trading): 모의투자 + /AI통계 기능 구현                             |
| `9fda026` | refactor(paper-trading): 모의투자를 AI 자동매매 ON/OFF 방식으로 전면 개편     |
| `5b67db0` | feat(ai): UX 개편 — 커맨드 리뉴얼 + 실전/모의 이중 사이클 격리                |
| `3b5e38a` | feat(ai): SWING/SCALPING 투자 성향 분리 + 매시간 스케줄러 도입                |
| `add2b21` | feat(ux): AI 설정 UI를 드롭다운 2단계 플로우로 개편                           |
| `c04f2b7` | feat(trade): 100원 미만 동전주 하드 필터 이중 구현                            |
| `53eb5fa` | feat(ai): AI 전용 예산 한도(ai_budget_krw) + 연착륙/즉시 종료 출구 전략       |
| `cd39532` | fix(worker): 모의투자 기억상실 방어 + AI 불필요한 포지션 복구 알림 억제       |
| `086bd07` | fix(ai_manager): 실거래 매수 시 수수료 버퍼 차감·정수화 + 최소 주문 금액 방어 |
| `6416fc8` | feat(ai통계): 총자산 기준 잔고 계산 + 포트폴리오 비중 텍스트 차트             |
| `ebef935` | feat(db): AI 메타데이터 파이프라인 (trade_style / ai_score / ai_reason)       |
| `533aa26` | feat(ai): ATR 기반 동적 리스크 관리 + 15m 봉 진입 타점 필터 도입              |
| `d45fb7c` | fix(db): bot_settings AI 컬럼 명시적 DEFAULT 값 마이그레이션 추가             |

**완성된 기반**: 실전·모의 이중 사이클 완전 격리, SWING/SCALPING 성향 분리, 동전주 방어, 예산 한도, 연착륙 출구 전략, AI 메타데이터 파이프라인

---

### Phase 4 — 백테스팅 파이프라인 + 전략 진화 (2026-03-17)

| 커밋      | 내용                                                                           |
| --------- | ------------------------------------------------------------------------------ |
| `beadd02` | feat(backtest): AI 퀀트 매니저 백테스팅 파이프라인 구축 (OpenAI 기반)          |
| `15daeb1` | feat(backtest): Time-Stepping 백테스트 + Gemini 최신 SDK 마이그레이션          |
| `3015e6a` | feat(backtest): 고승률 스나이퍼 전략 + 가상 시드 잔고 추적                     |
| `dc634d9` | feat(backtest): 스나이퍼 v2 — BTC 국면 필터·손절 7%·Score 90·예외처리 강화     |
| `7303f58` | feat(backtest): 스마트 청산 로직 v4 (BREAKEVEN 본절 이동 + TIME EXIT 48h)      |
| `cfdd41e` | feat(backtest): 반익반손 Partial TP + 트레일링 스탑 + 고정 비중 20% (v5)       |
| `a4e4bd8` | feat(backtest): claude-sonnet-4-6 기본 모델 고정 + 전량 익절 복구 (v6)         |
| `cd1ae2e` | feat(ai_trader): OpenAI gpt-4o-mini → Anthropic claude-sonnet-4-6 마이그레이션 |
| `a6c01a8` | feat(backtest): TIME_EXIT 72h 연장 + 역추세 스나이핑 진입 조건 (v7)            |
| `6f722dd` | feat(backtest): 고변동성 잡알트 원천 차단 — stop_loss_pct 9% 상한 하드 룰 (v8) |

**핵심 전략 진화 요약**:

| 버전 | 핵심 변경                           | 백테스트 결과                      |
| ---- | ----------------------------------- | ---------------------------------- |
| v1   | 기초 스나이퍼 (score 80, SL 2~4.5%) | 승률 20%대 (실패)                  |
| v2   | BTC 국면 필터·Score 90·SL 7%        | 승률 57~71% (역 손익비로 ROI 부진) |
| v4   | BREAKEVEN 본절 이동 + TIME EXIT 48h | 평균 PnL 양수 전환                 |
| v5   | 반익반손 50% + 고정 비중 20%        | 횡보장 수익 반토막 (역효과)        |
| v6   | Claude 채택·전량 익절 복귀          | Claude 압도적 우위 확인            |
| v7   | TIME EXIT 72h·역추세 스나이핑       | **최초 양수 ROI 달성**             |
| v8   | stop_loss 9% 이중 차단              | 고변동성 잡알트 원천 차단          |

---

### Phase 5 — fast_backtest + v7 알트코인 전용 이식 (2026-03-18)

| 커밋      | 내용                                                                              |
| --------- | --------------------------------------------------------------------------------- |
| `d4b9c8e` | feat(backtest): fast_backtest.py — 로컬 OHLCV 캐시 기반 초고속 순수 퀀트 백테스터 |
| `dadff11` | feat(backtest): fast_backtest v6 — MA50 장기 추세 필터 + RSI 55~70 도입           |
| `1402e49` | feat(backtest): fast_backtest v7 — 메이저 코인 블랙리스트 8종 필터                |
| `9532ad5` | feat(ai): ai_trader.py — 추세 돌파 스나이퍼 v7 (알트코인 전용) 이식               |

**fast_backtest 전략 진화**:

| 버전 | 파라미터                                                 | 결과                                                  |
| ---- | -------------------------------------------------------- | ----------------------------------------------------- |
| v1   | Close>MA20 / RSI 50~65 / TP 8% / SL 4%                   | 승률 33%, BEAST ROI -46.5%                            |
| v6   | +Close>MA50 / RSI 55~70 / TP 6% / SL 4%                  | 승률 40.4%, BEAST ROI +21%                            |
| v7   | +블랙리스트 8종 (BTC·ETH·XRP·DOGE·ADA·SOL·SUI·PEPE 제거) | **승률 44.6%, BEAST ROI +91.4%, BEAST MDD -19.1%** ✅ |

---

### Phase 6 — Discord UI 동기화 + 버그픽스 (2026-03-18)

| 커밋      | 내용                                                                              |
| --------- | --------------------------------------------------------------------------------- |
| `b6fa78e` | feat(settings): /잔고 Embed에 SNIPER/BEAST 모드 태그 표시 추가                    |
| `ab115e7` | refactor(ai_trader): SWING/SCALPING 폐기 → SNIPER/BEAST 단일 역추세 프롬프트 통합 |
| `c7c696e` | feat(bot): SNIPER/BEAST 듀얼 모드 Embed UI — v7 백테스트 결과 Discord 봇 동기화   |
| `28a8f39` | refactor(bot): Embed 정제 + AI 실전 모달 입력 단순화 (1회 매수금액 자동 산정)     |
| `7dc13ce` | fix(ai): BTC 환각 버그 픽스 · 스나이퍼/비스트 레이블 전면 교체                    |
| `952ff42` | fix(prompt): \_CORE_SNIPER_PROMPT 추가 BTC 명시 제거                              |

**핵심 버그 픽스 — BTC 환각(Hallucination)**:

- **증상**: AI가 "BTC RSI 45~55 중립 국면"이라며 매수 없이 관망
- **원인**: `_BLACKLIST`로 BTC 데이터를 AI에 미전달하는데 프롬프트에 BTC RSI 기준 룰이 남아 있어 AI가 BTC 값을 지어냄
- **수정**: `_CORE_SNIPER_PROMPT`에서 "BTC 시장 국면 필터" 섹션 완전 삭제 + 개별 알트코인 모멘텀 집중 룰로 대체

---

### Phase 7 — 하락장 대응 역추세 전략 검증 + 듀얼 엔진 통합 (2026-03-22)

| 커밋      | 날짜       | 내용                                                                                                |
| --------- | ---------- | --------------------------------------------------------------------------------------------------- |
| `27ca33d` | 2026-03-22 | feat(backtest): fast_backtest_reversal v1 — 낙폭과대 스나이핑 전략 기초 테스트                      |
| `d815fbc` | 2026-03-22 | feat(backtest): fast_backtest_reversal v2 — MDD 방어용 파라미터 튜닝 (SL 2.5%, RSI < 25)            |
| `f8dd9ef` | 2026-03-22 | docs(state): Phase 7 역추세 전략 검증 이력 및 전천후 엔진 통합 목표 기록                            |
| `d11a0fd` | 2026-03-22 | feat(ai): 듀얼 엔진 통합 — 전략A 추세돌파 + 전략B 낙폭과대 반등 `ai_trader.py` 통합                 |
| `d9f3b3a` | 2026-03-22 | feat(bot): Discord UI 텍스트 — 듀얼 엔진 전략 반영 (VIP embed, 드롭다운 설명, 완료 Embed 전략 설명) |
| `93bb575` | 2026-03-22 | feat(bot): DM 리포트 `_build_unified_report_embed` — "⚔️ 듀얼 스윙" 레이블 + 관망 텍스트 다변화     |

**Phase 7 결과**:

- 역추세 전략 v1: 승률 51.5% (Edge 확인), BEAST MDD -60.9% → 파라미터 개선 필요
- 역추세 전략 v2: RSI<25 + SL 2.5% → MDD 억제, 승률 55% 목표
- 듀얼 엔진 통합: `ai_trader.py` 시스템 프롬프트 전면 재작성 → 하락장 대응 역량 확보

---

### Phase 8 — 1시간 봉 단기 모멘텀 스캘핑(SCALPING) 전략 검증 (2026-03-22 ~ )

| 커밋  | 내용                                                                                  |
| ----- | ------------------------------------------------------------------------------------- |
| `TBD` | feat(backtest): fast_backtest_scalping v1 — 1시간 봉 단기 모멘텀 스캘핑 백테스터 추가 |
| `TBD` | docs(state): PROJECT_STATE.md 스캘핑 검증 페이즈 기록 추가                            |

**배경 및 목적 (왜 1시간 봉 스캘핑인가?)**:

- 메인 엔진에 `SWING(4시간 봉)` 듀얼 엔진(추세 돌파 + 낙폭 역추세)이 안정적으로 통합되었으나, 4시간 캔들 특성상 진입 기회가 상대적으로 적음.
- 자금 회전율을 극대화하고 봇의 활동성을 높이기 위해, 현재 껍데기만 존재하는 `SCALPING` 모드에 탑재할 **1시간 봉 단타 전략** 검증 시작.

**스캘핑 전략 (1h Momentum) 테스트 셋업**:

- **타임프레임**: 1h (1시간 봉)
- **진입 조건**: `Close > MA20` (단기 상승 추세) & `RSI 60~75` (강한 모멘텀 초입)
- **청산 조건**: 짧게 치고 빠지는 타이트한 손익비 구성 (익절 **2.0%** / 손절 **1.5%**, R:R 1.33:1)
- **핵심 과제**: 거래 횟수가 4시간 봉 대비 폭증함에 따라, 잦은 휩쏘(거짓 돌파)와 업비트 수수료(0.05%) 누수를 극복할 수 있는 승률(최소 45% 이상)과 MDD 방어력 확인 필수.

---

### Phase 9 — 메이저 코인 전용 엔진 전략 피벗: Bollinger Ping-Pong → Trend Catcher (2026-03-23)

#### [2026-03-23 업데이트]

**추가/변경 내역**: 메이저 코인 전용 엔진 로직을 '박스권 역추세(Bollinger Ping-Pong)'에서 '정배열 추세 돌파(Trend Catcher)'로 전면 피벗 및 신규 백테스트(`fast_backtest_trend.py`) 진행.

**업데이트 사유 (Reasoning)**:

1차 볼린저 핑퐁 백테스트(`fast_backtest_bollinger_v2.py`) 결과, 메이저 코인(BTC·ETH·SOL·XRP·ADA·DOGE 등)에서 역추세 매매(BB 하단 이탈 반등 포착)의 기대값이 확정적 음수(Negative EV)로 증명됨.

- **Case A** (1h, 2.0σ, TP2/SL1.5): 승률이 존재하나 수수료·슬리피지 반영 시 장기 수익 불가
- **Case B** (4h+EMA200 필터, 2.0σ, TP3/SL3): EMA200 필터로 신호 급감, 기대값 여전히 음수
- **Case C** (4h, 2.8σ 극단 과매도, TP3/SL3): 진입 빈도 극소 → 통계적 유의성 미달

**근본 원인**: 메이저 코인은 유동성이 풍부하고 시장 주도 세력이 강해, BB 이탈 후 즉각 반등하는 '핑퐁' 패턴이 아닌 **추세 연장(Trend Continuation)** 성향이 지배적. 역추세 매매는 메이저 코인에서 구조적 음수 EV 전략임이 데이터로 확인됨.

**전략 피벗 결정**: 역추세(Contrarian) 폐기 → **정배열 추세 돌파(Trend Catcher)** 채택

#### 신규 백테스트: fast_backtest_trend.py

**진입 조건** (3중 필터 — 4h 봉 기준):

| 조건 | 설명 |
|---|---|
| `Close > EMA200` | 장기 우상향 추세 확인 (상승장 필터) |
| `EMA20 > EMA50` | 단·중기 정배열 확인 (모멘텀 가속) |
| `Close > BB Upper(20, 2.0σ)` | 저항 돌파 확인 (추세 돌파 신호) |

**WHITELIST**: BTC, ETH, SOL, XRP, ADA, DOGE, DOT, TRX (KRW 거래쌍, 8종 메이저)

**3가지 TP/SL 케이스 비교**:

| 케이스 | TP | SL | R:R | 손익분기 승률 |
|---|---|---|---|---|
| Case A | 4% | 2% | 2.0:1 | 33.3% |
| Case B | 6% | 3% | 2.0:1 | 33.3% |
| Case C | 8% | 3% | 2.6:1 | 27.3% |

**`build_entry_signals()` 최적화**: 3개 케이스가 동일한 진입 조건을 공유하므로 심볼당 1회 신호 계산 후 공유 → 연산량 1/3 절감.

| 커밋 | 날짜 | 내용 |
|---|---|---|
| `TBD` | 2026-03-23 | feat(backtest): fast_backtest_trend.py — 정배열 추세 돌파 Trend Catcher 백테스터 |
| `TBD` | 2026-03-23 | docs(state): PROJECT_STATE.md Phase 9 전략 피벗 기록 추가 |

---

---

## 13. 과거 스키마 변경 히스토리 (삭제된 마이그레이션 스크립트 대체 기록)

> 아래 스크립트들은 DB에 성공적으로 적용 완료 후, V2 대청소(2026-03-23) 시점에 삭제되었습니다.
> 컬럼 존재 여부는 해당 SQLAlchemy 모델을 참조하세요.

| 삭제된 스크립트 | 적용된 컬럼 | 대상 테이블 |
|---|---|---|
| `add_report_columns.py` | `report_enabled`, `report_interval_hours`, `last_report_sent_at` | `users` |
| `add_ai_columns.py` | `ai_mode_enabled`, `ai_trade_amount`*, `ai_budget_krw`* | `users` |
| `add_ai_max_coins_column.py` | `ai_max_coins` | `users` |
| `add_ai_paper_mode_column.py` | `ai_paper_mode_enabled` | `users` |
| `add_ai_renewal_columns.py` | `is_ai_managed` | `bot_settings` |
| `add_ai_trade_style_column.py` | `ai_trade_style`* | `users` |
| `add_ai_budget_shutdown_columns.py` | `ai_is_shutting_down` | `users` |
| `add_ai_log_columns.py` | `trade_style`, `ai_score`, `ai_reason` | `bot_settings`, `trade_history` |
| `add_bot_setting_ai_columns.py` | `ai_score`, `ai_reason` (중복분) | `bot_settings` |
| `add_paper_trading.py` | `is_paper_trading`, `virtual_krw`, `trade_history` 테이블 | `users`, `bot_settings` |
| `add_ai_v2_engine_columns.py` | `ai_engine_mode`, `ai_swing_budget_krw`, `ai_swing_weight_pct`, `ai_scalp_budget_krw`, `ai_scalp_weight_pct` | `users` |
| `migrate_keys.py` | 업비트 API 키 Fernet 암호화 1회 전환 (데이터 마이그레이션) | `users` |
| `migrate_v2_architecture.py` | SNIPER/BEAST → SWING/SCALPING 엔진 명칭 변환 (데이터 마이그레이션) | `users` |

> `*` 표시 컬럼: V1 레거시로 판단, `add_major_engine_columns.py`에서 **`ALTER TABLE DROP COLUMN`** 으로 물리적 삭제됨.

**삭제된 폐기 백테스트 스크립트:**

| 파일 | 폐기 사유 |
|---|---|
| `fast_backtest_reversal.py` | Phase 7 역추세 검증 → Negative EV 확증, 전략 폐기 |
| `fast_backtest_reversal_v2.py` | 동일 사유 |
| `fast_backtest_bollinger.py` | V2(`fast_backtest_bollinger_v2.py`)로 대체 완료 |

---

### 현재 시스템 상태 (2026-03-23 기준)

```
✅ 완료된 것들
  - AI 실전 매매 (VIP): claude-sonnet-4-6 기반 SWING/SCALPING/MAJOR 3-엔진 모듈형
  - AI 모의투자 (전 등급): 동일 전략, 가상 KRW 잔고 시뮬레이션
  - 듀얼 엔진 (전략A 추세돌파 + 전략B 낙폭과대 반등) — 전천후 매매 가능
  - MAJOR 엔진 DB 스키마·UI 추가 (Trend Catcher 전략 전용, 백테스트 결과 이식 예정)
  - 3중 방어 (Python 블랙리스트 + 프롬프트 룰 + 코드 검증)
  - 연착륙/즉시 종료 출구 전략
  - DM 리포트 (정기 보고 + AI 매매 리포트)
  - TossPayments 결제 + 구독 만료 알림
  - Bollinger Ping-Pong Negative EV 확증 → 전략 폐기
  - Trend Catcher (정배열 추세 돌파) 백테스터 구축 (fast_backtest_trend.py)
  - V2 대청소: 레거시 마이그레이션 스크립트 13개 + 폐기 백테스트 3개 삭제
  - V1 잔재 컬럼(ai_trade_style, ai_budget_krw, ai_trade_amount) 물리적 DROP

🎯 다음 단계 (Forward Testing)
  - fast_backtest_trend.py 3-Case 결과 분석 → 최적 TP/SL 케이스 선정
  - Trend Catcher 전략을 ai_trader.py MAJOR 엔진에 이식 (ai_trader.py 업데이트)
  - SWING/SCALPING/MAJOR 모의투자 실시간 가동 → 승률·MDD·ROI 검증
  - 듀얼 엔진 하락장 대응 성능 검증 (전략B 실전 데이터 수집)
  - ANTHROPIC_API_KEY .env 등록 필수 (운영 가동 전제 조건)
```
