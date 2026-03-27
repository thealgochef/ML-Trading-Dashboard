import { useState } from 'react';
import { useDashboardStore } from '../store/dashboardStore';
import { API_BASE } from '../config';

export function AccountControls() {
  const hasPositions = useDashboardStore(
    (s) => s.openPositions.length > 0,
  );
  const [loading, setLoading] = useState<string | null>(null);

  async function closeAll() {
    setLoading('close');
    try {
      await fetch(`${API_BASE}/api/trading/close-all`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: 'manual' }),
      });
    } catch (err) {
      console.error('Close all failed:', err);
    } finally {
      setLoading(null);
    }
  }

  async function manualEntry(direction: 'long' | 'short') {
    setLoading(direction);
    try {
      await fetch(`${API_BASE}/api/trading/manual-entry`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ direction }),
      });
    } catch (err) {
      console.error(`Manual ${direction} entry failed:`, err);
    } finally {
      setLoading(null);
    }
  }

  return (
    <div className="flex items-center gap-2 px-3 py-2 border-b border-[#1e1e2f]">
      <button
        onClick={() => manualEntry('long')}
        disabled={hasPositions || loading !== null}
        className="flex-1 px-3 py-1.5 text-xs font-mono font-bold rounded
          bg-green-500/20 text-green-400 border border-green-500/30
          hover:bg-green-500/30 transition-colors
          disabled:opacity-30 disabled:cursor-not-allowed"
      >
        {loading === 'long' ? '...' : 'BUY'}
      </button>
      <button
        onClick={() => manualEntry('short')}
        disabled={hasPositions || loading !== null}
        className="flex-1 px-3 py-1.5 text-xs font-mono font-bold rounded
          bg-red-500/20 text-red-400 border border-red-500/30
          hover:bg-red-500/30 transition-colors
          disabled:opacity-30 disabled:cursor-not-allowed"
      >
        {loading === 'short' ? '...' : 'SELL'}
      </button>
      <button
        onClick={closeAll}
        disabled={!hasPositions || loading !== null}
        className="flex-1 px-3 py-1.5 text-xs font-mono font-bold rounded
          bg-red-600/20 text-red-300 border border-red-600/30
          hover:bg-red-600/30 transition-colors
          disabled:opacity-30 disabled:cursor-not-allowed"
      >
        {loading === 'close' ? '...' : 'CLOSE ALL'}
      </button>
    </div>
  );
}
