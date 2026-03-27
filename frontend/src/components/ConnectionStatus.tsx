import { useDashboardStore } from '../store/dashboardStore';

const DOT_STYLES = {
  connected: 'bg-green-500',
  disconnected: 'bg-red-500',
  connecting: 'bg-yellow-500 animate-pulse',
} as const;

export function ConnectionStatus() {
  const wsStatus = useDashboardStore((s) => s.wsStatus);
  const wsReconnectAttempt = useDashboardStore((s) => s.wsReconnectAttempt);
  const dataStatus = useDashboardStore((s) => s.dataStatus);

  return (
    <div className="flex items-center gap-3 px-3 py-2 border-b border-[#1e1e2f]">
      <StatusDot label="WS" status={wsStatus} />
      <StatusDot label="Data" status={dataStatus} />
      {wsStatus === 'connecting' && wsReconnectAttempt > 0 && (
        <span className="text-xs font-mono text-yellow-400">
          Reconnecting... (attempt {wsReconnectAttempt})
        </span>
      )}
    </div>
  );
}

function StatusDot({ label, status }: { label: string; status: string }) {
  const style = DOT_STYLES[status as keyof typeof DOT_STYLES] ?? DOT_STYLES.disconnected;
  return (
    <div className="flex items-center gap-1.5 text-xs font-mono text-[#888]">
      <span className={`w-2 h-2 rounded-full ${style}`} />
      {label}: {status}
    </div>
  );
}
