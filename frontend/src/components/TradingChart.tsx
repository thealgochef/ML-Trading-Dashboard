import { useEffect, useRef } from 'react';
import {
  createChart,
  CandlestickSeries,
  ColorType,
} from 'lightweight-charts';
import type {
  IChartApi,
  ISeriesApi,
  CandlestickData,
  Time,
  AutoscaleInfo,
} from 'lightweight-charts';
import { DashboardDatafeed } from '../datafeed/DashboardDatafeed';
import type { ChartBar } from '../datafeed/DashboardDatafeed';
import { ChartManager } from '../chart/ChartManager';
import { useDashboardStore } from '../store/dashboardStore';

const MIN_BAR_SPACING = 12;
const MAX_BAR_SPACING = 16;
const DEFAULT_BAR_SPACING = 14;

interface TradingChartProps {
  timeframe?: string;
}

/**
 * Self-contained Lightweight Charts wrapper — black box.
 * No parent component touches the chart or series objects.
 */
export function TradingChart({ timeframe = '5m' }: TradingChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const datafeedRef = useRef<DashboardDatafeed | null>(null);
  const chartManagerRef = useRef<ChartManager | null>(null);
  const lastBarTimeRef = useRef<number>(0);
  const baseBarSpacingRef = useRef<number>(0);
  const baseHalfRangeRef = useRef<number>(0);

  // ── Mount: create chart, series, and realtime connection ──────

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#0f0f1a' },
        textColor: '#e0e0e0',
      },
      grid: {
        vertLines: { color: '#1e1e2f' },
        horzLines: { color: '#1e1e2f' },
      },
      crosshair: {
        vertLine: { color: '#444' },
        horzLine: { color: '#444' },
      },
      timeScale: {
        borderColor: '#2a2a3d',
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 10,
        barSpacing: DEFAULT_BAR_SPACING,
      },
      rightPriceScale: {
        borderColor: '#2a2a3d',
        autoScale: true,
      },
      handleScale: {
        mouseWheel: true,
        pinch: true,
        axisPressedMouseMove: false,
        axisDoubleClickReset: false,
      },
      handleScroll: {
        mouseWheel: false,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
      autoSize: true,
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#00c853',
      downColor: '#ff1744',
      borderUpColor: '#00c853',
      borderDownColor: '#ff1744',
      wickUpColor: '#00c853',
      wickDownColor: '#ff1744',
      autoscaleInfoProvider: (baseImpl: () => AutoscaleInfo | null) => {
        const baseInfo = baseImpl();
        if (!baseInfo?.priceRange) return baseInfo;

        const currentBarSpacing = chart.timeScale().options().barSpacing;

        // First call after data load: capture baseline for proportional zoom
        // Multiply by 2 so the initial view shows a wider price range
        // (shorter, more compact candles — closer to TradingView proportions)
        if (baseBarSpacingRef.current === 0 && currentBarSpacing > 0) {
          baseBarSpacingRef.current = currentBarSpacing;
          baseHalfRangeRef.current =
            (baseInfo.priceRange.maxValue - baseInfo.priceRange.minValue) / 2 * 2;
          const center =
            (baseInfo.priceRange.minValue + baseInfo.priceRange.maxValue) / 2;
          return {
            priceRange: {
              minValue: center - baseHalfRangeRef.current,
              maxValue: center + baseHalfRangeRef.current,
            },
          };
        }

        // Proportional zoom: Y-axis range scales inversely with barSpacing
        const scaleFactor = currentBarSpacing / baseBarSpacingRef.current;
        const newHalfRange = baseHalfRangeRef.current / scaleFactor;
        const center =
          (baseInfo.priceRange.minValue + baseInfo.priceRange.maxValue) / 2;
        return {
          priceRange: {
            minValue: center - newHalfRange,
            maxValue: center + newHalfRange,
          },
        };
      },
    });

    // Clamp barSpacing to prevent extreme zoom levels
    chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
      const bs = chart.timeScale().options().barSpacing;
      const clamped = Math.max(MIN_BAR_SPACING, Math.min(MAX_BAR_SPACING, bs));
      if (clamped !== bs) {
        chart.timeScale().applyOptions({ barSpacing: clamped });
      }
    });

    chartRef.current = chart;
    seriesRef.current = series;

    const cm = new ChartManager(series);
    chartManagerRef.current = cm;

    const datafeed = new DashboardDatafeed();
    datafeedRef.current = datafeed;

    // Initial bar update handler — overwritten by timeframe-change effect
    datafeed.onBarUpdate = (bar: ChartBar, barTimeframe: string) => {
      if (barTimeframe !== timeframe) return;
      if (bar.time >= lastBarTimeRef.current - 1) {
        series.update(bar as CandlestickData<Time>);
        if (bar.time > lastBarTimeRef.current) {
          lastBarTimeRef.current = bar.time;
        }
      }
    };

    datafeed.connectRealtime();

    return () => {
      datafeed.destroy();
      datafeedRef.current = null;
      cm.destroy();
      chartManagerRef.current = null;
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      lastBarTimeRef.current = 0;
    };
  }, []);

  // ── Timeframe change: reload bars + resubscribe ───────────────

  useEffect(() => {
    const series = seriesRef.current;
    const datafeed = datafeedRef.current;
    const chart = chartRef.current;
    if (!series || !datafeed || !chart) return;

    let cancelled = false;
    const isTick = timeframe === '147t' || timeframe === '987t' || timeframe === '2000t';

    // For tick timeframes: full reload on each bar completion prevents
    // rendering glitches from WebSocket/HTTP race conditions during fast replay.
    // For time-based: incremental update is fine (one bar per time bucket).
    datafeed.onBarUpdate = (bar: ChartBar, barTimeframe: string) => {
      if (barTimeframe !== timeframe) return;

      if (isTick) {
        // Full reload from backend — same as switching timeframes
        datafeed.refreshTickBars().then((bars) => {
          if (cancelled || bars.length === 0) return;
          series.setData(bars as CandlestickData<Time>[]);
          lastBarTimeRef.current = bars[bars.length - 1].time;
        });
      } else {
        if (bar.time >= lastBarTimeRef.current - 1) {
          series.update(bar as CandlestickData<Time>);
          if (bar.time > lastBarTimeRef.current) {
            lastBarTimeRef.current = bar.time;
          }
        }
      }
    };

    datafeed.subscribeTimeframe(timeframe);

    datafeed.fetchBars(timeframe).then((bars) => {
      if (cancelled) return;
      if (bars.length > 0) {
        baseBarSpacingRef.current = 0; // Reset so provider recalibrates
        series.setData(bars as CandlestickData<Time>[]);
        lastBarTimeRef.current = bars[bars.length - 1].time;
        datafeed.setLastBar(bars[bars.length - 1]);

        // Show last ~150 bars instead of entire dataset (prevents vertical compression)
        const visibleBars = 150;
        const from = Math.max(0, bars.length - visibleBars);
        const to = bars.length - 1;
        chart.timeScale().setVisibleLogicalRange({ from, to });
      } else {
        series.setData([]);
        lastBarTimeRef.current = 0;
      }
    });

    return () => {
      cancelled = true;
    };
  }, [timeframe]);

  // ── Sync store → chart overlays ────────────────────────────────

  const levels = useDashboardStore((s) => s.levels);
  const lastPrediction = useDashboardStore((s) => s.lastPrediction);
  const openPositions = useDashboardStore((s) => s.openPositions);
  const todaysTrades = useDashboardStore((s) => s.todaysTrades);
  const todaysPredictions = useDashboardStore((s) => s.todaysPredictions);

  // Level lines
  useEffect(() => {
    chartManagerRef.current?.syncLevels(levels);
  }, [levels]);

  // Prediction markers
  useEffect(() => {
    if (lastPrediction) {
      chartManagerRef.current?.addPredictionMarker(lastPrediction);
    }
  }, [lastPrediction]);

  // Trade entries — call addTradeEntry for every position; ChartManager dedupes
  useEffect(() => {
    const cm = chartManagerRef.current;
    if (!cm) return;
    for (const pos of openPositions) {
      cm.addTradeEntry(pos);
    }
  }, [openPositions]);

  // Trade exits — rebuild entry + exit markers from backfill; ChartManager dedupes
  useEffect(() => {
    const cm = chartManagerRef.current;
    if (!cm) return;
    for (const trade of todaysTrades) {
      // Reconstruct entry marker (TP/SL lines immediately removed by addTradeExit)
      cm.addTradeEntry({
        account_id: trade.account_id,
        direction: trade.direction,
        entry_price: trade.entry_price,
        entry_time: trade.entry_time,
        contracts: trade.contracts,
        tp_price: trade.exit_price,
        sl_price: trade.exit_price,
        unrealized_pnl: 0,
        group: trade.group,
      });
      cm.addTradeExit(trade);
    }
  }, [todaysTrades]);

  // Prediction + outcome markers — rebuild from backfill; ChartManager dedupes
  useEffect(() => {
    const cm = chartManagerRef.current;
    if (!cm) return;
    for (const pred of todaysPredictions) {
      cm.addPredictionMarker(pred);
      if (pred.prediction_correct != null) {
        cm.updateOutcome(pred.event_id, pred.prediction_correct);
      }
    }
  }, [todaysPredictions]);

  return (
    <div
      ref={containerRef}
      className="w-full h-full"
    />
  );
}
