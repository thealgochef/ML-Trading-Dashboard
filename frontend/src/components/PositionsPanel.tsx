import { useDashboardStore } from '../store/dashboardStore';

const POINT_VALUE = 20; // NQ: $20 per point

export function PositionsPanel() {
  const openPositions = useDashboardStore((s) => s.openPositions);
  const latestPrice = useDashboardStore((s) => s.latestPrice);

  // Group positions by signal (direction + entry_price) to show ONE card
  const signals = dedupeBySignal(openPositions);

  return (
    <div className="px-3 py-2 border-b border-[#1e1e2f]">
      <h3 className="text-xs font-semibold text-[#666] uppercase tracking-wider mb-2">
        Positions
      </h3>
      {signals.length === 0 ? (
        <div className="text-xs text-[#555] font-mono">No open positions</div>
      ) : (
        <div className="space-y-2">
          {signals.map((sig) => {
            const isLong = sig.direction === 'long';
            const multiplier = isLong ? 1 : -1;
            const pnlPoints =
              latestPrice != null
                ? (latestPrice - sig.entry_price) * multiplier
                : 0;
            const pnlDollars = pnlPoints * POINT_VALUE * sig.totalContracts;
            const pnlPositive = pnlPoints > 0;
            const pnlNegative = pnlPoints < 0;

            return (
              <div
                key={sig.key}
                className="rounded border border-[#2a2a3d] bg-[#12121f] px-2.5 py-2"
              >
                {/* Direction + accounts */}
                <div className="flex items-center justify-between mb-1">
                  <span
                    className={`px-2 py-0.5 text-xs font-mono font-bold rounded border ${
                      isLong
                        ? 'bg-green-500/20 text-green-400 border-green-500/30'
                        : 'bg-red-500/20 text-red-400 border-red-500/30'
                    }`}
                  >
                    {sig.direction.toUpperCase()}
                  </span>
                  <span className="text-xs font-mono text-[#666]">
                    {sig.accountCount} account{sig.accountCount > 1 ? 's' : ''}
                  </span>
                </div>

                {/* Entry + P&L */}
                <div className="grid grid-cols-2 gap-y-1 text-xs font-mono mt-1">
                  <span className="text-[#888]">Entry</span>
                  <span className="text-white text-right tabular-nums">
                    {sig.entry_price.toFixed(2)}
                  </span>

                  <span className="text-[#888]">P&L</span>
                  <span
                    className={`text-right tabular-nums ${
                      pnlPositive
                        ? 'text-green-400'
                        : pnlNegative
                          ? 'text-red-400'
                          : 'text-white'
                    }`}
                  >
                    {pnlPoints >= 0 ? '+' : ''}{pnlPoints.toFixed(1)} pts
                    ({pnlDollars >= 0 ? '+' : ''}${pnlDollars.toFixed(0)})
                  </span>

                  <span className="text-[#888]">TP</span>
                  <span className="text-green-400/70 text-right tabular-nums">
                    {sig.tp_price.toFixed(2)}
                  </span>

                  <span className="text-[#888]">SL</span>
                  <span className="text-red-400/70 text-right tabular-nums">
                    {sig.sl_price.toFixed(2)}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

interface SignalGroup {
  key: string;
  direction: 'long' | 'short';
  entry_price: number;
  tp_price: number;
  sl_price: number;
  accountCount: number;
  totalContracts: number;
}

function dedupeBySignal(
  positions: { direction: string; entry_price: number; tp_price: number; sl_price: number; contracts: number }[],
): SignalGroup[] {
  const map = new Map<string, SignalGroup>();
  for (const pos of positions) {
    const key = `${pos.direction}_${pos.entry_price}`;
    const existing = map.get(key);
    if (existing) {
      existing.accountCount++;
      existing.totalContracts += pos.contracts;
    } else {
      map.set(key, {
        key,
        direction: pos.direction as 'long' | 'short',
        entry_price: pos.entry_price,
        tp_price: pos.tp_price,
        sl_price: pos.sl_price,
        accountCount: 1,
        totalContracts: pos.contracts,
      });
    }
  }
  return [...map.values()];
}
