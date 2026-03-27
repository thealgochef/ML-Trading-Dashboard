import { useEffect, useRef, useState } from 'react';
import { API_BASE } from '../config';

// ── Types matching backend GET /api/models ───────────────────────

interface ModelVersion {
  id: number;
  version: string;
  is_active: boolean;
  metrics: Record<string, unknown> | null;
  uploaded_at: string | null;
  activated_at: string | null;
}

interface ModelsResponse {
  active: ModelVersion | null;
  versions: ModelVersion[];
}

// ── Data fetching ────────────────────────────────────────────────

function useModels() {
  const [data, setData] = useState<ModelsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/models`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  return { data, loading, error, reload: load };
}

// ── Active Model Card ────────────────────────────────────────────

function ActiveModelCard({ model }: { model: ModelVersion | null }) {
  if (!model) {
    return (
      <div className="rounded border border-[#1e1e2f] bg-[#0a0a14] p-4 mb-6">
        <h3 className="text-xs font-semibold text-[#666] uppercase tracking-wider mb-2">Active Model</h3>
        <div className="text-sm text-[#555] font-mono">No model loaded</div>
      </div>
    );
  }

  return (
    <div className="rounded border border-green-500/20 bg-[#0a0a14] p-4 mb-6">
      <h3 className="text-xs font-semibold text-[#666] uppercase tracking-wider mb-3">Active Model</h3>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <InfoItem label="Version" value={model.version} />
        <InfoItem label="ID" value={String(model.id)} />
        <InfoItem label="Uploaded" value={model.uploaded_at ? new Date(model.uploaded_at).toLocaleDateString() : '—'} />
        <InfoItem label="Activated" value={model.activated_at ? new Date(model.activated_at).toLocaleDateString() : '—'} />
      </div>
      {model.metrics && Object.keys(model.metrics).length > 0 && (
        <div className="mt-3 pt-3 border-t border-[#1e1e2f]">
          <div className="text-xs text-[#666] font-mono mb-1">Metrics</div>
          <div className="grid grid-cols-3 gap-2">
            {Object.entries(model.metrics).map(([key, val]) => (
              <InfoItem key={key} label={key} value={String(val)} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function InfoItem({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs text-[#666] font-mono">{label}</div>
      <div className="text-sm text-white font-mono tabular-nums">{value}</div>
    </div>
  );
}

// ── Upload Form ──────────────────────────────────────────────────

function UploadForm({ onSuccess }: { onSuccess: () => void }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);

  async function handleUpload() {
    const file = fileRef.current?.files?.[0];
    if (!file) return;

    setLoading(true);
    setMessage(null);
    try {
      const form = new FormData();
      form.append('file', file);
      const res = await fetch(`${API_BASE}/api/models/upload`, {
        method: 'POST',
        body: form,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
        setMessage({ text: err.error ?? `HTTP ${res.status}`, error: true });
      } else {
        setMessage({ text: 'Model uploaded', error: false });
        if (fileRef.current) fileRef.current.value = '';
        onSuccess();
      }
    } catch (err) {
      setMessage({ text: String(err), error: true });
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="rounded border border-[#1e1e2f] bg-[#0a0a14] p-4 mb-6">
      <h3 className="text-xs font-semibold text-[#666] uppercase tracking-wider mb-3">Upload New Model</h3>
      <div className="flex items-end gap-3">
        <div>
          <label className="block text-xs text-[#666] font-mono mb-1">CatBoost Model (.cbm)</label>
          <input
            ref={fileRef}
            type="file"
            accept=".cbm"
            className="text-xs font-mono text-[#888] file:mr-3 file:px-3 file:py-1.5 file:rounded file:border file:border-[#2a2a3d] file:bg-[#1e1e2f] file:text-white file:text-xs file:font-mono file:cursor-pointer"
          />
        </div>
        <button
          onClick={handleUpload}
          disabled={loading}
          className="px-4 py-1.5 text-xs font-mono font-bold rounded bg-blue-500/20 text-blue-400 border border-blue-500/30 hover:bg-blue-500/30 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
        >
          {loading ? 'Uploading...' : 'Upload'}
        </button>
        {message && (
          <span className={`text-xs font-mono ${message.error ? 'text-red-400' : 'text-green-400'}`}>
            {message.text}
          </span>
        )}
      </div>
    </div>
  );
}

// ── Model History Table ──────────────────────────────────────────

function ModelHistory({ versions, onAction }: { versions: ModelVersion[]; onAction: () => void }) {
  const [loading, setLoading] = useState<number | null>(null);

  async function activate(id: number) {
    setLoading(id);
    try {
      const res = await fetch(`${API_BASE}/api/models/${id}/activate`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
        console.error('Activate failed:', err.error);
      }
      onAction();
    } catch (err) {
      console.error('Activate failed:', err);
    } finally {
      setLoading(null);
    }
  }

  if (versions.length === 0) {
    return (
      <div className="text-sm text-[#555] font-mono">No model versions found</div>
    );
  }

  return (
    <div>
      <h3 className="text-xs font-semibold text-[#666] uppercase tracking-wider mb-3">Model History</h3>
      <div className="overflow-x-auto rounded border border-[#1e1e2f]">
        <table className="w-full text-xs font-mono">
          <thead>
            <tr className="bg-[#0a0a14] text-[#888]">
              <th className="px-3 py-2 text-left">ID</th>
              <th className="px-3 py-2 text-left">Version</th>
              <th className="px-3 py-2 text-left">Uploaded</th>
              <th className="px-3 py-2 text-left">Activated</th>
              <th className="px-3 py-2 text-left">Status</th>
              <th className="px-3 py-2 text-left">Action</th>
            </tr>
          </thead>
          <tbody>
            {versions.map((v, i) => (
              <tr
                key={v.id}
                className={i % 2 === 0 ? 'bg-[#0f0f1a]' : 'bg-[#12121f]'}
              >
                <td className="px-3 py-1.5 text-[#aaa]">{v.id}</td>
                <td className="px-3 py-1.5 text-white">{v.version}</td>
                <td className="px-3 py-1.5 text-[#aaa]">
                  {v.uploaded_at ? new Date(v.uploaded_at).toLocaleString() : '—'}
                </td>
                <td className="px-3 py-1.5 text-[#aaa]">
                  {v.activated_at ? new Date(v.activated_at).toLocaleString() : '—'}
                </td>
                <td className="px-3 py-1.5">
                  {v.is_active ? (
                    <span className="px-1.5 py-0.5 rounded bg-green-500/20 text-green-400 text-xs font-bold">
                      ACTIVE
                    </span>
                  ) : (
                    <span className="text-[#555]">inactive</span>
                  )}
                </td>
                <td className="px-3 py-1.5">
                  {v.is_active ? (
                    <span className="text-[#555]">Current</span>
                  ) : (
                    <button
                      onClick={() => activate(v.id)}
                      disabled={loading === v.id}
                      className="px-2 py-0.5 text-xs font-bold rounded bg-blue-500/20 text-blue-400 border border-blue-500/30 hover:bg-blue-500/30 transition-colors disabled:opacity-30"
                    >
                      {loading === v.id ? '...' : 'Activate'}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Tab Component ────────────────────────────────────────────────

export function ModelsTab() {
  const { data, loading, error, reload } = useModels();

  if (loading && !data) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-sm font-mono text-[#555]">Loading models...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-sm font-mono text-red-400">Error: {error}</div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto p-6 space-y-6">
      <ActiveModelCard model={data?.active ?? null} />
      <UploadForm onSuccess={reload} />
      <ModelHistory versions={data?.versions ?? []} onAction={reload} />
    </div>
  );
}
