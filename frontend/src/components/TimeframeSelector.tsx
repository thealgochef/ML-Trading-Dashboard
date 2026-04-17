const TIMEFRAMES = ['147t', '987t', '2000t', '1m', '5m', '15m', '1H'] as const;

const LABELS: Record<string, string> = {
  '147t': '147T',
  '987t': '987T',
  '2000t': '2000T',
  '1m': '1m',
  '5m': '5m',
  '15m': '15m',
  '1H': '1H',
};

interface TimeframeSelectorProps {
  active: string;
  onTimeframeChange: (tf: string) => void;
}

export function TimeframeSelector({ active, onTimeframeChange }: TimeframeSelectorProps) {
  return (
    <div className="flex items-center gap-1 px-3 py-2 bg-[#0f0f1a]">
      {TIMEFRAMES.map((tf) => (
        <button
          key={tf}
          onClick={() => onTimeframeChange(tf)}
          className={`px-3 py-1 text-sm font-mono rounded transition-colors ${
            active === tf
              ? 'bg-[#1e1e3a] text-white border border-[#3a3a5c]'
              : 'text-[#888] hover:text-white hover:bg-[#1a1a2e]'
          }`}
        >
          {LABELS[tf]}
        </button>
      ))}
    </div>
  );
}
