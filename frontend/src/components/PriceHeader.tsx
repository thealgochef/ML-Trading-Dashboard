import { useDashboardStore } from '../store/dashboardStore';

export function PriceHeader() {
  const price = useDashboardStore((s) => s.latestPrice);
  const bid = useDashboardStore((s) => s.latestBid);
  const ask = useDashboardStore((s) => s.latestAsk);

  return (
    <div className="px-3 py-3 border-b border-[#1e1e2f]">
      <div className="text-2xl font-mono font-bold text-white tabular-nums">
        {price != null ? price.toFixed(2) : '—'}
      </div>
      <div className="flex gap-3 mt-1 text-xs font-mono text-[#888]">
        <span>Bid: {bid != null ? bid.toFixed(2) : '—'}</span>
        <span>Ask: {ask != null ? ask.toFixed(2) : '—'}</span>
        {bid != null && ask != null && (
          <span>Spread: {(ask - bid).toFixed(2)}</span>
        )}
      </div>
    </div>
  );
}
