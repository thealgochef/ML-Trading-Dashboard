/**
 * Runtime configuration — centralized API and WebSocket URLs.
 *
 * In development, Vite reads from `.env` (localhost defaults).
 * In production, both are empty so the app uses relative paths
 * and auto-detects the WebSocket protocol from the current page.
 */

export const API_BASE: string = import.meta.env.VITE_API_BASE ?? '';

function resolveWsUrl(): string {
  const explicitWs = import.meta.env.VITE_WS_URL;
  if (explicitWs) return explicitWs;

  // If API_BASE points to a backend origin (common in local dev), default
  // websocket should follow that backend host instead of the Vite host.
  if (API_BASE) {
    try {
      const apiUrl = new URL(API_BASE, window.location.origin);
      const wsProtocol = apiUrl.protocol === 'https:' ? 'wss:' : 'ws:';
      return `${wsProtocol}//${apiUrl.host}/ws`;
    } catch {
      // Fall through to same-origin default for malformed API_BASE.
    }
  }

  return `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`;
}

export const WS_URL: string =
  resolveWsUrl();
