/** Shared TypeScript types matching backend WebSocket message formats. */

// ── Level Types ──────────────────────────────────────────────────

export type LevelType =
  | 'pdh'
  | 'pdl'
  | 'asia_high'
  | 'asia_low'
  | 'london_high'
  | 'london_low'
  | 'manual';

export type LevelSide = 'high' | 'low';
export type TradeDirection = 'long' | 'short';

export interface KeyLevel {
  type: LevelType;
  price: number;
  is_manual: boolean;
}

export interface LevelZone {
  zone_id: string;
  price: number;
  side: LevelSide;
  is_touched: boolean;
  levels: KeyLevel[];
}

// ── Prediction ───────────────────────────────────────────────────

export interface Prediction {
  event_id: string;
  predicted_class: string;
  is_executable: boolean;
  probabilities: Record<string, number>;
  features: Record<string, number>;
  trade_direction: TradeDirection;
  level_price: number;
  level_type?: LevelType | null;
  model_version: string;
  timestamp: string;
  prediction_correct?: boolean | null;
  actual_class?: number | string | null;
  mfe_points?: number | null;
  mae_points?: number | null;
}

// ── Observation ──────────────────────────────────────────────────

export type ObservationStatus =
  | 'active'
  | 'completed'
  | 'discarded_feed_drop'
  | 'discarded_time_cutoff'
  | 'discarded_level_deleted';

export interface Observation {
  event_id: string;
  direction: TradeDirection;
  level_type?: LevelType;
  level_price: number;
  start_time: string;
  end_time: string;
  status: ObservationStatus;
  trades_accumulated: number;
}

// ── Trading ──────────────────────────────────────────────────────

export interface OpenPosition {
  account_id: string;
  direction: TradeDirection;
  entry_price: number;
  contracts: number;
  entry_time: string;
  unrealized_pnl: number;
  tp_price: number;
  sl_price: number;
  group: string;
}

export interface ClosedTrade {
  account_id: string;
  direction: TradeDirection;
  entry_price: number;
  exit_price: number;
  contracts: number;
  entry_time: string;
  exit_time: string;
  pnl: number;
  pnl_points: number;
  exit_reason: string;
  group: string;
}

export interface Account {
  account_id: string;
  label: string;
  group: string;
  balance: number;
  status: string;
  tier: number;
  has_position: boolean;
  daily_pnl: number;
}

// ── Session Stats ────────────────────────────────────────────────

export interface SessionStats {
  signals_fired: number;
  wins: number;
  losses: number;
  accuracy: number;
  total_trades?: number;
  total_pnl?: number;
}

// ── Connection ───────────────────────────────────────────────────

export type ConnectionStatus = 'connected' | 'disconnected' | 'connecting';

// ── Backfill (initial state snapshot) ────────────────────────────

export interface BackfillData {
  connection_status: string;
  latest_price: number | null;
  latest_bid: number | null;
  latest_ask: number | null;
  active_levels: LevelZone[];
  active_observation: Observation | null;
  last_prediction: Prediction | null;
  open_positions: OpenPosition[];
  todays_trades: ClosedTrade[];
  todays_predictions: Prediction[];
  session_stats: SessionStats;
  accounts: Account[];
  config: BackfillConfig;
  replay_mode: boolean;
}

export interface BackfillConfig {
  group_a_tp: number;
  group_b_tp: number;
  group_a_sl: number;
  group_b_sl: number;
  second_signal_mode: string;
  overlays: {
    ema_13: boolean;
    ema_48: boolean;
    ema_200: boolean;
    vwap: boolean;
    levels: boolean;
  };
}
