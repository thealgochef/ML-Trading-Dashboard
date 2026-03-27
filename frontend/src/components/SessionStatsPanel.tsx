import { useDashboardStore } from '../store/dashboardStore';

export function SessionStatsPanel() {
  const stats = useDashboardStore((s) => s.sessionStats);
  const todaysTrades = useDashboardStore((s) => s.todaysTrades);
  const totalPnl = stats.total_pnl ?? 0;

  return (
    <div className="px-3 py-2">
      <h3 className="text-xs font-semibold text-[#666] uppercase tracking-wider mb-2">
        Session Stats
      </h3>
      <div className="grid grid-cols-2 gap-y-1.5 gap-x-4 text-xs font-mono">
        <StatRow label="Signals" value={stats.signals_fired} />
        <StatRow label="Accuracy" value={`${(stats.accuracy * 100).toFixed(0)}%`} />
        <StatRow
          label="W / L"
          value={`${stats.wins} / ${stats.losses}`}
          valueClass={stats.wins > stats.losses ? 'text-green-400' : stats.losses > stats.wins ? 'text-red-400' : 'text-white'}
        />
        <StatRow
          label="PnL"
          value={`$${totalPnl.toFixed(0)}`}
          valueClass={totalPnl > 0 ? 'text-green-400' : totalPnl < 0 ? 'text-red-400' : 'text-white'}
        />
        <StatRow label="Trades" value={todaysTrades.length} />
      </div>
    </div>
  );
}

function StatRow({
  label,
  value,
  valueClass = 'text-white',
}: {
  label: string;
  value: string | number;
  valueClass?: string;
}) {
  return (
    <>
      <span className="text-[#888]">{label}</span>
      <span className={`text-right tabular-nums ${valueClass}`}>{value}</span>
    </>
  );
}
