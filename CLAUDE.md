# Trade Dashboard — CLAUDE.md

## Project Overview

Live NQ futures trading dashboard: FastAPI backend (data pipeline, ML model, trade execution) + React/TypeScript frontend (TradingView charts, WebSocket-driven UI).

## Project Structure

```
Trade-Dashboard/
├── backend/          # FastAPI + WebSocket server, pipeline, engine, ML model, trading
│   ├── src/alpha_lab/dashboard/
│   │   ├── api/      # FastAPI app, routes, WebSocket, schemas
│   │   ├── config/   # Pydantic settings (DASHBOARD_ env prefix, .env)
│   │   ├── db/       # Database (PostgreSQL + asyncpg, Alembic migrations)
│   │   ├── engine/   # LevelEngine, TouchDetector, ObservationManager, FeatureComputer
│   │   ├── model/    # ModelManager, PredictionEngine, OutcomeTracker (CatBoost)
│   │   ├── pipeline/ # PipelineService, DatabentoClient, RithmicClient, TickBarBuilder
│   │   └── trading/  # AccountManager, TradeExecutor, PositionMonitor
│   ├── tests/
│   ├── .env          # Secrets (DASHBOARD_DATABENTO_API_KEY, etc.) — DO NOT commit
│   └── pyproject.toml
├── frontend/         # React 19 + Vite + TypeScript + Tailwind v4
│   ├── src/
│   │   ├── components/    # UI panels (chart, accounts, models, trading)
│   │   ├── config.ts      # Centralized API_BASE + WS_URL (env-driven)
│   │   ├── datafeed/      # TradingView datafeed adapter
│   │   ├── store/         # Zustand store (dashboardStore)
│   │   ├── websocket/     # WebSocketManager + useWebSocket hook
│   │   └── types.ts       # Shared TypeScript types
│   ├── .env               # Dev defaults (localhost URLs)
│   ├── .env.production    # Prod defaults (relative paths, auto-detect)
│   └── package.json
└── data/             # Parquet files, CatBoost models (.cbm), tick recordings
```

## Startup

### Python version

The backend MUST be started with **Python 3.13** — the correct executable is:
```
C:/Users/gonza/AppData/Local/Programs/Python/Python313/python.exe
```
Do NOT use the default `python` command (resolves to 3.11, missing dependencies).

### Backend (port 8000)

```bash
cd backend
"C:/Users/gonza/AppData/Local/Programs/Python/Python313/python.exe" -m alpha_lab.dashboard.api
```

- Runs FastAPI + Uvicorn on **http://localhost:8000**
- WebSocket endpoint: **ws://localhost:8000/ws**
- Loads settings from `backend/.env` (env prefix: `DASHBOARD_`)
- Auto-loads CatBoost model from `data/models/`
- Creates 5 default paper trading accounts on startup

### Frontend (port 5173)

```bash
cd frontend
npm run dev
```

- Runs Vite dev server on **http://localhost:5173**
- API base and WS URL configured via `frontend/.env` (VITE_API_BASE, VITE_WS_URL)
- Production build uses relative paths (auto-detects protocol/host)

### Start order

1. Backend first (frontend expects WebSocket + REST on port 8000)
2. Frontend second

## Ports

| Service    | Port | Protocol  |
|------------|------|-----------|
| Backend    | 8000 | HTTP + WS |
| Frontend   | 5173 | HTTP (Vite) |
| PostgreSQL | 5432 | TCP       |

## Backend Configuration

All env vars use `DASHBOARD_` prefix. Key settings in `backend/.env`:

- `DASHBOARD_DATA_SOURCE` — `"databento"` or `"rithmic"` (default: databento)
- `DASHBOARD_DATABENTO_API_KEY` — Databento API key
- `DASHBOARD_DATABASE_URL` — PostgreSQL connection string
- `DASHBOARD_SYMBOL` — Instrument symbol (default: NQ)
- `DASHBOARD_MODEL_DIR` — Path to CatBoost .cbm model files (default: data/models)

## Key Commands

```bash
# Backend
cd backend
"C:/Users/gonza/AppData/Local/Programs/Python/Python313/python.exe" -m pytest          # run tests
"C:/Users/gonza/AppData/Local/Programs/Python/Python313/python.exe" -m ruff check src  # lint

# Frontend
cd frontend
npm run dev       # dev server
npm run build     # production build (tsc + vite build)
npm run lint      # eslint
```

## Architecture Notes

- **Signal-to-trade pipeline**: Trade tick → TouchDetector → ObservationManager → PredictionEngine → TradeExecutor + OutcomeTracker
- **WebSocket broadcasts**: All state updates (prices, predictions, trades, levels) are pushed to connected clients via a single `/ws` endpoint
- **Thread safety**: Databento callbacks run on a background thread; `call_soon_threadsafe` bridges to the async event loop for WS broadcasts
- **State management**: Backend uses `DashboardState` dataclass on `app.state.dashboard`; frontend uses Zustand (`dashboardStore`)

## Critical Rules

### 1. Never force-kill the backend

**Never** use `taskkill //F`, `kill -9`, or any forced termination on the backend process. The Databento live stream holds an authenticated TCP connection with subscription state. Force-killing leaves a zombie connection on Databento's side — the next start authenticates successfully but subscription acknowledgements never arrive, so zero ticks flow. The dashboard will show "Data: connected" while receiving nothing.

### 2. Frontend changes don't require a backend restart

Vite hot-reloads TypeScript/React changes instantly. After editing any file under `frontend/src/`, the browser reflects changes within seconds. **Never restart the backend** in response to a frontend-only change — it wastes time, risks the Databento connection issue above, and is completely unnecessary.

### 3. Do NOT restart the backend yourself

When a backend change requires a restart, tell the user:
- Which file(s) you changed
- Why a restart is needed
- Then STOP. Do not run any kill or start commands.

The user will handle the restart. Do NOT use taskkill, kill, pkill, or any process management commands on the Python backend. Ever. For any reason.

### 4. Never modify backend code without being asked

Do **not** touch any file under `backend/` unless the user explicitly requests a backend change. This includes "helpful" fixes, refactors, or additions. The backend is a production trading system — unauthorized changes risk breaking the live pipeline, Databento connection, or trade execution.


## Lessons Learned

### Trading date uses CME rollover, not UTC midnight
The CME trading day rolls at 6:00 PM ET. If current time is after 6 PM ET, trading_date is tomorrow. The `_on_backfill_complete()` in server.py uses ET-based calculation, NOT `datetime.now(UTC).date()`. Do not change this.

### PDH/PDL use prior day RTH only
PDH/PDL are computed from the prior day's NY RTH session (9:30 AM - 4:15 PM ET), NOT the full 24-hour session. This matches the CatBoost model's training data. Do not change this without retraining the model.

### All 6 levels must always be present
PDH, PDL, Asia High, Asia Low, London High, London Low. Levels recompute on session transitions (asia→london, london→pre_market, pre_market→ny_rth) via `TouchDetector.on_session_change()` callbacks. If levels are missing, check the session change callback wiring.

### Touched zones stay visible on the chart
`_broadcast_levels()` uses `level_engine.all_zones` (including touched). Touched zones render as dashed lines, active zones as solid. Never filter touched zones from the WebSocket broadcast.

### Chart markers reconstruct from backfill on refresh
`TradingChart.tsx` rebuilds prediction markers, entry markers, and exit markers from the store's `todaysPredictions`, `todaysTrades`, and `openPositions` after backfill. ChartManager's dedup Sets prevent duplicates.

### Session detection uses tick timestamp, not wall clock
`TouchDetector._classify_session()` takes `ts_utc` as a parameter and uses `trade.timestamp`. This is critical for replay mode — never change it to use `datetime.now()`.

### Chart proportional zoom
Both axes scale proportionally (barSpacing and pixels-per-point change together). Zoom is clamped: MIN_BAR_SPACING=6, MAX_BAR_SPACING=9, DEFAULT=8. Do not change these without asking.

### Apex 50K Intraday PA Rules (verified March 2026)
**Trailing DD**: $2,000, caps at $50,100 liquidation. Starting balance $50K, initial liquidation $48K. Liquidation trails peak up to $50,100 (when peak reaches $52,100), then locks permanently. Constants: `TRAILING_DD=2000`, `SAFETY_NET_PEAK=52100`, `SAFETY_NET_LIQUIDATION=50100`.

**DLL (Daily Loss Limit)**: Does NOT blow the account — sets `DLL_LOCKED`, closes open position, stops trading for the day, resets to `ACTIVE` next day. Per-tier: T1=$1K, T2=$1K, T3=$2K, T4=$3K.

**Tiers**: Based on profit above $50K starting balance. T1: $0–$1,499 (2 contracts), T2: $1,500–$2,999 (3 contracts), T3: $3,000–$5,999 (4 contracts), T4: $6,000+ (4 contracts).

**Costs**: Eval=$20 (promo), Activation=$79, Reset=$99 ($20+$79). Payout split=100%.

**Payouts**: Min balance $52,600 to request. Graduated caps: $1,500/$2,000/$2,500/$2,500/$3,000/$3,000 (6 max). Min withdrawal $500. 5 qualifying days required (min $250/day profit). 50% consistency rule (best day ≤ 50% of total).

**Config**: `EconomicConfig` in `economic_config.py` holds these defaults. `ApexAccount` uses constants from `trading/__init__.py`. Keep both in sync.