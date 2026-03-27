/**
 * Runtime configuration — centralized API and WebSocket URLs.
 *
 * In development, Vite reads from `.env` (localhost defaults).
 * In production, both are empty so the app uses relative paths
 * and auto-detects the WebSocket protocol from the current page.
 */

export const API_BASE: string = import.meta.env.VITE_API_BASE ?? '';

export const WS_URL: string =
  import.meta.env.VITE_WS_URL ??
  `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`;
