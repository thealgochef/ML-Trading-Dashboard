# Trade Dashboard

Live NQ futures trading dashboard with TradingView Advanced Charts.

## Structure

- `backend/` — FastAPI + WebSocket server, pipeline, engine, ML model, trading
- `frontend/` — React + TypeScript + TradingView Advanced Charts (TBD)
- `data/` — Symlink to Claude-Quant-Lab/data (Parquet files, models)

## Backend

```bash
cd backend
pip install -e ".[dev]"
python -m alpha_lab.dashboard.api
```
