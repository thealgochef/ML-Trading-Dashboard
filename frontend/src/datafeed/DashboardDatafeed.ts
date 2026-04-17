/**
 * Backend communication adapter for chart data.
 *
 * - fetchBars()          → GET /api/data/ohlcv?timeframe={tf}&since={since}
 * - Real-time updates via WebSocketManager (shared connection)
 *
 * Timestamp conversion (ISO string → Unix seconds) happens ONLY in this file.
 * Consumers receive bars with `time` already as UTCTimestamp (seconds).
 */

import type { UTCTimestamp } from 'lightweight-charts';
import { wsManager } from '../websocket/WebSocketManager';
import { API_BASE } from '../config';

export interface ChartBar {
  time: UTCTimestamp;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface BackendBar {
  timestamp: string; // ISO 8601
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

function toChartBar(bar: BackendBar): ChartBar {
  return {
    time: (new Date(bar.timestamp).getTime() / 1000) as UTCTimestamp,
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
    volume: bar.volume,
  };
}

function isTickTimeframe(timeframe: string | null): boolean {
  return timeframe === '147t' || timeframe === '987t' || timeframe === '2000t';
}

export type BarUpdateCallback = (bar: ChartBar, timeframe: string) => void;

export class DashboardDatafeed {
  private subscribedTimeframe: string | null = null;
  private unsubPrice: (() => void) | null = null;
  private unsubBar: (() => void) | null = null;

  /** The in-progress bar being updated by price_update messages (non-tick only). */
  private currentBar: ChartBar | null = null;

  /** Time of the last completed bar — new bars must have time > this. */
  private lastCompletedBarTime: number = 0;

  /** Prevent overlapping backend refreshes for tick partial bars. */
  private tickRefreshInFlight = false;

  onBarUpdate: BarUpdateCallback | null = null;

  // ── HTTP: fetch historical bars ──────────────────────────────

  async fetchBars(timeframe: string, since?: string): Promise<ChartBar[]> {
    const params = new URLSearchParams({ timeframe });
    if (since) params.set('since', since);

    const url = `${API_BASE}/api/data/ohlcv?${params}`;
    const response = await fetch(url);

    if (!response.ok) {
      console.error(`fetchBars failed: HTTP ${response.status}`);
      return [];
    }

    const data: { bars: BackendBar[]; timeframe: string } = await response.json();

    if (!data.bars || data.bars.length === 0) return [];

    const bars = data.bars.map(toChartBar);

    // Lightweight Charts requires bars sorted ascending by time
    bars.sort((a, b) => a.time - b.time);

    // Deduplicate: if two bars share the same timestamp, keep the last one
    const deduped: ChartBar[] = [];
    for (const bar of bars) {
      if (deduped.length > 0 && deduped[deduped.length - 1].time === bar.time) {
        deduped[deduped.length - 1] = bar;
      } else {
        deduped.push(bar);
      }
    }

    return deduped;
  }

  /**
   * Initialize currentBar from the last bar returned by fetchBars().
   * Called by TradingChart after series.setData(). For tick timeframes,
   * the backend is the source of truth for the rightmost partial bar.
   */
  setLastBar(bar: ChartBar): void {
    if (isTickTimeframe(this.subscribedTimeframe)) {
      this.currentBar = null;
      this.lastCompletedBarTime = bar.time - 1;
      return;
    }
    this.currentBar = { ...bar };
    this.lastCompletedBarTime = bar.time - 1;
  }

  // ── WebSocket: real-time updates (via shared WebSocketManager) ─

  connectRealtime(): void {
    // Listen for price_update and bar_update via the shared manager
    this.unsubPrice = wsManager.on('price_update', (data) => {
      this.handlePriceUpdate(data as { price: number; timestamp: string });
    });

    this.unsubBar = wsManager.on('bar_update', (data) => {
      this.handleBarUpdate(data as { timeframe: string; bar: BackendBar });
    });
  }

  /**
   * price_update arrives ~1/sec with the latest trade price.
   * - Tick timeframes: refresh rightmost bar from backend true partial state.
   * - Non-tick timeframes: continue local in-progress approximation.
   */
  private handlePriceUpdate(data: { price: number; timestamp: string }): void {
    if (!this.onBarUpdate || !this.subscribedTimeframe) return;

    if (isTickTimeframe(this.subscribedTimeframe)) {
      this.refreshTickPartialBar();
      return;
    }

    const price = data.price;

    if (this.currentBar) {
      this.currentBar.close = price;
      this.currentBar.high = Math.max(this.currentBar.high, price);
      this.currentBar.low = Math.min(this.currentBar.low, price);
    } else {
      const time = Math.floor(new Date(data.timestamp).getTime() / 1000);
      const safeTime = Math.max(time, this.lastCompletedBarTime + 1);
      this.currentBar = {
        time: safeTime as UTCTimestamp,
        open: price,
        high: price,
        low: price,
        close: price,
        volume: 0,
      };
    }

    this.onBarUpdate({ ...this.currentBar }, this.subscribedTimeframe);
  }

  /**
   * Full reload of tick bars from backend. During fast replay, incremental
   * updates via WebSocket can miss bars or get out of sync. A full reload
   * (same as switching timeframes) is the reliable path.
   */
  async refreshTickBars(): Promise<ChartBar[]> {
    if (!this.subscribedTimeframe || !isTickTimeframe(this.subscribedTimeframe)) return [];
    if (this.tickRefreshInFlight) return [];

    this.tickRefreshInFlight = true;
    const timeframe = this.subscribedTimeframe;

    try {
      const bars = await this.fetchBars(timeframe);
      if (this.subscribedTimeframe !== timeframe) return [];
      return bars;
    } catch (err) {
      console.error('tick bar refresh failed', err);
      return [];
    } finally {
      this.tickRefreshInFlight = false;
    }
  }

  /** @deprecated Use refreshTickBars + series.setData instead */
  private async refreshTickPartialBar(): Promise<void> {
    if (!this.onBarUpdate || !this.subscribedTimeframe) return;
    const bars = await this.refreshTickBars();
    if (bars.length > 0 && this.onBarUpdate) {
      this.onBarUpdate(bars[bars.length - 1], this.subscribedTimeframe!);
    }
  }

  /**
   * bar_update arrives when a bar completes. Finalize and start fresh.
   */
  private handleBarUpdate(data: { timeframe: string; bar: BackendBar }): void {
    if (!this.onBarUpdate) return;

    const completedBar = toChartBar(data.bar);
    this.onBarUpdate(completedBar, data.timeframe);
    this.lastCompletedBarTime = completedBar.time;
    this.currentBar = null;

    if (isTickTimeframe(data.timeframe) && this.subscribedTimeframe === data.timeframe) {
      void this.refreshTickPartialBar();
    }
  }

  subscribeTimeframe(timeframe: string): void {
    this.subscribedTimeframe = timeframe;
    this.currentBar = null;
    this.lastCompletedBarTime = 0;
    this.tickRefreshInFlight = false;
    wsManager.send({
      type: 'subscribe_timeframe',
      data: { timeframe },
    });
  }

  // ── Cleanup ────────────────────────────────────────────────────

  destroy(): void {
    this.onBarUpdate = null;
    this.subscribedTimeframe = null;
    this.currentBar = null;
    this.lastCompletedBarTime = 0;
    this.tickRefreshInFlight = false;
    this.unsubPrice?.();
    this.unsubBar?.();
    this.unsubPrice = null;
    this.unsubBar = null;
  }
}
