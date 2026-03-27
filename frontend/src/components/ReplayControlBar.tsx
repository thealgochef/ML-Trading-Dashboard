import { useState, useEffect, useRef } from 'react';
import { API_BASE } from '../config';
import { useDashboardStore } from '../store/dashboardStore';

interface ReplayStatus {
  status: string;
  replay_mode: boolean;
  pipeline_running: boolean;
  current_date?: string;
  current_timestamp?: string;
  replay_complete?: boolean;
  paused?: boolean;
  speed?: number;
  step_mode?: boolean;
  preloading?: boolean;
  tick_count?: number;
  prediction_count?: number;
  trade_count?: number;
}

export function ReplayControlBar() {
  const [startDate, setStartDate] = useState('2025-07-07');
  const [endDate, setEndDate] = useState('2025-07-11');
  const [speed, setSpeed] = useState(10);
  const [status, setStatus] = useState<ReplayStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const [hasPlayed, setHasPlayed] = useState(false);
  const speedRef = useRef(speed);
  speedRef.current = speed;

  // Poll replay status every 1s
  useEffect(() => {
    const poll = async () => {
      try {
        const resp = await fetch(`${API_BASE}/api/replay/status`);
        if (resp.ok) setStatus(await resp.json());
      } catch {
        /* ignore network errors */
      }
    };
    poll();
    const id = window.setInterval(poll, 1000);
    return () => window.clearInterval(id);
  }, []);

  const handleStart = async () => {
    setStarting(true);
    setHasPlayed(false);
    useDashboardStore.getState().resetForReplay();
    try {
      await fetch(`${API_BASE}/api/replay/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start_date: startDate, end_date: endDate, speed }),
      });
    } catch (e) {
      console.error('replay start failed:', e);
    }
    setStarting(false);
  };

  const handlePlayPause = async () => {
    const action = status?.paused !== false ? 'play' : 'pause';
    if (action === 'play') setHasPlayed(true);
    try {
      await fetch(`${API_BASE}/api/replay/control`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      });
    } catch {
      /* ignore */
    }
  };

  const handleSpeedInput = (newSpeed: number) => {
    setSpeed(newSpeed);
    speedRef.current = newSpeed;
  };

  const commitSpeed = () => {
    if (status?.pipeline_running) {
      fetch(`${API_BASE}/api/replay/control`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'set_speed', speed: speedRef.current }),
      }).catch(() => {});
    }
  };

  // Derived state
  const isIdle = !status || status.status === 'idle';
  const isRunning = !!status?.pipeline_running && !status?.replay_complete;
  const isPaused = !!status?.paused;
  const isComplete = !!status?.replay_complete;
  const isPreloading = !!status?.preloading;
  const isPlaying = isRunning && !isPaused && !isPreloading;

  // Status label + color
  let statusLabel = 'Idle';
  let statusColor = 'text-[#666]';
  if (starting || isPreloading) {
    statusLabel = 'Loading...';
    statusColor = 'text-yellow-400';
  } else if (isComplete) {
    statusLabel = 'Replay complete';
    statusColor = 'text-blue-400';
  } else if (isPlaying) {
    statusLabel = 'Playing';
    statusColor = 'text-green-400';
  } else if (isPaused && isRunning && !hasPlayed) {
    statusLabel = 'Ready \u2014 click Play';
    statusColor = 'text-cyan-400';
  } else if (isPaused && isRunning) {
    statusLabel = 'Paused';
    statusColor = 'text-yellow-400';
  }

  // Format replay timestamp to ET
  const replayTime = status?.current_timestamp
    ? new Date(status.current_timestamp).toLocaleString('en-US', {
        timeZone: 'America/New_York',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      }) + ' ET'
    : null;

  // Day progress
  const dayProgress = (() => {
    if (!status?.current_date || !startDate || !endDate || isPreloading) return null;
    const s = new Date(startDate + 'T00:00:00');
    const e = new Date(endDate + 'T00:00:00');
    const c = new Date(status.current_date + 'T00:00:00');
    const totalDays = Math.round((e.getTime() - s.getTime()) / 86400000) + 1;
    const currentDay = Math.round((c.getTime() - s.getTime()) / 86400000) + 1;
    return `Day ${Math.max(1, Math.min(currentDay, totalDays))} of ${totalDays}`;
  })();

  const disableInputs = isRunning && !isComplete;
  const showPlayPause = !isIdle;

  return (
    <div className="flex items-center gap-3 px-3 py-2 bg-[#0a0a14] border-b border-[#1e1e2f] font-mono text-sm">
      {/* Date inputs */}
      <label className="flex items-center gap-1 text-[#888]">
        <span className="text-xs">Start</span>
        <input
          type="date"
          value={startDate}
          onChange={(e) => setStartDate(e.target.value)}
          disabled={disableInputs}
          className="bg-[#1a1a2e] border border-[#2a2a4a] rounded px-2 py-1 text-white text-xs
                     disabled:opacity-50 disabled:cursor-not-allowed
                     [color-scheme:dark]"
        />
      </label>
      <label className="flex items-center gap-1 text-[#888]">
        <span className="text-xs">End</span>
        <input
          type="date"
          value={endDate}
          onChange={(e) => setEndDate(e.target.value)}
          disabled={disableInputs}
          className="bg-[#1a1a2e] border border-[#2a2a4a] rounded px-2 py-1 text-white text-xs
                     disabled:opacity-50 disabled:cursor-not-allowed
                     [color-scheme:dark]"
        />
      </label>

      {/* Start Replay */}
      <button
        onClick={handleStart}
        disabled={disableInputs || starting}
        className="px-3 py-1 rounded text-xs font-bold bg-green-600 hover:bg-green-500 text-white
                   disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
      >
        {starting ? 'Starting...' : 'Start Replay'}
      </button>

      <div className="w-px h-5 bg-[#2a2a4a]" />

      {/* Play / Pause */}
      {showPlayPause && (
        <button
          onClick={handlePlayPause}
          disabled={isComplete || isPreloading}
          className="px-3 py-1 rounded text-xs font-bold bg-[#1e1e3a] hover:bg-[#2a2a4a] text-white
                     border border-[#3a3a5c] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {isPlaying ? '\u23F8 Pause' : '\u25B6 Play'}
        </button>
      )}

      {/* Speed slider */}
      <div className="flex items-center gap-2">
        <span className="text-[#666] text-xs">Speed</span>
        <input
          type="range"
          min={1}
          max={100}
          value={speed}
          onChange={(e) => handleSpeedInput(Number(e.target.value))}
          onPointerUp={commitSpeed}
          className="w-20 accent-blue-500"
        />
        <span className="text-white text-xs w-8">{speed}x</span>
      </div>

      <div className="w-px h-5 bg-[#2a2a4a]" />

      {/* Status label */}
      <span className={`text-xs font-bold ${statusColor}`}>{statusLabel}</span>

      {/* Replay time */}
      {replayTime && !isIdle && (
        <>
          <div className="w-px h-5 bg-[#2a2a4a]" />
          <span className="text-[#aaa] text-xs">{replayTime}</span>
        </>
      )}

      {/* Day progress */}
      {dayProgress && (
        <>
          <div className="w-px h-5 bg-[#2a2a4a]" />
          <span className="text-[#888] text-xs">{dayProgress}</span>
        </>
      )}
    </div>
  );
}
