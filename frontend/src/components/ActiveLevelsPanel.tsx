import { useDashboardStore } from '../store/dashboardStore';
import type { LevelType } from '../types';

const LEVEL_COLORS: Record<LevelType, string> = {
  pdh: 'text-orange-400',
  pdl: 'text-orange-400',
  asia_high: 'text-purple-400',
  asia_low: 'text-purple-400',
  london_high: 'text-blue-400',
  london_low: 'text-blue-400',
  manual: 'text-gray-400',
};

const LEVEL_LABELS: Record<LevelType, string> = {
  pdh: 'PDH',
  pdl: 'PDL',
  asia_high: 'Asia H',
  asia_low: 'Asia L',
  london_high: 'Lon H',
  london_low: 'Lon L',
  manual: 'Manual',
};

export function ActiveLevelsPanel() {
  const levels = useDashboardStore((s) => s.levels);

  // Flatten zones → individual levels for display
  const flatLevels = levels.flatMap((zone) =>
    zone.levels.map((lvl) => ({
      ...lvl,
      zone_id: zone.zone_id,
      side: zone.side,
      is_touched: zone.is_touched,
      zone_price: zone.price,
    })),
  );

  // Sort by price descending (highs at top)
  flatLevels.sort((a, b) => b.price - a.price);

  return (
    <div className="px-3 py-2 border-b border-[#1e1e2f]">
      <h3 className="text-xs font-semibold text-[#666] uppercase tracking-wider mb-2">
        Key Levels
      </h3>
      {flatLevels.length === 0 ? (
        <div className="text-xs text-[#555] font-mono">No levels computed</div>
      ) : (
        <div className="space-y-1">
          {flatLevels.map((lvl, i) => (
            <div
              key={`${lvl.zone_id}-${lvl.type}-${i}`}
              className="flex items-center justify-between text-xs font-mono"
            >
              <span className={LEVEL_COLORS[lvl.type] ?? 'text-gray-400'}>
                {LEVEL_LABELS[lvl.type] ?? lvl.type}
                {lvl.is_touched && <span className="ml-1 text-[#555]">(T)</span>}
              </span>
              <span className="text-white tabular-nums">
                {lvl.price.toFixed(2)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
