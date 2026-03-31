"""
WebSocket manager — real-time data push with throttled price updates.

Manages connected WebSocket clients, broadcasts messages, and enforces
the 1 price_update per second throttle. Non-price events (predictions,
trades, observations, alerts) bypass throttling.

Backfill assembly: when a client connects, builds an atomic snapshot
of all current state from the DashboardState.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

from starlette.websockets import WebSocket, WebSocketState

from alpha_lab.dashboard.api.level_serialization import serialize_zones

if TYPE_CHECKING:
    from alpha_lab.dashboard.api.server import DashboardState

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages WebSocket clients with throttled price broadcasting.

    Price updates are throttled to at most 1 per second via a background
    asyncio task. All other message types are broadcast immediately.
    """

    def __init__(self, throttle_interval: float = 1.0) -> None:
        self._clients: list[WebSocket] = []
        self._subscriptions: dict[WebSocket, set[str]] = {}
        self._pending_price: dict | None = None
        self._last_price_push: float = 0.0
        self._throttle_interval = throttle_interval
        self._broadcast_task: asyncio.Task | None = None

    async def connect(self, ws: WebSocket, state: DashboardState) -> None:
        """Accept a WebSocket connection and send backfill."""
        await ws.accept()
        self._clients.append(ws)
        self._subscriptions[ws] = {"987t"}
        backfill = self.assemble_backfill(state)
        await ws.send_json(backfill)
        logger.info("WebSocket client connected (%d total)", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        """Remove a client from the active list."""
        if ws in self._clients:
            self._clients.remove(ws)
        self._subscriptions.pop(ws, None)
        logger.info("WebSocket client disconnected (%d remaining)", len(self._clients))

    async def broadcast(self, message: dict) -> None:
        """Send a message to all connected clients immediately."""
        disconnected: list[WebSocket] = []
        for client in self._clients:
            try:
                if client.client_state == WebSocketState.CONNECTED:
                    await client.send_json(message)
            except Exception:
                disconnected.append(client)
        for client in disconnected:
            self.disconnect(client)

    def update_price(
        self,
        price: float,
        bid: float | None,
        ask: float | None,
        timestamp: str,
    ) -> None:
        """Store latest price for next throttled push.

        Called on every trade tick (10K+/min). Only stores the latest
        values — the background task broadcasts at most 1/sec.
        """
        self._pending_price = {
            "type": "price_update",
            "data": {
                "price": price,
                "bid": bid,
                "ask": ask,
                "timestamp": timestamp,
            },
        }

    def subscribe_timeframe(self, ws: WebSocket, timeframe: str) -> None:
        """Set the timeframe subscription for a client."""
        if ws in self._subscriptions:
            self._subscriptions[ws] = {timeframe}

    async def broadcast_bar(self, timeframe: str, bar_data: dict) -> None:
        """Send a bar_update to clients subscribed to this timeframe."""
        message = {"type": "bar_update", "data": {"timeframe": timeframe, "bar": bar_data}}
        disconnected: list[WebSocket] = []
        for client in self._clients:
            if timeframe not in self._subscriptions.get(client, set()):
                continue
            try:
                if client.client_state == WebSocketState.CONNECTED:
                    await client.send_json(message)
            except Exception:
                disconnected.append(client)
        for client in disconnected:
            self.disconnect(client)

    async def flush_price(self) -> None:
        """Push pending price if throttle interval has elapsed.

        Called by the background broadcast loop every throttle_interval seconds.
        """
        now = time.monotonic()
        if self._pending_price and (now - self._last_price_push) >= self._throttle_interval:
            await self.broadcast(self._pending_price)
            self._pending_price = None
            self._last_price_push = now

    async def start_broadcast_loop(self) -> None:
        """Background task: flush pending price updates every interval."""
        try:
            while True:
                await asyncio.sleep(self._throttle_interval)
                await self.flush_price()
        except asyncio.CancelledError:
            pass

    def start(self) -> None:
        """Start the background broadcast loop."""
        if self._broadcast_task is None:
            self._broadcast_task = asyncio.create_task(self.start_broadcast_loop())

    async def stop(self) -> None:
        """Stop the background broadcast loop."""
        if self._broadcast_task is not None:
            self._broadcast_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._broadcast_task
            self._broadcast_task = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def assemble_backfill(self, state: DashboardState) -> dict:
        """Build an atomic snapshot of all current state for a new client."""
        # Levels (include touched/disabled state; same semantics as live updates/REST)
        active_levels = []
        if state.level_engine is not None:
            active_levels = serialize_zones(
                state.level_engine.all_zones,
                state.disabled_level_types,
            )

        # Active observation
        active_obs = None
        if state.observation_manager is not None:
            obs = state.observation_manager.active_observation
            if obs is not None:
                active_obs = {
                    "event_id": obs.event.event_id,
                    "direction": obs.event.trade_direction.value,
                    "level_price": float(obs.event.level_zone.representative_price),
                    "start_time": obs.start_time.isoformat(),
                    "end_time": obs.end_time.isoformat(),
                    "status": obs.status.value,
                    "trades_accumulated": len(obs.trades_accumulated),
                }

        # Open positions (include TP/SL prices for chart overlays)
        open_positions = []
        for acct in state.account_manager.get_all_accounts():
            if acct.has_position:
                pos = acct.current_position
                tp_points = state.position_monitor.get_group_tp(acct.group)
                sl_points = state.position_monitor.get_group_sl(acct.group)
                entry = pos.entry_price
                if pos.direction.value == "long":
                    tp_price = float(entry + tp_points)
                    sl_price = float(entry - sl_points)
                else:
                    tp_price = float(entry - tp_points)
                    sl_price = float(entry + sl_points)
                open_positions.append({
                    "account_id": pos.account_id,
                    "direction": pos.direction.value,
                    "entry_price": float(pos.entry_price),
                    "contracts": pos.contracts,
                    "entry_time": pos.entry_time.isoformat(),
                    "unrealized_pnl": float(pos.unrealized_pnl),
                    "tp_price": tp_price,
                    "sl_price": sl_price,
                })

        # Account states
        accounts = []
        for acct in state.account_manager.get_all_accounts():
            accounts.append({
                "account_id": acct.account_id,
                "label": acct.label,
                "group": acct.group,
                "balance": float(acct.balance),
                "status": acct.status.value,
                "tier": acct.tier,
                "has_position": acct.has_position,
                "daily_pnl": float(acct.daily_pnl),
            })

        # Config
        monitor = state.position_monitor
        config = {
            "group_a_tp": float(monitor.get_group_tp("A")),
            "group_b_tp": float(monitor.get_group_tp("B")),
            "group_a_sl": float(monitor.get_group_sl("A")),
            "group_b_sl": float(monitor.get_group_sl("B")),
            "second_signal_mode": state.trade_executor.second_signal_mode,
            "overlays": dict(state.overlay_config),
        }

        # Session stats — derive from trade outcomes, not prediction accuracy
        trades = list(state.todays_trades)
        closed_signals: dict[str, str] = {}
        for t in trades:
            key = t.get("entry_time", "")
            if key not in closed_signals:
                closed_signals[key] = t.get("exit_reason", "")
        wins = sum(1 for r in closed_signals.values() if r == "tp")
        losses = sum(1 for r in closed_signals.values() if r in ("sl", "blown"))
        decided = wins + losses

        return {
            "type": "backfill",
            "data": {
                "connection_status": state.connection_status,
                "latest_price": state.latest_price,
                "latest_bid": state.latest_bid,
                "latest_ask": state.latest_ask,
                "active_levels": active_levels,
                "active_observation": active_obs,
                "last_prediction": state.last_prediction,
                "open_positions": open_positions,
                "todays_trades": trades,
                "todays_predictions": list(state.todays_predictions),
                "session_stats": {
                    "signals_fired": len(closed_signals),
                    "wins": wins,
                    "losses": losses,
                    "accuracy": round(wins / decided, 4) if decided > 0 else 0,
                    "total_trades": len(closed_signals),
                    "total_pnl": sum(float(t.get("pnl", 0)) for t in trades),
                },
                "accounts": accounts,
                "config": config,
                "replay_mode": getattr(state, "replay_mode", False),
            },
        }
