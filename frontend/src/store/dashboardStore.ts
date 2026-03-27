/**
 * Zustand store — single source of truth for all dashboard state.
 *
 * Initialized from the `backfill` message on WebSocket connect.
 * Updated incrementally by each subsequent message type.
 */

import { create } from 'zustand';
import type {
  ConnectionStatus,
  LevelZone,
  Prediction,
  Observation,
  OpenPosition,
  ClosedTrade,
  SessionStats,
  Account,
  BackfillData,
} from '../types';

// ── Payload types for WS messages that differ from store types ────

export interface TradeOpenedPayload {
  account_id: string;
  direction: 'long' | 'short';
  entry_price: number;
  entry_time: string;
  contracts: number;
  group: string;
  tp_price: number;
  sl_price: number;
}

export interface TradeClosedPayload {
  account_id: string;
  direction: 'long' | 'short';
  entry_price: number;
  exit_price: number;
  entry_time: string;
  exit_time: string;
  exit_reason: string;
  pnl: number;
  pnl_points: number;
  contracts: number;
  group: string;
}

export interface AccountUpdatePayload {
  account_id: string;
  balance: number;
  profit: number;
  daily_pnl: number;
  group: string;
  status: string;
}

export interface OutcomeResolvedPayload {
  event_id: string;
  predicted_class: string;
  actual_class: string;
  prediction_correct: boolean;
  mfe_points: number;
  mae_points: number;
  resolution_type: string;
}

interface DashboardState {
  // Connection
  wsStatus: ConnectionStatus;
  wsReconnectAttempt: number;
  dataStatus: ConnectionStatus;

  // Price
  latestPrice: number | null;
  latestBid: number | null;
  latestAsk: number | null;

  // Levels
  levels: LevelZone[];

  // Predictions
  lastPrediction: Prediction | null;
  todaysPredictions: Prediction[];

  // Observation
  activeObservation: Observation | null;

  // Trading
  openPositions: OpenPosition[];
  todaysTrades: ClosedTrade[];
  accounts: Account[];

  // Session stats
  sessionStats: SessionStats;

  // Replay
  replayMode: boolean;
  replayGeneration: number;

  // Actions
  resetForReplay: () => void;
  setWsStatus: (status: ConnectionStatus, reconnectAttempt?: number) => void;
  applyBackfill: (data: BackfillData) => void;
  updatePrice: (data: { price: number; bid?: number | null; ask?: number | null; timestamp: string }) => void;
  updateLevels: (levels: LevelZone[]) => void;
  addPrediction: (prediction: Prediction) => void;
  setObservation: (observation: Observation | null) => void;
  updateSessionStats: (stats: SessionStats) => void;
  setDataStatus: (status: string) => void;
  openPosition: (data: TradeOpenedPayload) => void;
  closePosition: (data: TradeClosedPayload) => void;
  updateAccount: (data: AccountUpdatePayload) => void;
  resolveOutcome: (data: OutcomeResolvedPayload) => void;
}

const DEFAULT_STATS: SessionStats = {
  signals_fired: 0,
  wins: 0,
  losses: 0,
  accuracy: 0,
};

export const useDashboardStore = create<DashboardState>((set) => ({
  // Initial state
  wsStatus: 'disconnected',
  wsReconnectAttempt: 0,
  dataStatus: 'disconnected',
  latestPrice: null,
  latestBid: null,
  latestAsk: null,
  levels: [],
  lastPrediction: null,
  todaysPredictions: [],
  activeObservation: null,
  openPositions: [],
  todaysTrades: [],
  accounts: [],
  sessionStats: { ...DEFAULT_STATS },
  replayMode: false,
  replayGeneration: 0,

  // Actions
  resetForReplay: () =>
    set((state) => ({
      latestPrice: null,
      latestBid: null,
      latestAsk: null,
      levels: [],
      lastPrediction: null,
      todaysPredictions: [],
      activeObservation: null,
      openPositions: [],
      todaysTrades: [],
      sessionStats: { ...DEFAULT_STATS },
      replayGeneration: state.replayGeneration + 1,
      accounts: state.accounts.map((a) => ({
        ...a,
        balance: 50000,
        daily_pnl: 0,
        status: 'active',
        has_position: false,
      })),
    })),

  setWsStatus: (status, reconnectAttempt) => set({ wsStatus: status, wsReconnectAttempt: reconnectAttempt ?? 0 }),

  applyBackfill: (data) =>
    set({
      dataStatus: data.connection_status as ConnectionStatus,
      latestPrice: data.latest_price,
      latestBid: data.latest_bid,
      latestAsk: data.latest_ask,
      levels: data.active_levels,
      lastPrediction: data.last_prediction,
      todaysPredictions: data.todays_predictions,
      activeObservation: data.active_observation,
      openPositions: data.open_positions,
      todaysTrades: data.todays_trades,
      accounts: data.accounts,
      sessionStats: data.session_stats ?? { ...DEFAULT_STATS },
      replayMode: data.replay_mode,
    }),

  updatePrice: (data) =>
    set({
      latestPrice: data.price,
      latestBid: data.bid ?? null,
      latestAsk: data.ask ?? null,
    }),

  updateLevels: (levels) => set({ levels }),

  addPrediction: (prediction) =>
    set((state) => ({
      lastPrediction: prediction,
      todaysPredictions: [...state.todaysPredictions, prediction],
      activeObservation: null, // observation completed → prediction fired
    })),

  setObservation: (observation) => set({ activeObservation: observation }),

  updateSessionStats: (stats) => set({ sessionStats: stats }),

  setDataStatus: (status) => set({ dataStatus: status as ConnectionStatus }),

  openPosition: (data) =>
    set((state) => ({
      openPositions: [
        ...state.openPositions,
        {
          account_id: data.account_id,
          direction: data.direction,
          entry_price: data.entry_price,
          entry_time: data.entry_time,
          contracts: data.contracts,
          group: data.group,
          tp_price: data.tp_price,
          sl_price: data.sl_price,
          unrealized_pnl: 0,
        },
      ],
    })),

  closePosition: (data) =>
    set((state) => ({
      openPositions: state.openPositions.filter(
        (p) => p.account_id !== data.account_id,
      ),
      todaysTrades: [
        ...state.todaysTrades,
        {
          account_id: data.account_id,
          direction: data.direction,
          entry_price: data.entry_price,
          exit_price: data.exit_price,
          contracts: data.contracts,
          entry_time: data.entry_time,
          exit_time: data.exit_time,
          pnl: data.pnl,
          pnl_points: data.pnl_points,
          exit_reason: data.exit_reason,
          group: data.group,
        },
      ],
    })),

  updateAccount: (data) =>
    set((state) => ({
      accounts: state.accounts.map((a) =>
        a.account_id === data.account_id
          ? { ...a, balance: data.balance, daily_pnl: data.daily_pnl, status: data.status }
          : a,
      ),
    })),

  resolveOutcome: (data) =>
    set((state) => ({
      todaysPredictions: state.todaysPredictions.map((p) =>
        p.event_id === data.event_id
          ? {
              ...p,
              prediction_correct: data.prediction_correct,
              actual_class: data.actual_class,
              mfe_points: data.mfe_points,
              mae_points: data.mae_points,
            }
          : p,
      ),
    })),
}));
