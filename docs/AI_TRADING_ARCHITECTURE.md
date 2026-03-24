# AI 트레이딩 아키텍처

> CoinCome 프로젝트에서 제공하는 AI 기반 매매 기능의 전체 구조와 각 모드별 동작 방식을 설명합니다.

---

## 1. 트레이딩 모드 비교

| 항목 | 실전 트레이딩 (`/ai실전`) | 모의 트레이딩 (`/ai모의`) | 백테스터 (`backtester.py`) |
|---|---|---|---|
| **목적** | 실제 자금으로 매매 자동화 | 실시간 시장 데이터로 가상 매매 연습 | 과거 데이터 기반 전략 성능 검증 |
| **자금** | 사용자 실제 KRW 잔고 | 가상 잔고 (설정 금액) | 가상 시드 (기본 1,000,000 KRW) |
| **주문 실행** | Upbit API 실제 주문 | 없음 (시세 기록만) | 없음 (OHLCV 시뮬레이션) |
| **AI 호출** | 매 폴링 사이클 (0.5초) | 매 폴링 사이클 (0.5초) | 사용자 정의 Step 간격 (기본 24h) |
| **지표 수집** | 실시간 API (`fetch_ticker`) + MarketDataManager 1h 주기 캐시 | 동일 | 사전 수집 OHLCV 슬라이싱 |
| **결과 저장** | DB (`trades`, `bot_settings`) | DB (`paper_trades`) | CSV (`.result/backtest_results_*.csv`) |
| **구독 필요** | VIP | FREE 이상 | 불필요 (스크립트 직접 실행) |
| **지원 AI 모델** | Claude (Anthropic) | Claude (Anthropic) | OpenAI / Anthropic / Gemini |
| **진입 파일** | `app/services/trading_worker.py` | `app/services/trading_worker.py` | `scripts/backtester.py` |

---

## 2. 실전 / 모의 트레이딩 아키텍처

### 2-1. 컴포넌트 관계

```
Discord Bot (discord.py)
  ├─ /ai실전 커맨드 (bot/cogs/ai_trading.py)
  │    └─ 엔진 선택: SWING / SCALPING / MAJOR / ALL
  ├─ /ai모의 커맨드 (bot/cogs/paper_trading.py)
  │    └─ 엔진 선택: SWING / SCALPING / MAJOR / ALL (ai_engine_mode)
  └─ AIManagerTask (bot/tasks/ai_manager.py) — 매시 정각 스케줄
       ├─ MarketDataManager (market_data.py)
       │    ├─ 1시간 주기: Top10 KRW 코인 4h+1h+15m 지표 캐시
       │    └─ on-demand fetch: 보유 포지션 캐시 미스 시 즉시 fetch
       ├─ AITraderService (ai_trader.py)
       │    ├─ analyze_market(engine_type, weight_pct) — 신규 픽
       │    └─ review_positions(engine_type) — 기존 포지션 HOLD/UPDATE/SELL
       ├─ WorkerRegistry + TradingWorker (trading_worker.py)
       │    └─ 0.5초 폴링 · 익절/손절 자동 실행 · Discord DM 알림
       └─ ExchangeService (exchange.py) — 실전 매매 전용
```

### 2-2. AI 펀드 매니저 사이클 (매시 정각)

```
매시 정각 실행
  │
  ├─ [Step 0] 사용자 필터링
  │       VIP AND (ai_mode_enabled OR is_major_enabled): 실전 대상
  │       ai_paper_mode_enabled: 모의 대상
  │
  ├─ [Step 1·2] 기존 포지션 리뷰 (_review_existing_positions)
  │       _group_by_engine() → trade_style 별 분리 리뷰
  │       캐시 미스 심볼 → MarketDataManager.fetch_and_cache_symbol() on-demand
  │       SELL → force_sell() | 실패 시 유저 DM 알림
  │       UPDATE → target/stop_loss DB 갱신
  │
  ├─ [Step 3-a] SWING 엔진 (is_swing_hour=True)
  │       analyze_market("SWING") → score≥90 픽 → _buy_new_coins
  │
  ├─ [Step 3-b] SCALPING 엔진 (매 사이클)
  │       analyze_market("SCALPING") → score≥90 픽 → _buy_new_coins
  │
  ├─ [Step 3-c] MAJOR 엔진 (is_swing_hour=True AND is_major_on)
  │       3중 기계적 필터 (EMA200·정배열·BB상단돌파)
  │       통과 종목 → analyze_market("MAJOR_TREND")
  │       통과 없어도 DM에 관망 한 줄 표시
  │
  ├─ [Step 4] DM 리포트 전송
  │       엔진별 섹션 통합 임베드 (엔진 태그 정확 표시)
  │
  └─ 에러 처리
          잔고 조회 실패 → 신규 매수 스킵 + 유저 DM
          DB 삽입 실패(실전) → 고아 포지션 방지 알림 DM
          force_sell 실패 → 수동 확인 요청 DM
```

### 2-3. 매시간 AI 포지션 리뷰 채점 기준

| 기준 | 설명 |
|---|---|
| **① 진행률 기반 본절 이동** | 목표 대비 50% 이상 달성 시 손절가를 매수가(0%)로 이동 — 리스크 프리 상태 |
| **② 모멘텀 관성 평가** | RSI·MA 추세 유지 여부로 지속성 판단. 강한 모멘텀 시 목표 상향 가능 |
| **③ ATR 대비 손절폭 검증** | ATR 급증 시 손절 확대 또는 즉시 청산. 변동성 정상화 시 손절 복원 |

> 리뷰 액션: **HOLD** (유지) / **UPDATE** (목표·손절 조정) / **SELL** (즉시 청산)
> MAJOR 엔진: 4h 봉 최우선 평가, 1h 노이즈 과반응 금지

---

## 3. 백테스터 아키텍처

### 3-1. 설계 원칙

- **미래 데이터 누수 완전 차단**: 각 시뮬레이션 스텝에서 해당 시점 이전 봉만 슬라이싱
- **Live API 호출 최소화**: OHLCV 전량을 1회 일괄 수집 → 이후 모든 지표 계산은 순수 연산
- **다중 AI 모델 병렬 비교**: OpenAI / Anthropic / Gemini를 동일 데이터로 동시 평가
- **가상 시드 잔고 추적**: `weight_pct` 기반 투자금 배분 → 누적 손익 계산

### 3-2. 백테스트 흐름 (Mermaid)

```mermaid
flowchart TD
    A([스크립트 실행<br/>python backtester.py --model all]) --> B

    subgraph INIT["[1단계] 초기화"]
        B[API 키 검증 & 어댑터 생성<br/>OpenAI / Anthropic / Gemini] --> C
        C[Upbit 거래소 연결<br/>ccxt.upbit]
    end

    subgraph FETCH["[2단계] 데이터 일괄 수집 (1회)"]
        C --> D[상위 심볼 선정<br/>fetch_top_symbols<br/>KRW 마켓 24h 거래대금 Top N]
        D --> E[전체 OHLCV 일괄 수집<br/>fetch_ohlcv × N 심볼<br/>candles + future_candles 봉]
    end

    subgraph LOOP["[3단계] Time-Stepping 루프"]
        E --> F{스텝 인덱스 순회<br/>WARMUP → end, step=6봉}
        F --> G[슬라이싱 & 지표 계산<br/>ohlcv[:step_idx]<br/>compute_indicators_from_ohlcv]
        G --> H[유저 프롬프트 빌드<br/>build_user_prompt<br/>RSI·MA·ATR·거래대금·예산]
        H --> I[AI 픽 요청<br/>adapter.pick<br/>JSON 형식 응답]
        I --> J{픽 존재?}
        J -- 없음/관망 --> F
        J -- 있음 --> K[미래 봉 슬라이싱<br/>ohlcv[step_idx : step_idx+future]]
        K --> L[매매 시뮬레이션<br/>simulate_trade_from_data<br/>WIN / LOSS / TIMEOUT]
        L --> M[가상 잔고 업데이트<br/>invested = balance × weight_pct/100<br/>pnl = invested × sim_pnl/100<br/>balance += pnl]
        M --> N[CSV 행 기록<br/>Invested_KRW / PnL_KRW / Balance_KRW]
        N --> F
    end

    subgraph REPORT["[4단계] 결과 저장 & 리포트"]
        F -- 루프 종료 --> O[CSV 파일 저장<br/>.result/backtest_results_YYYYMMDD.csv]
        O --> P[콘솔 요약 출력<br/>승률 / 평균PnL / 최종 잔고 / ROI<br/>AI 토큰 사용량 & 비용]
    end
```

### 3-3. 핵심 함수 정리

| 함수 | 위치 | 역할 |
|---|---|---|
| `fetch_top_symbols()` | `backtester.py` | KRW 마켓 상위 N종목 선정 (거래대금 기준) |
| `compute_indicators_from_ohlcv()` | `backtester.py` | OHLCV 슬라이스 → RSI14·MA20·ATR%·거래대금 순수 계산 |
| `build_user_prompt()` | `backtester.py` | 시장 스냅샷 → AI 입력 텍스트 변환 |
| `parse_picks()` | `backtester.py` | AI JSON 응답 파싱 + 유효성 검증 (score ≥ 85, stop ≥ 3.5%) |
| `simulate_trade_from_data()` | `backtester.py` | 미래 봉 슬라이스 기반 WIN/LOSS/TIMEOUT 결정론적 판정 |
| `calc_cost()` | `backtester.py` | 모델·토큰 수 기반 USD 비용 추산 |
| `run_backtest()` | `backtester.py` | 전체 파이프라인 오케스트레이터 |

### 3-4. AI 어댑터 비교

| 어댑터 | 모델 | SDK | 비동기 방식 | JSON 강제 |
|---|---|---|---|---|
| `OpenAIAdapter` | `gpt-5.4` | `openai` | `AsyncOpenAI` 네이티브 | `response_format={"type":"json_object"}` |
| `AnthropicAdapter` | `claude-sonnet-4-6` | `anthropic` | `AsyncAnthropic` 네이티브 | 시스템 프롬프트 지시 |
| `GeminiAdapter` | `gemini-3.1-pro-preview` | `google-genai` | `client.aio.models` 네이티브 | `GenerateContentConfig(response_mime_type="application/json")` |

---

## 4. 엔진별 전략 요약

### 4-0. V2 엔진 매트릭스

| 엔진 | 대상 | 타임프레임 | 실행 주기 | 손절 상한 |
|---|---|---|---|---|
| **SWING** | 알트코인 전체 (메이저 제외) | 4h 봉 우선 | 6회/일 (스윙 시간대) | 동적 ATR 기반 |
| **SCALPING** | 알트코인 전체 (메이저 제외) | 1h 봉 우선 | 24회/일 | ≤ 2.0% 하드 상한 |
| **MAJOR** | BTC/ETH/XRP/BNB/SOL/DOGE/ADA/SUI | 4h 봉 최우선 | 6회/일 (스윙 시간대) | 동적 ATR 기반 |
| **ALL** | 알트+메이저 동시 | 엔진별 분리 | 각 엔진 일정대로 | 엔진별 |

### 4-1. MAJOR 3중 기계적 필터

```
1. Close > EMA200   — 장기 추세 상방
2. EMA20 > EMA50    — 단·중기 정배열
3. Close > BB상단   — 볼린저밴드(20, 2.0σ) 상단 돌파
```

조건 3개 모두 통과 시에만 AI 분석 진행. 통과 없으면 "전체 관망" DM 표시.

## 5. 매매 전략 (고승률 스나이퍼)

### 4-1. 시스템 프롬프트 핵심 룰

```
[손절폭 — 휩쏘 방어]
  stop_loss_pct 최솟값 : 3.5%
  고변동성(ATR% 3~5%) : 5.0~7.0%
  → parse_picks() 레벨에서 3.5% 미만이면 강제 보정

[BTC 하락장 관망]
  BTC RSI14 < 45 : 알트코인 픽 극도 자제
  BTC RSI14 < 40 : picks 배열 강제 비움

[과매수 타점 회피]
  대상 RSI14 > 70 : 진입 패스
  대상 RSI14 60~70 : score ≥ 87 필수

[진입 조건]
  score ≥ 85 / RSI14 35~65 / MA20 지지·돌파 / 24h 대금 ≥ 50억 KRW

[리스크-리워드]
  target_profit_pct ≥ stop_loss_pct × 1.5
```

### 4-2. 가상 시드 잔고 계산 방식

```
초기 시드 (budget)  : 1,000,000 KRW  (--budget 인자, 기본값)

각 픽마다:
  invested_krw = current_balance × weight_pct / 100
  pnl_krw      = invested_krw × sim_pnl_pct / 100
  balance      = balance + pnl_krw

CSV 컬럼:
  Invested_KRW  — 해당 픽에 투자한 금액
  PnL_KRW       — 시뮬레이션 손익금
  Balance_KRW   — 픽 처리 후 누적 잔고
```

---

## 6. CLI 옵션 (backtester.py)

```bash
python scripts/backtester.py \
  --model     [openai|anthropic|gemini|all]   # 사용할 AI 모델 (기본: openai)
  --top       30                               # 분석 상위 코인 수 (거래대금 기준)
  --candles   200                              # 과거 4h 봉 수 (분석 기간 ≈ 33일)
  --future-candles 20                          # 시뮬레이션용 미래 봉 수 (≈ 3.3일)
  --step      6                                # 분석 주기 (4h 봉 수, 기본 6 = 24시간)
  --budget    1000000                          # 가상 시드 KRW (기본 1,000,000)
  --api-key   ""                               # API 키 (미지정 시 .env 환경변수 사용)
```

---

## 7. 환경변수 (.env)

```
# AI 모델 API 키
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=AIza...

# 거래소 (실전/모의 트레이딩 전용)
UPBIT_ACCESS_KEY=...
UPBIT_SECRET_KEY=...

# DB (실전/모의 트레이딩 전용)
DATABASE_URL=postgresql+asyncpg://...
```

---

---

## 8. MarketDataManager 캐시 전략

| 모드 | 설명 |
|---|---|
| **정기 캐시 (1h 주기)** | Top10 KRW 거래대금 코인 → 4h+1h+15m OHLCV 일괄 fetch |
| **on-demand fetch** | 보유 포지션 심볼이 캐시에 없을 때 (MAJOR 코인 등) 즉시 단일 fetch |
| **캐시 병합** | on-demand 결과는 기존 Top10 캐시에 merge (덮어쓰기 X) |

> `fetch_and_cache_symbol(symbol)`: 단일 심볼 4h+1h+15m 지표 즉시 fetch 후 `self._cache[symbol]` 갱신
> `_review_existing_positions()` 진입 시 자동으로 캐시 미스 탐지 → on-demand fetch 실행

---

*최종 수정: 2026-03-25*
