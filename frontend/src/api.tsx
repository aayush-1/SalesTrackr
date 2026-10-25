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
export const RefreshContext = createContext<{
  version: number; lastSave: Save<unknown> | null; refresh: (saved?: Save<unknown>) => void;
}>({ version: 0, lastSave: null, refresh: () => {} });

function savedResource<T>(path: string, current: T | null, saved: Save<unknown> | null): T | null {
  if (!current || !saved?.revisionIsCurrent || saved.warning || !saved.complete) return null;
  if (path === '/status') {
    const previous = current as { syncedAt?: string | null };
    if (previous.syncedAt && saved.syncedAt && Date.parse(previous.syncedAt) > Date.parse(saved.syncedAt)) return null;
    return { ...current, syncedAt: saved.syncedAt, conflictCount: saved.conflictCount, complete: saved.complete, error: null };
  }
  const revision = saved.revision;
  if (path !== `/entities/${revision.entityType}/${revision.entityId}` &&
      !(revision.entityType === 'record' && saved.related && path === `/records/${revision.entityId}/detail`)) return null;
  const previous = (current as { revision?: Revision<unknown> }).revision;
  if (!previous || (previous.id !== revision.id && !revision.parents.includes(previous.id))) return null;
  return { ...current, ...saved };
}

export function useResource<T>(path: string | null) {
  const { version, lastSave } = useContext(RefreshContext);
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const resource = useRef<{ path: string | null; data: T | null }>({ path: null, data: null });
  useEffect(() => {
    if (!path) { resource.current = { path, data: null }; setData(null); setLoading(false); return; }
    const updated = savedResource(path, resource.current.path === path ? resource.current.data : null, lastSave);
    if (updated) {
      resource.current = { path, data: updated };
      setData(updated); setError(''); setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError('');
    setData(null);
    resource.current = { path, data: null };
    api<T>(path, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) { resource.current = { path, data: value }; setData(value); }
    }).catch(e => {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : 'Could not load data.');
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [path, version, lastSave]);
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
      refresh(result);
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
