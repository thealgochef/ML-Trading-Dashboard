import { useEffect, useRef } from 'react';
import {
  createChart,
  LineSeries,
  ColorType,
} from 'lightweight-charts';
import type {
  IChartApi,
  Time,
} from 'lightweight-charts';

interface EquityCurveProps {
  data: { time: string; value: number }[];
}

/**
 * Simple equity curve using Lightweight Charts LineSeries.
 * Expects data sorted chronologically with ISO timestamps.
 */
export function EquityCurve({ data }: EquityCurveProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#0a0a14' },
        textColor: '#666',
      },
      grid: {
        vertLines: { color: '#1a1a2a' },
        horzLines: { color: '#1a1a2a' },
      },
      timeScale: {
        borderColor: '#1e1e2f',
        timeVisible: true,
      },
      rightPriceScale: {
        borderColor: '#1e1e2f',
      },
      crosshair: {
        vertLine: { color: '#333' },
        horzLine: { color: '#333' },
      },
      autoSize: true,
    });

    const series = chart.addSeries(LineSeries, {
      color: '#29b6f6',
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
    });

    // Convert ISO timestamps to UTCTimestamp (seconds)
    const chartData = data.map((d) => ({
      time: (new Date(d.time).getTime() / 1000) as unknown as Time,
      value: d.value,
    }));

    if (chartData.length > 0) {
      series.setData(chartData);
      chart.timeScale().fitContent();
    }

    // Zero line
    series.createPriceLine({
      price: 0,
      color: '#333',
      lineWidth: 1,
      lineStyle: 1,
      axisLabelVisible: false,
      title: '',
    });

    chartRef.current = chart;

    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, [data]);

  return <div ref={containerRef} className="w-full h-full" />;
}
