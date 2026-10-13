import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import type { Kind, Page, Revision, Save } from './types';

export class ApiError extends Error {
  constructor(message: string, public status: number, public code: string) { super(message); }
}
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...options, headers: { 'Content-Type': 'application/json', 'X-SalesTrackr-Request': '1', ...options.headers },
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError('Connection lost. If saving, retry without changing the form to confirm the same save.', 503, 'network');
  }
  const text = await response.text();
  let body;
  try { body = JSON.parse(text); }
  catch { throw new ApiError('The server did not return data. Your sign-in may have expired; refresh this page.', 503, 'response'); }
  if (!response.ok) throw new ApiError(body.error?.message || 'Request failed.', response.status, body.error?.code || 'request');
  return body as T;
}
export const RefreshContext = createContext({ version: 0, refresh: () => {} });
export function useResource<T>(path: string | null) {
  const { version } = useContext(RefreshContext);
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    if (!path) { setData(null); setLoading(false); return; }
    const controller = new AbortController();
    setLoading(true);
    setError('');
    setData(null);
    api<T>(path, { signal: controller.signal }).then(setData).catch(e => {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : 'Could not load data.');
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [path, version]);
  return { data, error, loading };
}

export function useSave<T>(kind: Kind, entityId?: string, baseRevision?: string) {
  const { refresh } = useContext(RefreshContext);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const ticket = useRef<{ key: string; writeId: string; entityId: string } | null>(null);
  const inFlight = useRef(false);
  const save = useCallback(async (path: string, data: unknown, method = 'POST') => {
    if (inFlight.current) return null;
    inFlight.current = true;
    setBusy(true);
    setError('');
    const key = JSON.stringify({ path, data, baseRevision, method });
    try {
      if (ticket.current && ticket.current.key !== key) {
        throw new Error('The previous save is unconfirmed. Restore the form values and retry that save before making changes.');
      }
      if (!ticket.current) {
        const prepared = await api<{ writeId: string; entityId: string }>('/writes/prepare', {
          method: 'POST', body: JSON.stringify({ entityType: kind, entityId: entityId || null }),
        });
        ticket.current = { ...prepared, key };
      }
      const result = await api<Save<T>>(path, {
        method, body: JSON.stringify({
          writeId: ticket.current.writeId, entityId: ticket.current.entityId, baseRevision: baseRevision || null, data,
        }),
      });
      ticket.current = null;
      refresh();
      return result;
    } catch (e) {
      if (e instanceof ApiError && e.status < 500) ticket.current = null;
      setError(e instanceof Error ? e.message : 'Save failed.');
      return null;
    } finally { inFlight.current = false; setBusy(false); }
  }, [kind, entityId, baseRevision, refresh]);
  return { save, busy, error, setError };
}

export async function allAccounts<T>(signal: AbortSignal): Promise<Revision<T>[]> {
  const items: Revision<T>[] = [];
  let total: number;
  do {
    const page = await api<Page<T>>(`/accounts?offset=${items.length}&limit=200`, { signal });
    items.push(...page.items);
    total = page.total;
    if (!page.items.length) break;
  } while (items.length < total);
  return items;
}
