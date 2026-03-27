import { useState, useEffect, useRef } from 'react';
import { TradingChart } from './TradingChart';
import { TimeframeSelector } from './TimeframeSelector';
import { ReplayControlBar } from './ReplayControlBar';
import { ConnectionStatus } from './ConnectionStatus';
import { PriceHeader } from './PriceHeader';
import { AccountControls } from './AccountControls';
import { PositionsPanel } from './PositionsPanel';
import { PredictionPanel } from './PredictionPanel';
import { ObservationPanel } from './ObservationPanel';
import { ActiveLevelsPanel } from './ActiveLevelsPanel';
import { SessionStatsPanel } from './SessionStatsPanel';
import { useDashboardStore } from '../store/dashboardStore';

const MIN_SIDEBAR_WIDTH = 250;
const MAX_SIDEBAR_WIDTH = 500;
const DEFAULT_SIDEBAR_WIDTH = 300;
const SIDEBAR_WIDTH_KEY = 'backtesting-sidebar-width';

type SidebarTab = 'live' | 'economic';

export function BacktestingTab() {
  const [timeframe, setTimeframe] = useState('5m');
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>('live');
  const replayGeneration = useDashboardStore((s) => s.replayGeneration);

  // Resizable sidebar
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const saved = localStorage.getItem(SIDEBAR_WIDTH_KEY);
    return saved ? parseInt(saved, 10) : DEFAULT_SIDEBAR_WIDTH;
  });
  const [isResizing, setIsResizing] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // Save sidebar width to localStorage
  useEffect(() => {
    localStorage.setItem(SIDEBAR_WIDTH_KEY, sidebarWidth.toString());
  }, [sidebarWidth]);

  // Handle divider drag
  const handleMouseDown = () => {
    setIsResizing(true);
  };

  useEffect(() => {
    if (!isResizing) return;

    const handleMouseMove = (e: MouseEvent) => {
      if (!containerRef.current) return;
      const containerRect = containerRef.current.getBoundingClientRect();
      const newWidth = containerRect.right - e.clientX;
      const clampedWidth = Math.max(MIN_SIDEBAR_WIDTH, Math.min(MAX_SIDEBAR_WIDTH, newWidth));
      setSidebarWidth(clampedWidth);
    };

    const handleMouseUp = () => {
      setIsResizing(false);
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };
  }, [isResizing]);

  return (
    <div ref={containerRef} className="flex flex-1 min-h-0 relative">
      {/* ── Left: Chart (flexible width) ─────────────────────── */}
      <div
        className="flex flex-col"
        style={{ width: `calc(100% - ${sidebarWidth}px)` }}
      >
        <ReplayControlBar />
        <TimeframeSelector active={timeframe} onTimeframeChange={setTimeframe} />
        <div className="flex-1 min-h-0">
          <TradingChart key={`replay-${replayGeneration}`} timeframe={timeframe} />
        </div>
      </div>

      {/* ── Draggable Divider ────────────────────────────────── */}
      <div
        className="w-1 bg-[#1e1e2f] hover:bg-[#2a2a3d] cursor-col-resize flex-shrink-0 transition-colors"
        onMouseDown={handleMouseDown}
        style={{
          cursor: isResizing ? 'col-resize' : 'col-resize',
          backgroundColor: isResizing ? '#2a2a3d' : undefined,
        }}
      />

      {/* ── Right: Sidebar (resizable) ───────────────────────── */}
      <div
        className="flex flex-col flex-shrink-0"
        style={{ width: `${sidebarWidth}px` }}
      >
        {/* Sub-tab toggle */}
        <div className="flex border-b border-[#1e1e2f] bg-[#0a0a14]">
          <button
            onClick={() => setSidebarTab('live')}
            className={`flex-1 px-4 py-2 text-xs font-mono transition-colors border-b-2 ${
              sidebarTab === 'live'
                ? 'text-white border-blue-500'
                : 'text-[#666] border-transparent hover:text-[#aaa]'
            }`}
          >
            Live Panels
          </button>
          <button
            onClick={() => setSidebarTab('economic')}
            className={`flex-1 px-4 py-2 text-xs font-mono transition-colors border-b-2 ${
              sidebarTab === 'economic'
                ? 'text-white border-blue-500'
                : 'text-[#666] border-transparent hover:text-[#aaa]'
            }`}
          >
            Economic Analysis
          </button>
        </div>

        <div className="flex-1 overflow-y-auto">
          {sidebarTab === 'live' && (
            <>
              <ConnectionStatus />
              <PriceHeader />
              <AccountControls />
              <PositionsPanel />
              <PredictionPanel />
              <ObservationPanel />
              <ActiveLevelsPanel />
              <SessionStatsPanel />
            </>
          )}
          {sidebarTab === 'economic' && (
            <div className="flex items-center justify-center h-full">
              <p className="text-[#666] text-sm font-mono">
                Economic Analysis &mdash; coming in Phase BT-4
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
