export type TabId = 'trading' | 'analysis' | 'accounts' | 'models' | 'backtesting';

const TABS: { id: TabId; label: string }[] = [
  { id: 'trading', label: 'Trading' },
  { id: 'analysis', label: 'Analysis' },
  { id: 'accounts', label: 'Accounts' },
  { id: 'models', label: 'Models' },
  { id: 'backtesting', label: 'Backtesting' },
];

interface TabNavigationProps {
  active: TabId;
  onTabChange: (tab: TabId) => void;
  replayMode?: boolean;
}

export function TabNavigation({ active, onTabChange }: TabNavigationProps) {
  const visibleTabs = TABS;
  return (
    <nav className="flex items-center gap-0 border-b border-[#1e1e2f] bg-[#0a0a14]">
      {visibleTabs.map((tab) => (
        <button
          key={tab.id}
          onClick={() => onTabChange(tab.id)}
          className={`px-5 py-2 text-sm font-mono transition-colors border-b-2 ${
            active === tab.id
              ? 'text-white border-blue-500'
              : 'text-[#666] border-transparent hover:text-[#aaa] hover:border-[#333]'
          }`}
        >
          {tab.label}
        </button>
      ))}
    </nav>
  );
}
