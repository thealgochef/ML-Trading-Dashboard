import { useDashboardStore } from '../store/dashboardStore';

export function PredictionPanel() {
  const prediction = useDashboardStore((s) => s.lastPrediction);
  const observation = useDashboardStore((s) => s.activeObservation);

  return (
    <div className="px-3 py-2 border-b border-[#1e1e2f]">
      <h3 className="text-xs font-semibold text-[#666] uppercase tracking-wider mb-2">
        Prediction
      </h3>

      {/* Active observation indicator */}
      {observation && observation.status === 'active' && (
        <div className="mb-2 px-2 py-1 rounded bg-yellow-500/10 border border-yellow-500/30 text-xs font-mono text-yellow-400">
          Observing {observation.direction.toUpperCase()} @ {observation.level_price.toFixed(2)}
          <span className="ml-2 text-[#888]">
            {observation.trades_accumulated} ticks
          </span>
        </div>
      )}

      {/* Last prediction */}
      {prediction ? (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <DirectionBadge
              direction={prediction.trade_direction}
              executable={prediction.is_executable}
            />
            <span className="text-xs font-mono text-[#888]">
              @ {prediction.level_price.toFixed(2)}
            </span>
          </div>

          {/* Probabilities */}
          <div className="flex gap-2 text-xs font-mono">
            {Object.entries(prediction.probabilities).map(([cls, prob]) => (
              <span key={cls} className="text-[#888]">
                C{cls}: <span className="text-white">{((prob as number) * 100).toFixed(0)}%</span>
              </span>
            ))}
          </div>

          {/* Executable status */}
          <div className="text-xs font-mono">
            {prediction.is_executable ? (
              <span className="text-green-400">EXECUTABLE</span>
            ) : (
              <span className="text-[#555]">NOT EXECUTABLE</span>
            )}
          </div>

          {/* Model version + time */}
          <div className="text-xs font-mono text-[#555]">
            {prediction.model_version} &middot;{' '}
            {new Date(prediction.timestamp).toLocaleTimeString()}
          </div>
        </div>
      ) : (
        <div className="text-xs text-[#555] font-mono">No predictions yet</div>
      )}
    </div>
  );
}

function DirectionBadge({
  direction,
  executable,
}: {
  direction: string;
  executable: boolean;
}) {
  const isLong = direction === 'long';
  const bg = executable
    ? isLong
      ? 'bg-green-500/20 text-green-400 border-green-500/30'
      : 'bg-red-500/20 text-red-400 border-red-500/30'
    : 'bg-[#1e1e2f] text-[#666] border-[#2a2a3d]';

  return (
    <span className={`px-2 py-0.5 text-xs font-mono font-bold rounded border ${bg}`}>
      {direction.toUpperCase()}
    </span>
  );
}
