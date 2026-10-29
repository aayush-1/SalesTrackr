import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import type { Kind, Page, Revision, Save, WorkspaceStorage } from './types';

export const WorkspaceContext = createContext<WorkspaceStorage | null>(null);
let observedWorkspace: WorkspaceStorage | null = null;
export function generationHeaders(workspace: WorkspaceStorage | null): Record<string, string> {
  if (!workspace || !observedWorkspace) throw new ApiError('Workspace identity is unknown. Refresh this page before saving.', 409, 'generation_unknown');
  if (workspace.storageBackend === 'firestore' || observedWorkspace.storageBackend === 'firestore') {
    if (workspace.storageBackend !== 'firestore' || !workspace.generation ||
        workspace.generation !== observedWorkspace.generation) {
      throw new ApiError('The workspace has changed or its generation is unknown. Refresh this page and reopen the form before saving.', 409, 'generation_changed');
    }
    return { 'X-SalesTrackr-Generation': workspace.generation };
  }
  return {};
}
export function useFormWorkspace() {
  const workspace = useContext(WorkspaceContext);
  return useRef(workspace).current;
}

export class ApiError extends Error {
  constructor(message: string, public status: number, public code: string) { super(message); }
}
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers({ 'Content-Type': 'application/json', 'X-SalesTrackr-Request': '1' });
  new Headers(options.headers).forEach((value, name) => headers.set(name, value));
  const mutation = !['GET', 'HEAD', 'OPTIONS'].includes((options.method || 'GET').toUpperCase());
  const backupManagement = /^\/(?:backups(?:\/|$)|settings\/backup-recovery(?:\?|$))/.test(path);
  if (mutation && !backupManagement) {
    if (!observedWorkspace || (observedWorkspace.storageBackend === 'firestore' &&
        (!observedWorkspace.generation || headers.get('X-SalesTrackr-Generation') !== observedWorkspace.generation))) {
      throw new ApiError('Workspace generation is unknown or has changed. Refresh this page and reopen the form before saving.', 409, 'generation_changed');
    }
  }
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...options, headers,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError('Connection lost. If saving, retry without changing the form to confirm the same save.', 503, 'network');
  }
  let text: string;
  try { text = await response.text(); }
  catch { throw new ApiError('The response was interrupted. The operation is unconfirmed; retry the same operation.', 503, 'network'); }
  let body;
  try { body = JSON.parse(text); }
  catch { throw new ApiError('The server did not return data. Your sign-in may have expired; refresh this page.', 503, 'response'); }
  if (!body || typeof body !== 'object') throw new ApiError('The server returned an invalid response. The operation is unconfirmed; retry the same operation.', 503, 'response');
  if (!response.ok) throw new ApiError(body.error?.message || 'Request failed.', response.status, body.error?.code || 'request');
  if (path === '/me' || body.storageBackend) {
    if (body.storageBackend === 'firestore') {
      observedWorkspace = { storageBackend: 'firestore', generation: body.generation ?? null };
    } else if (body.storageBackend === 'drive' || path === '/me') {
      observedWorkspace = { storageBackend: 'drive', generation: null };
    }
  }
  return body as T;
}
export const RefreshContext = createContext<{
  version: number; lastSave: Save<unknown> | null; refresh: (saved?: Save<unknown>) => void;
}>({ version: 0, lastSave: null, refresh: () => {} });

export function usePdfDownload(path: string, filename: string) {
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const download = async () => {
    setBusy(true); setError('');
    try {
      const response = await fetch(`/api${path}`, { headers: { 'X-SalesTrackr-Request': '1' } });
      if (!response.ok) {
        const result = await response.json();
        throw new Error(result.error?.message || 'The PDF could not be created.');
      }
      if (!response.headers.get('Content-Type')?.startsWith('application/pdf')) throw new Error('The server did not return a PDF. Refresh and retry.');
      const url = URL.createObjectURL(await response.blob()), anchor = document.createElement('a');
      anchor.href = url; anchor.download = filename; document.body.appendChild(anchor); anchor.click(); anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The PDF could not be downloaded. Try again.');
    } finally { setBusy(false); }
  };
  useEffect(() => { setError(''); }, [path]);
  return { busy, error, download };
}

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
  const workspace = useFormWorkspace();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [errorCode, setErrorCode] = useState('');
  const ticket = useRef<{ key: string; writeId: string; entityId: string } | null>(null);
  const inFlight = useRef(false);
  const save = useCallback(async (path: string, data: unknown, method = 'POST') => {
    if (inFlight.current) return null;
    inFlight.current = true;
    setBusy(true);
    setError('');
    setErrorCode('');
    const key = JSON.stringify({ path, data, baseRevision, method });
    try {
      const headers = generationHeaders(workspace);
      if (ticket.current && ticket.current.key !== key) {
        throw new Error('The previous save is unconfirmed. Restore the form values and retry that save before making changes.');
      }
      if (!ticket.current) {
        const prepared = entityId && typeof globalThis.crypto?.randomUUID === 'function'
          ? { writeId: globalThis.crypto.randomUUID().replaceAll('-', ''), entityId }
          : await api<{ writeId: string; entityId: string }>('/writes/prepare', {
            method: 'POST', headers, body: JSON.stringify({ entityType: kind, entityId: entityId || null }),
          });
        ticket.current = { ...prepared, key };
      }
      const result = await api<Save<T>>(path, {
        method, headers, body: JSON.stringify({
          writeId: ticket.current.writeId, entityId: ticket.current.entityId, baseRevision: baseRevision || null, data,
        }),
      });
      ticket.current = null;
      refresh(result);
      return result;
    } catch (e) {
      if (e instanceof ApiError && e.status < 500) ticket.current = null;
      setError(e instanceof Error ? e.message : 'Save failed.');
      setErrorCode(e instanceof ApiError ? e.code : '');
      return null;
    } finally { inFlight.current = false; setBusy(false); }
  }, [kind, entityId, baseRevision, refresh, workspace]);
  return { save, busy, error, setError, errorCode, unconfirmed: ticket.current !== null };
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
