import { useMemo } from 'react';
import { useDashboardStore } from '../store/dashboardStore';
import { EquityCurve } from './EquityCurve';
import type { ClosedTrade } from '../types';

// ── Types ────────────────────────────────────────────────────────

interface GroupedTrade {
  key: string;
  direction: 'long' | 'short';
  entry_price: number;
  exit_price: number;
  exit_time: string;
  entry_time: string;
  pnl: number;
  pnl_points: number;
  exit_reason: string;
  accountCount: number;
}

// ── Stats computation ────────────────────────────────────────────

interface ComputedStats {
  totalSignals: number;
  totalTrades: number;
  wins: number;
  losses: number;
  winRate: number;
  totalPnl: number;
  totalPnlPoints: number;
  avgWin: number;
  avgLoss: number;
  profitFactor: number;
}

function computeStats(groups: GroupedTrade[]): ComputedStats {
  const wins = groups.filter((g) => g.pnl > 0);
  const losses = groups.filter((g) => g.pnl < 0);
  const grossWins = wins.reduce((s, g) => s + g.pnl, 0);
  const grossLosses = Math.abs(losses.reduce((s, g) => s + g.pnl, 0));

  return {
    totalSignals: groups.length,
    totalTrades: groups.reduce((s, g) => s + g.accountCount, 0),
    wins: wins.length,
    losses: losses.length,
    winRate: groups.length > 0 ? (wins.length / groups.length) * 100 : 0,
    totalPnl: groups.reduce((s, g) => s + g.pnl, 0),
    totalPnlPoints: groups.reduce((s, g) => s + g.pnl_points, 0),
    avgWin: wins.length > 0 ? grossWins / wins.length : 0,
    avgLoss: losses.length > 0 ? grossLosses / losses.length : 0,
    profitFactor: grossLosses > 0 ? grossWins / grossLosses : grossWins > 0 ? Infinity : 0,
  };
}

// ── Group trades by signal ───────────────────────────────────────

function groupTrades(trades: ClosedTrade[]): GroupedTrade[] {
  const map = new Map<string, GroupedTrade>();
  for (const t of trades) {
    const key = `${t.direction}_${t.entry_price}_${t.exit_price}_${t.exit_reason}`;
    const existing = map.get(key);
    if (existing) {
      existing.accountCount++;
      existing.pnl += t.pnl;
    } else {
      map.set(key, {
        key,
        direction: t.direction,
        entry_price: t.entry_price,
        exit_price: t.exit_price,
        exit_time: t.exit_time,
        entry_time: t.entry_time,
        pnl: t.pnl,
        pnl_points: t.pnl_points,
        exit_reason: t.exit_reason,
        accountCount: 1,
      });
    }
  }
  return [...map.values()];
}

// ── Sort helper ──────────────────────────────────────────────────

type SortKey = 'exit_time' | 'direction' | 'entry_price' | 'exit_price' | 'pnl' | 'pnl_points' | 'exit_reason' | 'accountCount';

function sortGroups(groups: GroupedTrade[], sortKey: SortKey, sortAsc: boolean): GroupedTrade[] {
  const sorted = [...groups];
  sorted.sort((a, b) => {
    let cmp = 0;
    if (sortKey === 'exit_time') {
      cmp = a.exit_time.localeCompare(b.exit_time);
    } else if (sortKey === 'direction') {
      cmp = a.direction.localeCompare(b.direction);
    } else if (sortKey === 'exit_reason') {
      cmp = a.exit_reason.localeCompare(b.exit_reason);
    } else {
      cmp = (a[sortKey] as number) - (b[sortKey] as number);
    }
    return sortAsc ? cmp : -cmp;
  });
  return sorted;
}

// ── Component ────────────────────────────────────────────────────

import { useState } from 'react';

export function AnalysisTab() {
  const todaysTrades = useDashboardStore((s) => s.todaysTrades);
  const [sortKey, setSortKey] = useState<SortKey>('exit_time');
  const [sortAsc, setSortAsc] = useState(false);

  const groups = useMemo(() => groupTrades(todaysTrades), [todaysTrades]);
  const stats = useMemo(() => computeStats(groups), [groups]);
  const sorted = useMemo(() => sortGroups(groups, sortKey, sortAsc), [groups, sortKey, sortAsc]);

  // Equity curve data: cumulative P&L per signal in chronological order
  const equityData = useMemo(() => {
    const chronological = [...groups].sort((a, b) => a.exit_time.localeCompare(b.exit_time));
    let cum = 0;
    return chronological.map((g) => {
      cum += g.pnl_points;
      return { time: g.exit_time, value: cum };
    });
  }, [groups]);

  function handleSort(key: SortKey) {
    if (sortKey === key) {
      setSortAsc(!sortAsc);
    } else {
      setSortKey(key);
      setSortAsc(false);
    }
  }

  const arrow = (key: SortKey) =>
    sortKey === key ? (sortAsc ? ' \u25B2' : ' \u25BC') : '';

  return (
    <div className="flex-1 overflow-y-auto p-6 space-y-6">
      {/* Stats cards */}
      <div className="grid grid-cols-4 gap-3">
        <StatCard label="Total Signals" value={stats.totalSignals} />
        <StatCard label="Win Rate" value={`${stats.winRate.toFixed(1)}%`} color={stats.winRate >= 50 ? 'text-green-400' : 'text-red-400'} />
        <StatCard label="Total P&L" value={`$${stats.totalPnl.toFixed(0)}`} color={stats.totalPnl >= 0 ? 'text-green-400' : 'text-red-400'} />
        <StatCard label="P&L (pts)" value={stats.totalPnlPoints.toFixed(1)} color={stats.totalPnlPoints >= 0 ? 'text-green-400' : 'text-red-400'} />
        <StatCard label="Avg Win" value={`$${stats.avgWin.toFixed(0)}`} color="text-green-400" />
        <StatCard label="Avg Loss" value={`$${stats.avgLoss.toFixed(0)}`} color="text-red-400" />
        <StatCard label="Profit Factor" value={stats.profitFactor === Infinity ? '\u221E' : stats.profitFactor.toFixed(2)} />
        <StatCard label="Total Trades" value={stats.totalTrades} />
      </div>

      {/* Equity curve */}
      {equityData.length > 1 && (
        <div>
          <h3 className="text-xs font-semibold text-[#666] uppercase tracking-wider mb-2">
            Equity Curve (Cumulative Points)
          </h3>
          <div className="h-48 rounded border border-[#1e1e2f] bg-[#0a0a14]">
            <EquityCurve data={equityData} />
          </div>
        </div>
      )}

      {/* Trade history table */}
      <div>
        <h3 className="text-xs font-semibold text-[#666] uppercase tracking-wider mb-2">
          Trade History
        </h3>
        {sorted.length === 0 ? (
          <div className="text-sm text-[#555] font-mono py-4">No trades today</div>
        ) : (
          <div className="overflow-x-auto rounded border border-[#1e1e2f]">
            <table className="w-full text-xs font-mono">
              <thead>
                <tr className="bg-[#0a0a14] text-[#888]">
                  <Th onClick={() => handleSort('exit_time')}>Time{arrow('exit_time')}</Th>
                  <Th onClick={() => handleSort('direction')}>Direction{arrow('direction')}</Th>
                  <Th onClick={() => handleSort('entry_price')}>Entry{arrow('entry_price')}</Th>
                  <Th onClick={() => handleSort('exit_price')}>Exit{arrow('exit_price')}</Th>
                  <Th onClick={() => handleSort('pnl')}>P&L{arrow('pnl')}</Th>
                  <Th onClick={() => handleSort('pnl_points')}>Points{arrow('pnl_points')}</Th>
                  <Th onClick={() => handleSort('exit_reason')}>Reason{arrow('exit_reason')}</Th>
                  <Th onClick={() => handleSort('accountCount')}>Accounts{arrow('accountCount')}</Th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((g, i) => (
                  <tr
                    key={g.key}
                    className={i % 2 === 0 ? 'bg-[#0f0f1a]' : 'bg-[#12121f]'}
                  >
                    <td className="px-3 py-1.5 text-[#aaa]">
                      {g.exit_time ? new Date(g.exit_time).toLocaleTimeString() : '—'}
                    </td>
                    <td className="px-3 py-1.5">
                      <span
                        className={`px-1.5 py-0.5 rounded text-xs font-bold ${
                          g.direction === 'long'
                            ? 'bg-green-500/20 text-green-400'
                            : 'bg-red-500/20 text-red-400'
                        }`}
                      >
                        {g.direction.toUpperCase()}
                      </span>
                    </td>
                    <td className="px-3 py-1.5 text-white tabular-nums">{g.entry_price.toFixed(2)}</td>
                    <td className="px-3 py-1.5 text-white tabular-nums">{g.exit_price.toFixed(2)}</td>
                    <td className={`px-3 py-1.5 tabular-nums ${g.pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {g.pnl >= 0 ? '+' : ''}${g.pnl.toFixed(0)}
                    </td>
                    <td className={`px-3 py-1.5 tabular-nums ${g.pnl_points >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {g.pnl_points >= 0 ? '+' : ''}{g.pnl_points.toFixed(1)}
                    </td>
                    <td className="px-3 py-1.5">
                      <ReasonBadge reason={g.exit_reason} />
                    </td>
                    <td className="px-3 py-1.5 text-[#888] text-center">{g.accountCount}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Sub-components ───────────────────────────────────────────────

function StatCard({
  label,
  value,
  color = 'text-white',
}: {
  label: string;
  value: string | number;
  color?: string;
}) {
  return (
    <div className="rounded border border-[#1e1e2f] bg-[#0a0a14] px-4 py-3">
      <div className="text-xs text-[#666] font-mono mb-1">{label}</div>
      <div className={`text-xl font-mono font-bold tabular-nums ${color}`}>{value}</div>
    </div>
  );
}

function Th({ children, onClick }: { children: React.ReactNode; onClick: () => void }) {
  return (
    <th
      onClick={onClick}
      className="px-3 py-2 text-left cursor-pointer select-none hover:text-white transition-colors"
    >
      {children}
    </th>
  );
}

function ReasonBadge({ reason }: { reason: string }) {
  const r = reason.toLowerCase();
  let cls = 'bg-[#1e1e2f] text-[#888]';
  if (r === 'tp') cls = 'bg-green-500/20 text-green-400';
  else if (r === 'sl') cls = 'bg-red-500/20 text-red-400';
  else if (r === 'flatten') cls = 'bg-yellow-500/20 text-yellow-400';

  return (
    <span className={`px-1.5 py-0.5 rounded text-xs font-bold uppercase ${cls}`}>
      {reason}
    </span>
  );
}
