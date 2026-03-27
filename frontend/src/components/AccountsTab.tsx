import { useState } from 'react';
import { useDashboardStore } from '../store/dashboardStore';
import { API_BASE } from '../config';

// ── Portfolio Summary ────────────────────────────────────────────

function PortfolioSummary() {
  const accounts = useDashboardStore((s) => s.accounts);

  const total = accounts.length;
  const active = accounts.filter((a) => a.status.toLowerCase() === 'active').length;
  const blown = accounts.filter((a) => a.status.toLowerCase() === 'blown').length;
  const retired = accounts.filter((a) => a.status.toLowerCase() === 'retired').length;
  const totalBalance = accounts.reduce((s, a) => s + a.balance, 0);
  const totalDailyPnl = accounts.reduce((s, a) => s + a.daily_pnl, 0);

  return (
    <div className="grid grid-cols-3 md:grid-cols-6 gap-3 mb-6">
      <SummaryCard label="Total Accounts" value={total} />
      <SummaryCard label="Active" value={active} color="text-green-400" />
      <SummaryCard label="Blown" value={blown} color={blown > 0 ? 'text-red-400' : 'text-[#555]'} />
      <SummaryCard label="Retired" value={retired} color="text-[#888]" />
      <SummaryCard label="Total Balance" value={`$${totalBalance.toFixed(0)}`} />
      <SummaryCard
        label="Daily P&L"
        value={`${totalDailyPnl >= 0 ? '+' : ''}$${totalDailyPnl.toFixed(0)}`}
        color={totalDailyPnl >= 0 ? 'text-green-400' : 'text-red-400'}
      />
    </div>
  );
}

function SummaryCard({ label, value, color = 'text-white' }: { label: string; value: string | number; color?: string }) {
  return (
    <div className="rounded border border-[#1e1e2f] bg-[#0a0a14] px-4 py-3">
      <div className="text-xs text-[#666] font-mono mb-1">{label}</div>
      <div className={`text-xl font-mono font-bold tabular-nums ${color}`}>{value}</div>
    </div>
  );
}

// ── Account Cards ────────────────────────────────────────────────

function AccountCards() {
  const accounts = useDashboardStore((s) => s.accounts);

  if (accounts.length === 0) {
    return <div className="text-sm text-[#555] font-mono">No accounts configured</div>;
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 mb-6">
      {accounts.map((acct) => (
        <AccountCard key={acct.account_id} account={acct} />
      ))}
    </div>
  );
}

function AccountCard({ account: a }: { account: ReturnType<typeof useDashboardStore.getState>['accounts'][number] }) {
  const statusLower = a.status.toLowerCase();
  const statusColor =
    statusLower === 'active'
      ? 'bg-green-500/20 text-green-400 border-green-500/30'
      : statusLower === 'blown'
        ? 'bg-red-500/20 text-red-400 border-red-500/30'
        : 'bg-[#1e1e2f] text-[#888] border-[#2a2a3d]';

  return (
    <div className="rounded border border-[#1e1e2f] bg-[#0a0a14] p-4">
      {/* Header: label + group + status */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="text-sm font-mono font-bold text-white">{a.label}</span>
          <span className="px-1.5 py-0.5 text-xs font-mono rounded bg-blue-500/20 text-blue-400 border border-blue-500/30">
            {a.group}
          </span>
        </div>
        <span className={`px-2 py-0.5 text-xs font-mono font-bold rounded border ${statusColor}`}>
          {a.status}
        </span>
      </div>

      {/* Balance + Profit */}
      <div className="grid grid-cols-2 gap-y-2 text-xs font-mono">
        <span className="text-[#888]">Balance</span>
        <span className="text-right text-white tabular-nums">${a.balance.toFixed(0)}</span>

        <span className="text-[#888]">Daily P&L</span>
        <span className={`text-right tabular-nums ${a.daily_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
          {a.daily_pnl >= 0 ? '+' : ''}${a.daily_pnl.toFixed(0)}
        </span>

        <span className="text-[#888]">Tier</span>
        <span className="text-right text-[#aaa]">{a.tier}</span>

        {a.has_position && (
          <>
            <span className="text-[#888]">Position</span>
            <span className="text-right text-yellow-400">Open</span>
          </>
        )}
      </div>
    </div>
  );
}

// ── Add Account Form ─────────────────────────────────────────────

function AddAccountForm() {
  const [label, setLabel] = useState('');
  const [evalCost, setEvalCost] = useState('');
  const [activationCost, setActivationCost] = useState('');
  const [group, setGroup] = useState<'A' | 'B'>('A');
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!label.trim()) return;

    setLoading(true);
    setMessage(null);
    try {
      const res = await fetch(`${API_BASE}/api/accounts`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          label: label.trim(),
          eval_cost: parseFloat(evalCost) || 0,
          activation_cost: parseFloat(activationCost) || 0,
          group,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
        setMessage({ text: err.error ?? `HTTP ${res.status}`, error: true });
      } else {
        setMessage({ text: 'Account created', error: false });
        setLabel('');
        setEvalCost('');
        setActivationCost('');
      }
    } catch (err) {
      setMessage({ text: String(err), error: true });
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="rounded border border-[#1e1e2f] bg-[#0a0a14] p-4">
      <h3 className="text-xs font-semibold text-[#666] uppercase tracking-wider mb-3">
        Add Account
      </h3>
      <form onSubmit={handleSubmit} className="flex flex-wrap items-end gap-3">
        <Field label="Label" value={label} onChange={setLabel} placeholder="A6" />
        <Field label="Eval Cost" value={evalCost} onChange={setEvalCost} placeholder="0" type="number" />
        <Field label="Activation Cost" value={activationCost} onChange={setActivationCost} placeholder="0" type="number" />
        <div>
          <label className="block text-xs text-[#666] font-mono mb-1">Group</label>
          <select
            value={group}
            onChange={(e) => setGroup(e.target.value as 'A' | 'B')}
            className="px-3 py-1.5 text-xs font-mono rounded bg-[#1e1e2f] text-white border border-[#2a2a3d] focus:border-blue-500 outline-none"
          >
            <option value="A">A</option>
            <option value="B">B</option>
          </select>
        </div>
        <button
          type="submit"
          disabled={loading || !label.trim()}
          className="px-4 py-1.5 text-xs font-mono font-bold rounded bg-blue-500/20 text-blue-400 border border-blue-500/30 hover:bg-blue-500/30 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
        >
          {loading ? '...' : 'Add Account'}
        </button>
        {message && (
          <span className={`text-xs font-mono ${message.error ? 'text-red-400' : 'text-green-400'}`}>
            {message.text}
          </span>
        )}
      </form>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  type = 'text',
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  type?: string;
}) {
  return (
    <div>
      <label className="block text-xs text-[#666] font-mono mb-1">{label}</label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="px-3 py-1.5 text-xs font-mono rounded bg-[#1e1e2f] text-white border border-[#2a2a3d] focus:border-blue-500 outline-none w-28"
      />
    </div>
  );
}

// ── Tab Component ────────────────────────────────────────────────

export function AccountsTab() {
  return (
    <div className="flex-1 overflow-y-auto p-6 space-y-6">
      <PortfolioSummary />
      <div>
        <h3 className="text-xs font-semibold text-[#666] uppercase tracking-wider mb-3">
          Accounts
        </h3>
        <AccountCards />
      </div>
      <AddAccountForm />
    </div>
  );
}
