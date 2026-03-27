/**
 * React hook that connects WebSocketManager ↔ Zustand store.
 *
 * Call once at the app root. Subscribes to all message types and
 * dispatches to the appropriate store actions.
 */

import { useEffect } from 'react';
import { wsManager } from './WebSocketManager';
import { useDashboardStore } from '../store/dashboardStore';
import type {
  TradeOpenedPayload,
  TradeClosedPayload,
  AccountUpdatePayload,
  OutcomeResolvedPayload,
} from '../store/dashboardStore';
import type {
  BackfillData,
  LevelZone,
  Prediction,
  Observation,
  SessionStats,
} from '../types';

export function useWebSocket(): void {
  useEffect(() => {
    // WS connection status → store
    wsManager.onStatusChange = (info) => {
      useDashboardStore.getState().setWsStatus(info.status, info.reconnectAttempt);
    };

    // Subscribe to all message types
    const unsubs = [
      wsManager.on('backfill', (data) => {
        useDashboardStore.getState().applyBackfill(data as BackfillData);
      }),
      wsManager.on('price_update', (data) => {
        useDashboardStore.getState().updatePrice(
          data as { price: number; bid?: number | null; ask?: number | null; timestamp: string },
        );
      }),
      wsManager.on('level_update', (data) => {
        const msg = data as { action: string; levels: LevelZone[] };
        useDashboardStore.getState().updateLevels(msg.levels);
      }),
      wsManager.on('prediction', (data) => {
        useDashboardStore.getState().addPrediction(data as Prediction);
      }),
      wsManager.on('observation_started', (data) => {
        useDashboardStore.getState().setObservation(data as Observation);
      }),
      wsManager.on('session_stats', (data) => {
        useDashboardStore.getState().updateSessionStats(data as SessionStats);
      }),
      wsManager.on('connection_status', (data) => {
        const msg = data as { status: string };
        useDashboardStore.getState().setDataStatus(msg.status);
      }),
      wsManager.on('trade_opened', (data) => {
        useDashboardStore.getState().openPosition(data as TradeOpenedPayload);
      }),
      wsManager.on('trade_closed', (data) => {
        useDashboardStore.getState().closePosition(data as TradeClosedPayload);
      }),
      wsManager.on('account_update', (data) => {
        useDashboardStore.getState().updateAccount(data as AccountUpdatePayload);
      }),
      wsManager.on('outcome_resolved', (data) => {
        useDashboardStore.getState().resolveOutcome(data as OutcomeResolvedPayload);
      }),
    ];

    // Connect
    wsManager.connect();

    return () => {
      unsubs.forEach((unsub) => unsub());
      wsManager.onStatusChange = null;
    };
  }, []);
}
