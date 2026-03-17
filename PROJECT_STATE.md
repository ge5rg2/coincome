# CoinCome — 프로젝트 현황 (PROJECT STATE)

> **기준일**: 2026-03-17 (최종 수정: 2026-03-17 — 스나이퍼 v2 전략 적용)
> **현재 작업 브랜치**: `backtest`
> **최신 안정 브랜치**: `dev` (커밋 `4abaaf5`)

---

## 1. 프로젝트 개요

업비트(Upbit) 기반 Discord 자동 매매 봇 MVP.
Discord 슬래시 커맨드로 봇 설정·구독·리포트를 제어하고, FastAPI 서버가 결제 콜백을 처리한다.
AI 펀드 매니저(OpenAI GPT 기반)가 시장을 자동 분석하고 코인을 픽해 실전·모의투자를 병행 운영한다.

### 기술 스택

| 분류 | 기술 |
|---|---|
| **언어** | Python 3.12 |
| **API 서버** | FastAPI 0.115 + Uvicorn |
| **Discord 봇** | discord.py 2.4 |
| **DB** | PostgreSQL 18 (SQLAlchemy 2.0 async + asyncpg) |
| **마이그레이션** | Alembic |
| **거래소 연동** | CCXT 4.4 (upbit) |
| **AI 분석** | OpenAI `gpt-4o-mini` (운영) |
| **결제** | TossPayments |
| **배포** | Docker + docker-compose |
| **로케일/TZ** | `ko_KR.UTF-8` / `Asia/Seoul` |

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
              │  │ OpenAI gpt-4o-mini 호출                   ││
              │  │ SWING(4h) / SCALPING(1h) 전략 분기        ││
              │  │ analyze_market() / review_positions()     ││
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

### AI 펀드 매니저 루프 (`app/bot/tasks/ai_manager.py`)

```
매시간(SWING: 6시간 주기 / SCALPING: 1시간 주기)
  ① 연착륙 체크 (ai_is_shutting_down → 신규 매수 차단)
  ② 실전 가용 예산 계산 (ai_budget_krw 모드 vs 무제한)
  ③ review_existing_positions() → HOLD / UPDATE / SELL 결정
  ④ _buy_new_coins() → analyze_market() → score≥80 픽 선택
     └─ 동전주 하드가드 (100 KRW 미만 진입 차단)
     └─ safe_trade_amount = int(trade_amount × 0.999)  # 수수료 버퍼
  ⑤ 실전·모의 동시 실행 (ai_mode + ai_paper_mode)
  ⑥ DM 리포트 전송 (통합 임베드)
```

---

## 3. 구독 등급 (Subscription Tier)

| 등급 | 최대 코인 수 | 최대 1회 투자 | AI 모드 |
|---|---|---|---|
| **FREE** | 2개 | 100,000 KRW | ✗ (모의만 가능) |
| **PRO** | 무제한 | 100,000,000 KRW | ✗ (모의만 가능) |
| **VIP** | 무제한 | 100,000,000 KRW | ✓ 실전 AI 가능 |

> 결제: TossPayments `/confirm` (서버 승인) + `/callback` (웹훅)
> 구독 만료 알림: `app/services/subscription.py` 백그라운드 루프

---

## 4. DB 모델 요약

### `users`
| 컬럼 | 설명 |
|---|---|
| `user_id` | Discord 사용자 ID (PK) |
| `upbit_access_key` / `secret_key` | AES-256(Fernet) 암호화 저장 |
| `subscription_tier` | FREE / PRO / VIP |
| `ai_mode_enabled` | AI 실전 매매 ON/OFF |
| `ai_trade_style` | SWING / SCALPING |
| `ai_max_coins` | AI 동시 보유 최대 코인 수 (기본 3) |
| `ai_trade_amount` | AI 1회 매수 금액 (KRW) |
| `ai_budget_krw` | AI 운용 예산 한도 (0=무제한) |
| `ai_is_shutting_down` | 연착륙 모드 (신규 매수 중단) |
| `ai_paper_mode_enabled` | AI 모의투자 ON/OFF |
| `virtual_krw` | 모의투자 가상 KRW 잔고 (기본 1천만) |

### `bot_settings`
| 컬럼 | 설명 |
|---|---|
| `symbol` | 코인 심볼 (BTC/KRW 형식) |
| `buy_amount_krw` | 매수 금액 |
| `target_profit_pct` / `stop_loss_pct` | 익절·손절 기준 |
| `is_paper_trading` | 모의투자 여부 |
| `is_ai_managed` | AI 자동 생성 포지션 여부 |
| `trade_style` | AI 매수 당시 전략 (SWING/SCALPING) |
| `ai_score` | AI 부여 종목 점수 (0~100) |
| `ai_reason` | AI 매수 근거 텍스트 |

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
    └── backtester.py                # AI 백테스팅 파이프라인 (backtest 브랜치)
```

---

## 6. 환경변수 (`.env`)

| 변수 | 설명 |
|---|---|
| `DATABASE_URL` | PostgreSQL 연결 문자열 |
| `DISCORD_BOT_TOKEN` | Discord 봇 토큰 |
| `DISCORD_GUILD_ID` | 서버 ID (슬래시 커맨드 동기화용) |
| `UPBIT_ACCESS_KEY` / `SECRET_KEY` | 서버 공용 업비트 키 (시장 데이터용) |
| `TOSS_CLIENT_KEY` / `SECRET_KEY` | TossPayments 키 |
| `OPENAI_API_KEY` | AI 매매 분석용 |
| `ENCRYPTION_KEY` | Fernet 키 (API 키 암호화) |
| `SECRET_KEY` | JWT·세션 서명용 |
| `SERVER_IP` | 업비트 IP 화이트리스트 등록 서버 공인 IP |
| `DASHBOARD_BASE_URL` | 결제 콜백·리다이렉트 기준 URL |

> 백테스터 전용 추가 환경변수:
> `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`

---

## 7. 브랜치 역할

| 브랜치 | 상태 | 역할 |
|---|---|---|
| `main` | **운영** | 실제 서버에 배포된 최신 안정 코드. PR merge 후에만 업데이트. |
| `dev` | **통합** | 모든 기능 브랜치가 합류하는 스테이징 브랜치. CI 검증 후 `main`으로 PR. |
| `backtest` | **개발 중** | AI 백테스팅 파이프라인 (`scripts/backtester.py`). PR #35 오픈 중. |
| `feat` | 보류 | 과거 기능 개발 브랜치 (병합 완료, 현재 비활성). |
| `feat-new` | 보류 | 과거 기능 개발 브랜치 (병합 완료, 현재 비활성). |

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

| PR | 내용 |
|---|---|
| #35 | **[Open]** AI 백테스팅 파이프라인 (`scripts/backtester.py`) — `backtest` 브랜치 |
| #33-34 | fix(db): bot_settings AI 컬럼 DEFAULT 마이그레이션 + CI 배포 추가 |
| #31-32 | feat(db): AI 메타데이터 파이프라인 (`trade_style` / `ai_score` / `ai_reason`) |
| #29-30 | feat(ai): ATR 기반 동적 리스크 관리 + 15m 봉 진입 타점 필터 |
| #27-28 | feat(ai통계): 총자산 계산 + 포트폴리오 비중 텍스트 차트 |
| #25-26 | fix(ai_manager): 수수료 버퍼 차감·정수화 + 최소 주문 금액 방어 |
| #23-24 | fix(worker): 모의투자 기억상실 방어 + AI 불필요 복구 알림 억제 |
| #21-22 | feat(ai): AI 전용 예산 한도(`ai_budget_krw`) + 연착륙/즉시 종료 출구 전략 |
| #19-20 | feat(trade): 동전주(100 KRW 미만) 하드 필터 이중 구현 |
| #18 | feat(ui): 동전주 AI 매매 금지 프롬프트 + `format_krw_price()` 전체 적용 |
| #17 | feat(ai): 포트폴리오 슬롯 관리 + score/weight 기반 퀀트 고도화 + SELL 긴급 청산 |

---

## 9. 현재 오픈 이슈 / 다음 작업 후보

| 우선순위 | 항목 | 관련 브랜치 |
|---|---|---|
| 🔴 높음 | **PR #35 리뷰·병합** — 백테스터를 `dev`에 통합 | `backtest` → `dev` |
| 🟡 보통 | 스나이퍼 v2 전략 재실행 후 승률 재검증 | `backtest` |
| 🟡 보통 | AI 매매 성과 리포트 (실전 이력 집계 → Discord DM) | 신규 브랜치 필요 |
| 🟢 낮음 | `feat`, `feat-new` 브랜치 정리(삭제) | — |

---

## 10. 백테스팅 파이프라인 (`backtest` 브랜치)

`scripts/backtester.py` — OpenAI / Anthropic / Gemini 3종 LLM 성능 비교

### 모델 구성

| 어댑터 | 모델 ID | JSON 강제 방식 |
|---|---|---|
| `OpenAIAdapter` | `gpt-5.4` | `response_format={"type": "json_object"}` |
| `AnthropicAdapter` | `claude-sonnet-4-6` | 프롬프트 JSON 지시 + fallback 파서 |
| `GeminiAdapter` | `gemini-3.1-pro-preview` | `GenerateContentConfig(response_mime_type="application/json")` |

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

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `--model` | `openai` | 사용 모델 (openai / anthropic / gemini / **all**) |
| `--top` | `30` | 분석 대상 상위 코인 수 (거래대금 기준) |
| `--candles` | `200` | 지표 계산용 과거 4h 봉 수 |
| `--future-candles` | `30` | 시뮬레이션용 미래 4h 봉 수 (120시간 = 5일) |
| `--step` | `6` | AI 분석 사이클 간격 (4h 봉 수, 기본 6 = 24시간) |
| `--budget` | `1_000_000` | 가상 시드 (KRW) |

### 출력

- **콘솔**: 모델별 총 매매·승률·평균PnL·가상 잔고 ROI·AI 토큰 비용
- **`.result/backtest_results_YYYYMMDD_HHMMSS.csv`**: 실행마다 신규 파일 생성

### CSV 컬럼

`Timestamp / Model / Symbol / Score / Weight_Pct / Entry_Price / Target_Profit_Pct / Stop_Loss_Pct / Reason / Sim_Result / Sim_PnL_Pct / Candles_Held / Invested_KRW / PnL_KRW / Balance_KRW / Input_Tokens / Output_Tokens / Estimated_Cost_USD`

---

## 11. 백테스트 실패 분석 & 전략 개선 이력

### 🔴 1차 실패 (승률 20%대) — 원인 분석

| 실패 원인 | 내용 |
|---|---|
| **손절폭 과소 설정** | stop_loss_pct 2.0~4.5%로 너무 좁아 일반적인 가격 변동(휩쏘)에도 즉시 손절 발동 |
| **BTC 하락장 무시** | BTC가 하락/횡보 국면임에도 알트코인에 무차별 진입 → 시장 흐름 역행 |
| **진입 문턱 낮음** | score 80 기준이 너무 낮아 확신 없는 픽도 다수 포함 |
| **과도한 투입 비중** | weight_pct 50~60%로 단일 픽에 과도한 자금 집중 |
| **ERROR 결과 오염** | 신규 상장 코인 등 미래 데이터 부족 시 PnL 0.0% 기록으로 통계 왜곡 |

### ✅ 스나이퍼 v2 전략 적용 내용 (2026-03-17)

| 항목 | 변경 전 | 변경 후 |
|---|---|---|
| Score 임계값 | 80 (parse_picks 하드 차단) | **90** (극도 보수적 진입) |
| stop_loss_pct 최솟값 | 3.5% | **7.0%** (ATR × 2~3배) |
| weight_pct 상한 | 없음 | **30%** (투입 비중 제한) |
| BTC 필터 — 강제 관망 | 프롬프트 룰만 존재 | **유저 프롬프트에 ⛔ 태그 명시** → AI가 즉시 인지 |
| BTC 필터 — 극도 주의 | 없음 | **유저 프롬프트에 ⚠️ 태그 명시** → score 95+ 강제 |
| JSON 예시 앵커링 | stop 4.5, target 6.0, weight 55 | **stop 7.5, target 12.0, weight 25** |
| ERROR 결과 처리 | CSV에 PnL 0.0으로 기록 (통계 오염) | **SKIP 처리 — CSV 제외, 잔고 변동 없음** |
| 미래 봉 검증 | 없음 | **MIN_FUTURE_CANDLES=5 미달 시 조기 경고 + SKIP** |
| 비정상 봉 필터 | 없음 | **high<0, low<0, high<low 봉 자동 필터링** |
