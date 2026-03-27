/**
 * Centralized WebSocket connection manager.
 *
 * Single connection to the backend WebSocket endpoint.
 * Routes incoming messages to registered listeners by type.
 * Handles reconnection with exponential backoff (2s → 4s → 8s → ... → 30s max).
 */

import { WS_URL } from '../config';
const RECONNECT_BASE_MS = 2000;
const RECONNECT_MAX_MS = 30000;

export type MessageType =
  | 'price_update'
  | 'bar_update'
  | 'backfill'
  | 'level_update'
  | 'prediction'
  | 'connection_status'
  | 'session_stats'
  | 'trade_opened'
  | 'trade_closed'
  | 'account_update'
  | 'observation_started'
  | 'outcome_resolved';

type MessageHandler = (data: unknown) => void;

export type WsStatusInfo = {
  status: 'connected' | 'disconnected' | 'connecting';
  reconnectAttempt: number;
};

class WebSocketManager {
  private ws: WebSocket | null = null;
  private listeners = new Map<MessageType, Set<MessageHandler>>();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private _reconnectAttempt = 0;
  private destroyed = false;

  /** Status change callback — used by the store */
  onStatusChange: ((info: WsStatusInfo) => void) | null = null;

  get reconnectAttempt(): number {
    return this._reconnectAttempt;
  }

  connect(): void {
    if (this.destroyed) return;
    if (this.ws && this.ws.readyState === WebSocket.OPEN) return;

    // Close stale connection — neutralize onclose first to prevent
    // it from corrupting state (e.g. React StrictMode double-mount
    // closes ws1 while ws2 is being created; ws1.onclose must not
    // set this.ws = null or schedule a spurious reconnect).
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.onerror = null;
      this.ws.close();
      this.ws = null;
    }

    this.onStatusChange?.({ status: 'connecting', reconnectAttempt: this._reconnectAttempt });

    const ws = new WebSocket(WS_URL);

    ws.onopen = () => {
      if (this._reconnectAttempt > 0) {
        console.log(`[WS] Reconnected after ${this._reconnectAttempt} attempt(s)`);
      }
      this._reconnectAttempt = 0;
      this.onStatusChange?.({ status: 'connected', reconnectAttempt: 0 });
    };

    ws.onmessage = (event: MessageEvent) => {
      try {
        const msg = JSON.parse(event.data);
        const type = msg.type as MessageType;
        const handlers = this.listeners.get(type);
        if (handlers) {
          for (const handler of handlers) {
            handler(msg.data);
          }
        }
      } catch {
        // Ignore non-JSON or malformed messages
      }
    };

    ws.onclose = () => {
      this.ws = null;
      this.onStatusChange?.({ status: 'disconnected', reconnectAttempt: this._reconnectAttempt });
      if (!this.destroyed) {
        this.scheduleReconnect();
      }
    };

    ws.onerror = () => {
      // onclose will fire after onerror — reconnect handled there
    };

    this.ws = ws;
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    const delay = Math.min(
      RECONNECT_BASE_MS * Math.pow(2, this._reconnectAttempt),
      RECONNECT_MAX_MS,
    );
    this._reconnectAttempt++;
    this.onStatusChange?.({ status: 'connecting', reconnectAttempt: this._reconnectAttempt });
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  on(type: MessageType, handler: MessageHandler): () => void {
    let handlers = this.listeners.get(type);
    if (!handlers) {
      handlers = new Set();
      this.listeners.set(type, handlers);
    }
    handlers.add(handler);

    return () => {
      handlers!.delete(handler);
      if (handlers!.size === 0) {
        this.listeners.delete(type);
      }
    };
  }

  send(message: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
    }
  }

  destroy(): void {
    this.destroyed = true;
    this.onStatusChange = null;
    this.listeners.clear();
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}

/** Singleton instance — shared across the entire app. */
export const wsManager = new WebSocketManager();
