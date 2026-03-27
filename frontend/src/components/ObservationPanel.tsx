import { useEffect, useState } from 'react';
import { useDashboardStore } from '../store/dashboardStore';
import type { LevelType } from '../types';

const LEVEL_LABELS: Partial<Record<LevelType, string>> = {
  pdh: 'PDH',
  pdl: 'PDL',
  asia_high: 'Asia H',
  asia_low: 'Asia L',
  london_high: 'Lon H',
  london_low: 'Lon L',
  manual: 'Manual',
};

export function ObservationPanel() {
  const observation = useDashboardStore((s) => s.activeObservation);

  if (!observation || observation.status !== 'active') {
    return (
      <div className="px-3 py-2 border-b border-[#1e1e2f]">
        <h3 className="text-xs font-semibold text-[#666] uppercase tracking-wider mb-2">
          Observation
        </h3>
        <div className="text-xs text-[#555] font-mono">Waiting for level touch</div>
      </div>
    );
  }

  return (
    <div className="px-3 py-2 border-b border-[#1e1e2f]">
      <h3 className="text-xs font-semibold text-[#666] uppercase tracking-wider mb-2">
        Observation
      </h3>
      <ActiveObservation
        eventId={observation.event_id}
        direction={observation.direction}
        levelType={observation.level_type}
        levelPrice={observation.level_price}
        startTime={observation.start_time}
        endTime={observation.end_time}
        tradesAccumulated={observation.trades_accumulated}
      />
    </div>
  );
}

function ActiveObservation({
  eventId,
  direction,
  levelType,
  levelPrice,
  startTime,
  endTime,
  tradesAccumulated,
}: {
  eventId: string;
  direction: string;
  levelType?: string;
  levelPrice: number;
  startTime: string;
  endTime: string;
  tradesAccumulated: number;
}) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, []);

  const startMs = new Date(startTime).getTime();
  const endMs = new Date(endTime).getTime();
  const totalMs = endMs - startMs;
  const remainingMs = Math.max(0, endMs - now);
  const elapsedMs = totalMs - remainingMs;
  const progressPct = totalMs > 0 ? Math.min(100, (elapsedMs / totalMs) * 100) : 0;

  const remainingSec = Math.ceil(remainingMs / 1000);
  const minutes = Math.floor(remainingSec / 60);
  const seconds = remainingSec % 60;
  const isLong = direction === 'long';
  const levelLabel = levelType ? (LEVEL_LABELS[levelType as keyof typeof LEVEL_LABELS] ?? levelType) : '';

  return (
    <div className="space-y-2">
      {/* Direction + event ID */}
      <div className="flex items-center justify-between">
        <span
          className={`px-2 py-0.5 text-xs font-mono font-bold rounded border ${
            isLong
              ? 'bg-green-500/20 text-green-400 border-green-500/30'
              : 'bg-red-500/20 text-red-400 border-red-500/30'
          }`}
        >
          {direction.toUpperCase()}
        </span>
        <span className="text-xs font-mono text-[#555]">
          {eventId.slice(0, 8)}
        </span>
      </div>

      {/* Level info */}
      <div className="text-xs font-mono text-[#888]">
        {levelLabel}{levelLabel ? ' @ ' : ''}{levelPrice.toFixed(2)}
      </div>

      {/* Countdown */}
      <div className="text-center">
        {remainingMs > 0 ? (
          <span className="text-lg font-mono font-bold text-white tabular-nums">
            {minutes}:{seconds.toString().padStart(2, '0')}
          </span>
        ) : (
          <span className="text-sm font-mono text-yellow-400 animate-pulse">
            Processing...
          </span>
        )}
        <div className="text-xs font-mono text-[#555] mt-0.5">
          remaining
        </div>
      </div>

      {/* Progress bar */}
      <div className="w-full h-1.5 rounded-full bg-[#1e1e2f] overflow-hidden">
        <div
          className="h-full rounded-full bg-blue-500 transition-all duration-1000 ease-linear"
          style={{ width: `${progressPct}%` }}
        />
      </div>

      {/* Trades accumulated */}
      <div className="flex items-center justify-between text-xs font-mono">
        <span className="text-[#888]">Trades accumulated</span>
        <span className="text-white tabular-nums">{tradesAccumulated}</span>
      </div>
    </div>
  );
}
