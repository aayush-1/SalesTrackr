import { createContext, useContext, useEffect, useRef, useState } from 'react';
import type { Directory, ManufacturingMeta, ManufacturingPage, Master } from './manufacturing-types';

export const ManufacturingContext = createContext<{
  meta: ManufacturingMeta; version: number; refresh: () => void;
  workspaceChanged: boolean; noteWorkspaceChange: () => void;
} | null>(null);
export function useManufacturing() {
  const value = useContext(ManufacturingContext);
  if (!value) throw new Error('BuildTrackr identity is unavailable.');
  return value;
}
export class ManufacturingError extends Error {
  constructor(message: string, public status: number, public code = '') { super(message); }
}
export async function manufacturingApi<T>(path: string, options: RequestInit = {}, meta?: ManufacturingMeta): Promise<T> {
  const headers = new Headers(options.headers);
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  headers.set('X-SalesTrackr-Request', '1');
  if (meta) {
    headers.set('X-BuildTrackr-Workspace', meta.workspaceId);
    headers.set('X-BuildTrackr-Generation', meta.generation);
  }
  try {
    const response = await fetch(`/api/manufacturing${path}`, { ...options, headers });
    const result = await response.json();
    if (!result || typeof result !== 'object') throw new Error('Invalid response');
    if (!response.ok) throw new ManufacturingError(result.error?.message || 'Request failed.', response.status, result.error?.code);
    return result as T;
  } catch (error) {
    if (error instanceof ManufacturingError || (error instanceof DOMException && error.name === 'AbortError')) throw error;
    throw new ManufacturingError('Connection interrupted. If saving, retry the same change before editing anything else.', 503);
  }
}
function checkIdentity(data: unknown, meta: ManufacturingMeta) {
  const returned = data as Partial<ManufacturingMeta> | null;
  if (returned && ((returned.generation && returned.generation !== meta.generation) ||
      (returned.workspaceId && returned.workspaceId !== meta.workspaceId))) {
    throw new ManufacturingError('BuildTrackr was restored in another session. Reload before continuing.', 409, 'workspace_changed');
  }
}
export function useManufacturingResource<T>(path: string | null, { keepPreviousData = false }: { keepPreviousData?: boolean } = {}) {
  const { version, meta, noteWorkspaceChange } = useManufacturing();
  const [state, setState] = useState<{ data: T | null; error: string; loading: boolean }>({ data: null, error: '', loading: true });
  useEffect(() => {
    if (!path) { setState({ data: null, error: '', loading: false }); return; }
    const controller = new AbortController();
    setState(old => ({ data: keepPreviousData ? old.data : null, error: '', loading: true }));
    manufacturingApi<T>(path, { signal: controller.signal })
      .then(data => { if (!controller.signal.aborted) { checkIdentity(data, meta); setState({ data, error: '', loading: false }); } })
      .catch(error => { if (!controller.signal.aborted) {
        if (error.code === 'workspace_changed') noteWorkspaceChange();
        setState(old => ({ data: keepPreviousData && error.code !== 'workspace_changed' ? old.data : null, error: error.message, loading: false }));
      } });
    return () => controller.abort();
  }, [path, version, keepPreviousData, meta, noteWorkspaceChange]);
  return state;
}
export function useManufacturingDirectory(kind: Directory | null, query = '') {
  const { version, meta, noteWorkspaceChange } = useManufacturing();
  const [state, setState] = useState<{ items: Master[]; loading: boolean; error: string }>({ items: [], loading: !!kind, error: '' });
  useEffect(() => {
    if (!kind) { setState({ items: [], loading: false, error: '' }); return; }
    const controller = new AbortController();
    setState({ items: [], loading: true, error: '' });
    const load = async () => {
      const items: Master[] = [];
      let total = 1;
      while (items.length < total) {
        const result = await manufacturingApi<ManufacturingPage>(`/${kind}?offset=${items.length}&limit=200${query}`, { signal: controller.signal });
        checkIdentity(result, meta);
        if (!result.items.length && items.length < result.total) throw new Error('The directory changed while loading. Refresh and try again.');
        items.push(...result.items); total = result.total;
      }
      if (!controller.signal.aborted) setState({ items, loading: false, error: '' });
    };
    load().catch(error => { if (!controller.signal.aborted) {
      if (error.code === 'workspace_changed') noteWorkspaceChange();
      setState({ items: [], loading: false, error: error.message });
    } });
    return () => controller.abort();
  }, [kind, query, version, meta, noteWorkspaceChange]);
  return state;
}
export function useManufacturingSave({ refreshOnSave = true }: { refreshOnSave?: boolean } = {}) {
  const { meta, refresh, workspaceChanged, noteWorkspaceChange } = useManufacturing();
  const identity = useRef(meta).current;
  const ticket = useRef<{ key: string; path: string; method: string; body: object } | null>(null);
  const inFlight = useRef(false);
  const [state, setState] = useState({ busy: false, error: '', unconfirmed: false });
  const send = async () => {
    if (inFlight.current || !ticket.current) return null;
    inFlight.current = true; setState(old => ({ ...old, busy: true, error: '' }));
    try {
      if (workspaceChanged || identity.workspaceId !== meta.workspaceId || identity.generation !== meta.generation) {
        throw new ManufacturingError('BuildTrackr changed. Reload before saving.', 409);
      }
      const attempt = ticket.current;
      const result = await manufacturingApi<{ id: string }>(attempt.path, { method: attempt.method, body: JSON.stringify(attempt.body) }, identity);
      if (!result.id) throw new ManufacturingError('The save is unconfirmed. Retry the same change.', 503);
      ticket.current = null;
      setState({ busy: false, error: '', unconfirmed: false });
      if (refreshOnSave) refresh();
      return result;
    } catch (error) {
      if (error instanceof ManufacturingError && error.code === 'workspace_changed') noteWorkspaceChange();
      if (error instanceof ManufacturingError && error.status < 500 && error.status !== 408) ticket.current = null;
      setState({ busy: false, error: error instanceof Error ? error.message : 'Save failed.', unconfirmed: !!ticket.current });
      return null;
    } finally { inFlight.current = false; }
  };
  const save = async (path: string, data: object, baseVersion: number | null, id?: string, method = 'POST') => {
    if (inFlight.current) return null;
    const key = JSON.stringify({ path, data, baseVersion, id, method });
    if (ticket.current && ticket.current.key !== key) {
      setState(old => ({ ...old, error: 'The previous change is unconfirmed. Retry that same change before changing values.' })); return null;
    }
    ticket.current ||= { key, path, method, body: { writeId: crypto.randomUUID().replaceAll('-', ''), id: id || crypto.randomUUID().replaceAll('-', ''), baseVersion, data } };
    return send();
  };
  return { ...state, save, retry: send };
}
