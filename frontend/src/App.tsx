import { useState, useEffect, useRef } from 'react';
import { TabNavigation } from './components/TabNavigation';
import type { TabId } from './components/TabNavigation';
import { TradingChart } from './components/TradingChart';
import { TimeframeSelector } from './components/TimeframeSelector';
import { ConnectionStatus } from './components/ConnectionStatus';
import { PriceHeader } from './components/PriceHeader';
import { AccountControls } from './components/AccountControls';
import { PositionsPanel } from './components/PositionsPanel';
import { PredictionPanel } from './components/PredictionPanel';
import { ObservationPanel } from './components/ObservationPanel';
import { ActiveLevelsPanel } from './components/ActiveLevelsPanel';
import { SessionStatsPanel } from './components/SessionStatsPanel';
import { AnalysisTab } from './components/AnalysisTab';
import { AccountsTab } from './components/AccountsTab';
import { ModelsTab } from './components/ModelsTab';
import { BacktestingTab } from './components/BacktestingTab';
import { useWebSocket } from './websocket/useWebSocket';
import { useDashboardStore } from './store/dashboardStore';

const MIN_SIDEBAR_WIDTH = 250;
const MAX_SIDEBAR_WIDTH = 500;
const DEFAULT_SIDEBAR_WIDTH = 300;
const SIDEBAR_WIDTH_KEY = 'dashboard-sidebar-width';

function App() {
  const [tab, setTab] = useState<TabId>('trading');
  const [timeframe, setTimeframe] = useState('5m');
  const replayMode = useDashboardStore((s) => s.replayMode);

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

  // Connect WebSocketManager ↔ Zustand store (once at app root)
  useWebSocket();

  return (
    <div className="flex flex-col w-screen h-screen bg-[#0f0f1a] overflow-hidden">
      <TabNavigation active={tab} onTabChange={setTab} replayMode={replayMode} />

      {tab === 'trading' && (
        <div ref={containerRef} className="flex flex-1 min-h-0 relative">
          {/* ── Left: Chart (flexible width) ─────────────────────── */}
          <div
            className="flex flex-col"
            style={{ width: `calc(100% - ${sidebarWidth}px)` }}
          >
            <TimeframeSelector active={timeframe} onTimeframeChange={setTimeframe} />
            <div className="flex-1 min-h-0">
              <TradingChart timeframe={timeframe} />
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
            className="flex flex-col overflow-y-auto flex-shrink-0"
            style={{ width: `${sidebarWidth}px` }}
          >
            <ConnectionStatus />
            <PriceHeader />
            <AccountControls />
            <PositionsPanel />
            <PredictionPanel />
            <ObservationPanel />
            <ActiveLevelsPanel />
            <SessionStatsPanel />
          </div>
        </div>
      )}

      {tab === 'backtesting' && <BacktestingTab />}
      {tab === 'analysis' && <AnalysisTab />}
      {tab === 'accounts' && <AccountsTab />}
      {tab === 'models' && <ModelsTab />}
    </div>
  );
}

export default App;
