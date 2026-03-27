/**
 * Manages chart overlays: level price lines, prediction markers,
 * trade entry/exit markers, and TP/SL lines.
 *
 * Owns all createPriceLine() / setMarkers() calls.
 * TradingChart feeds it store state via useEffect.
 *
 * Dedup strategy: all 5 accounts mirror the same signal, so we draw
 * ONE entry marker, ONE set of TP/SL lines, and ONE exit marker per
 * unique signal (keyed by direction + entry_price).
 */

import {
  createSeriesMarkers,
} from 'lightweight-charts';
import type {
  ISeriesApi,
  ISeriesMarkersPluginApi,
  IPriceLine,
  SeriesMarker,
  Time,
} from 'lightweight-charts';
import type {
  LevelZone,
  LevelType,
  Prediction,
  OpenPosition,
  ClosedTrade,
} from '../types';

/** Map level type → color */
const LEVEL_COLORS: Record<LevelType, string> = {
  pdh: '#ff9800',
  pdl: '#ff9800',
  asia_high: '#ab47bc',
  asia_low: '#ab47bc',
  london_high: '#29b6f6',
  london_low: '#29b6f6',
  manual: '#78909c',
};

/** Map level type → display label */
const LEVEL_LABELS: Record<LevelType, string> = {
  pdh: 'PDH',
  pdl: 'PDL',
  asia_high: 'Asia H',
  asia_low: 'Asia L',
  london_high: 'Lon H',
  london_low: 'Lon L',
  manual: 'Manual',
};

/** Map predicted_class → short display name */
const CLASS_SHORT_NAMES: Record<string, string> = {
  tradeable_reversal: 'reversal',
  trap_reversal: 'trap',
  aggressive_blowthrough: 'blowthrough',
};

function isoToTime(iso: string): Time {
  return (new Date(iso).getTime() / 1000) as unknown as Time;
}

function signalKey(direction: string, entryPrice: number): string {
  return `${direction}_${entryPrice}`;
}

export class ChartManager {
  private series: ISeriesApi<'Candlestick'>;
  private markersPlugin: ISeriesMarkersPluginApi<Time>;

  // Level lines
  private levelLines: IPriceLine[] = [];

  // All markers (predictions + trade entries + trade exits)
  private markers: SeriesMarker<Time>[] = [];

  // Prediction dedup: one marker per event_id
  private drawnPredictionIds = new Set<string>();

  // Prediction event_id → time (for outcome marker updates)
  private predictionTimeMap = new Map<string, Time>();

  // Outcome dedup: only apply ✓/✗ once per event_id
  private resolvedOutcomeIds = new Set<string>();

  // Trade dedup: one entry marker per signal
  private drawnEntryKeys = new Set<string>();

  // Trade TP/SL lines: one set per signal
  private tradeLines = new Map<string, { tp: IPriceLine; sl: IPriceLine }>();

  // Trade dedup: one exit marker per signal
  private drawnExitKeys = new Set<string>();

  constructor(series: ISeriesApi<'Candlestick'>) {
    this.series = series;
    this.markersPlugin = createSeriesMarkers(series);
  }

  // ── Level Lines ────────────────────────────────────────────────

  syncLevels(zones: LevelZone[]): void {
    for (const line of this.levelLines) {
      this.series.removePriceLine(line);
    }
    this.levelLines = [];

    for (const zone of zones) {
      for (const level of zone.levels) {
        const color = LEVEL_COLORS[level.type] ?? '#78909c';
        const label = LEVEL_LABELS[level.type] ?? level.type;
        const line = this.series.createPriceLine({
          price: level.price,
          color,
          lineWidth: 1,
          lineStyle: zone.is_touched ? 1 : 0,
          axisLabelVisible: true,
          title: label,
        });
        this.levelLines.push(line);
      }
    }
  }

  // ── Prediction Markers ─────────────────────────────────────────

  addPredictionMarker(prediction: Prediction): void {
    if (this.drawnPredictionIds.has(prediction.event_id)) return;
    this.drawnPredictionIds.add(prediction.event_id);

    const time = isoToTime(prediction.timestamp);
    this.predictionTimeMap.set(prediction.event_id, time);

    // Build label on two lines to reduce horizontal width:
    //   Line 1: "{LEVEL_TYPE} {DIRECTION}"
    //   Line 2: "{class} {confidence}%"
    const levelLabel = prediction.level_type
      ? (LEVEL_LABELS[prediction.level_type] ?? prediction.level_type).toUpperCase()
      : '';
    const direction = prediction.trade_direction.toUpperCase();
    const className = CLASS_SHORT_NAMES[prediction.predicted_class] ?? prediction.predicted_class;
    const topProb = Math.round(
      (prediction.probabilities[prediction.predicted_class] ?? 0) * 100,
    );
    const line1 = `${levelLabel} ${direction}`.trim();
    const line2 = `${className} ${topProb}%`;

    const marker: SeriesMarker<Time> = {
      time,
      position: 'belowBar',
      color: '#888888',
      shape: 'circle',
      text: `${line1}\n${line2}`,
    };

    this.markers.push(marker);
    this.flushMarkers();
  }

  /**
   * Update a prediction marker's text with ✓ or ✗ after outcome resolution.
   */
  updateOutcome(eventId: string, correct: boolean): void {
    if (this.resolvedOutcomeIds.has(eventId)) return;

    const time = this.predictionTimeMap.get(eventId);
    if (!time) return;

    const timeNum = time as number;
    const idx = this.markers.findIndex(
      (m) =>
        (m.time as number) === timeNum &&
        m.shape === 'circle',
    );
    if (idx === -1) return;

    this.resolvedOutcomeIds.add(eventId);

    const existing = this.markers[idx];
    const suffix = correct ? ' \u2713' : ' \u2717';
    this.markers[idx] = {
      ...existing,
      text: (existing.text ?? '') + suffix,
      // Keep gray — only trade entry/exit markers use color for results
    };
    this.flushMarkers();
  }

  // ── Trade Entry ────────────────────────────────────────────────

  /**
   * Called for each new OpenPosition. Draws entry marker + TP/SL lines
   * only for the first account per signal (dedup).
   */
  addTradeEntry(pos: OpenPosition): void {
    const key = signalKey(pos.direction, pos.entry_price);
    if (this.drawnEntryKeys.has(key)) return;
    this.drawnEntryKeys.add(key);

    const isLong = pos.direction === 'long';
    const time = isoToTime(pos.entry_time);
    const entryLabel = `${isLong ? 'BUY' : 'SELL'} ${pos.entry_price.toFixed(2)}`;

    const marker: SeriesMarker<Time> = {
      time,
      position: isLong ? 'belowBar' : 'aboveBar',
      color: isLong ? '#00c853' : '#ff1744',
      shape: isLong ? 'arrowUp' : 'arrowDown',
      text: entryLabel,
    };
    this.markers.push(marker);
    this.flushMarkers();

    // TP/SL price lines
    const tp = this.series.createPriceLine({
      price: pos.tp_price,
      color: '#00c853',
      lineWidth: 1,
      lineStyle: 2, // dashed
      axisLabelVisible: true,
      title: `TP ${pos.tp_price.toFixed(2)}`,
    });
    const sl = this.series.createPriceLine({
      price: pos.sl_price,
      color: '#ff1744',
      lineWidth: 1,
      lineStyle: 2, // dashed
      axisLabelVisible: true,
      title: `SL ${pos.sl_price.toFixed(2)}`,
    });
    this.tradeLines.set(key, { tp, sl });
  }

  // ── Trade Exit ─────────────────────────────────────────────────

  /**
   * Called for each new ClosedTrade. Draws exit marker and removes
   * TP/SL lines only for the first account per signal (dedup).
   */
  addTradeExit(trade: ClosedTrade): void {
    const key = signalKey(trade.direction, trade.entry_price);
    if (this.drawnExitKeys.has(key)) return;
    this.drawnExitKeys.add(key);

    const time = isoToTime(trade.exit_time);
    const isLong = trade.direction === 'long';
    const reason = trade.exit_reason.toLowerCase();

    let text: string;
    let color: string;
    let shape: 'arrowUp' | 'arrowDown';
    let position: 'aboveBar' | 'belowBar';

    if (reason === 'tp') {
      const pts = Math.abs(trade.pnl_points);
      text = `TP +${pts.toFixed(1)}pts`;
      color = '#00c853';
      // Long TP: took profit above → aboveBar | Short TP: took profit below → belowBar
      shape = isLong ? 'arrowDown' : 'arrowUp';
      position = isLong ? 'aboveBar' : 'belowBar';
    } else if (reason === 'sl') {
      const pts = Math.abs(trade.pnl_points);
      text = `SL -${pts.toFixed(1)}pts`;
      color = '#ff1744';
      // Long SL: stopped out below → belowBar | Short SL: stopped out above → aboveBar
      shape = isLong ? 'arrowDown' : 'arrowUp';
      position = isLong ? 'belowBar' : 'aboveBar';
    } else {
      // Flatten / manual / dll / blown — same logic as SL
      const pts = trade.pnl_points;
      const sign = pts >= 0 ? '+' : '';
      text = `FLAT ${sign}${pts.toFixed(1)}pts`;
      color = '#ffd600';
      shape = isLong ? 'arrowDown' : 'arrowUp';
      position = isLong ? 'belowBar' : 'aboveBar';
    }

    const marker: SeriesMarker<Time> = {
      time,
      position,
      color,
      shape,
      text,
    };
    this.markers.push(marker);
    this.flushMarkers();

    // Remove TP/SL lines
    const lines = this.tradeLines.get(key);
    if (lines) {
      this.series.removePriceLine(lines.tp);
      this.series.removePriceLine(lines.sl);
      this.tradeLines.delete(key);
    }
  }

  // ── Helpers ────────────────────────────────────────────────────

  private flushMarkers(): void {
    this.markers.sort((a, b) => (a.time as number) - (b.time as number));
    this.markersPlugin.setMarkers(this.markers);
  }

  destroy(): void {
    for (const line of this.levelLines) {
      try { this.series.removePriceLine(line); } catch { /* disposed */ }
    }
    for (const { tp, sl } of this.tradeLines.values()) {
      try { this.series.removePriceLine(tp); } catch { /* disposed */ }
      try { this.series.removePriceLine(sl); } catch { /* disposed */ }
    }
    this.levelLines = [];
    this.tradeLines.clear();
    this.markers = [];
    this.drawnPredictionIds.clear();
    this.resolvedOutcomeIds.clear();
    this.drawnEntryKeys.clear();
    this.drawnExitKeys.clear();
    this.predictionTimeMap.clear();
  }
}
