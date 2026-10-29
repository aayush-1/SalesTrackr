import { useContext, useEffect, useRef, useState } from 'react';
import { LoaderCircle, RefreshCw, Trash2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { api, ApiError, generationHeaders, RefreshContext } from '../api';
import { Alert, Empty, Field, Loading, Modal } from '../components';
import { formatDate } from '../types';
import type { CleanupRequest, CleanupResult, CleanupSelection, CleanupSettings } from '../types';

type Attempt = {
  request: CleanupRequest; generation: string; remaining: CleanupSelection[];
  deleted: number; versions: number; kept: number;
};
let pending: Attempt | null = null;
const batchSize = 200;
const message = (error: unknown) => error instanceof Error ? error.message : 'Cleanup failed.';
const writeId = () => {
  if (!globalThis.crypto?.randomUUID) throw new Error('Secure operation IDs require HTTPS or localhost.');
  return globalThis.crypto.randomUUID().replaceAll('-', '');
};

export function RecordCleanup({ demo }: { demo: boolean }) {
  const { refresh } = useContext(RefreshContext);
  const [data, setData] = useState<CleanupSettings | null>(null);
  const [selected, setSelected] = useState(new Set<string>());
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [unconfirmed, setUnconfirmed] = useState(!!pending);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [dialog, setDialog] = useState(false);
  const [confirmation, setConfirmation] = useState('');
  const inFlight = useRef(false);
  const selectAll = useRef<HTMLInputElement>(null);
  const locked = busy || unconfirmed;
  const load = async (signal?: AbortSignal) => {
    const value = await api<CleanupSettings>('/settings/record-cleanup', { signal });
    if (signal?.aborted) return;
    setData(value);
    setSelected(previous => new Set([...previous].filter(id => value.items.some(row => row.entityId === id))));
    if (!pending && value.pending && value.generation) {
      pending = { request: value.pending, generation: value.generation, remaining: [], deleted: 0, versions: 0, kept: 0 };
      setUnconfirmed(true);
    }
  };
  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal).catch(error => {
      if (!controller.signal.aborted) setError(message(error));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (selectAll.current) selectAll.current.indeterminate = selected.size > 0 && selected.size < (data?.total || 0);
  }, [selected, data]);
  useEffect(() => {
    const unload = (event: BeforeUnloadEvent) => {
      if (inFlight.current || pending) { event.preventDefault(); event.returnValue = ''; }
    };
    const navigate = (event: MouseEvent) => {
      const anchor = event.target instanceof Element ? event.target.closest('a[href], [role="tab"]') : null;
      if (anchor && (inFlight.current || pending)) {
        event.preventDefault(); event.stopImmediatePropagation();
        setError('Keep this page open until cleanup is confirmed. Retry the same cleanup if interrupted.');
      }
    };
    window.addEventListener('beforeunload', unload);
    document.addEventListener('click', navigate, true);
    return () => { window.removeEventListener('beforeunload', unload); document.removeEventListener('click', navigate, true); };
  }, []);
  const reload = async () => {
    setLoading(true); setError('');
    try { await load(); } catch (error) { setError(message(error)); }
    finally { setLoading(false); }
  };
  const remove = async () => {
    if (inFlight.current || !data?.canDelete || (!pending && (confirmation !== 'DELETE' || !selected.size))) return;
    inFlight.current = true; setBusy(true); setError(''); setNotice('');
    try {
      if (!pending) {
        if (!data.generation) throw new Error('Refresh this page to identify the workspace before deleting.');
        const records = data.items.filter(row => selected.has(row.entityId)).map(({ entityId, baseRevision }) => ({ entityId, baseRevision }));
        pending = {
          request: { writeId: writeId(), records: records.slice(0, batchSize), confirmation: 'DELETE' },
          generation: data.generation, remaining: records.slice(batchSize), deleted: 0, versions: 0, kept: 0,
        };
      }
      while (pending) {
        const attempt: Attempt = pending;
        const result = await api<CleanupResult>('/settings/record-cleanup', {
          method: 'POST', body: JSON.stringify(attempt.request),
          headers: generationHeaders({ storageBackend: 'firestore', generation: attempt.generation }),
        });
        const requested = new Set(attempt.request.records.map(row => row.entityId));
        const returned = [...(result.deletedIds || []), ...(result.kept || []).map(row => row.entityId)];
        if (result.deletedCount !== result.deletedIds?.length || returned.length !== requested.size ||
            new Set(returned).size !== requested.size || returned.some(id => !requested.has(id)) ||
            !Number.isInteger(result.savedVersionsRemoved) || result.savedVersionsRemoved < 0) {
          throw new ApiError('Cleanup response is unconfirmed. Retry the same cleanup.', 503, 'response');
        }
        attempt.deleted += result.deletedCount; attempt.versions += result.savedVersionsRemoved; attempt.kept += result.kept.length;
        if (attempt.remaining.length) {
          pending = {
            ...attempt, request: { writeId: writeId(), records: attempt.remaining.slice(0, batchSize), confirmation: 'DELETE' },
            remaining: attempt.remaining.slice(batchSize),
          };
        } else {
          pending = null;
          setNotice(`Deleted ${attempt.deleted} ${demo ? 'local demo ' : ''}sales record${attempt.deleted === 1 ? '' : 's'} and ${attempt.versions} saved version${attempt.versions === 1 ? '' : 's'}. Accounts, contacts and existing backups were kept.${attempt.kept ? ` ${attempt.kept} changed record(s) were kept safely.` : ''}`);
        }
      }
      setUnconfirmed(false); setDialog(false); setConfirmation(''); setSelected(new Set());
      refresh();
      await load();
    } catch (error) {
      setError(message(error));
      if (error instanceof ApiError && error.status < 500 && !['cleanup_pending', 'maintenance'].includes(error.code)) {
        try {
          const state = await api<CleanupSettings>('/settings/record-cleanup');
          setData(state);
          if (!state.pending) {
            if (pending?.deleted) setNotice(`${pending.deleted} record(s) were deleted before cleanup stopped. Review the remaining list.`);
            pending = null; setDialog(false); setConfirmation(''); setSelected(new Set());
          }
        } catch { /* Retain the exact attempt when its state cannot be checked. */ }
      }
      setUnconfirmed(!!pending);
    } finally { inFlight.current = false; setBusy(false); }
  };
  return <div className="backup-settings">
    {demo && <Alert tone="warning">Local preview only. Cleanup affects synthetic demo records, not production data or Google Drive.</Alert>}
    <section className="panel details-panel settings-section" aria-label="Record cleanup">
      <h2><Trash2 size={19} />Record cleanup</h2>
      <p>Remove sales records you no longer need. Only <strong>closed records with feedback received</strong> and <strong>discarded records</strong> appear here. Active records and closed records awaiting feedback are protected.</p>
      <p className="help-text">Deletes the selected records and their saved history to free database storage, including copies in older Firestore datasets. Accounts, contacts and existing backups are kept. This does not change the Cloud Run memory limit.</p>
      <Alert tone="warning">There is no per-record undo. Create a current backup first if you may need these records again. Recovering deleted records requires a full backup restore.</Alert>
      {error && <Alert>{error}</Alert>}
      {notice && <Alert tone="success">{notice}</Alert>}
      {data?.lastError && !error && <Alert tone="warning">{data.lastError}</Alert>}
      {unconfirmed && <Alert tone="warning">Cleanup is unconfirmed or interrupted. Retry the same cleanup below; do not start a different one. Already deleted records will not be deleted or counted twice.</Alert>}
      {data && !data.canDelete && <Alert tone="warning">Permanent record cleanup requires a recovery administrator and Firestore storage.</Alert>}
      {data?.cleanupInProgress && !data.pending && !unconfirmed && <Alert tone="warning">Another administrator has a cleanup in progress.</Alert>}
      <div className="cleanup-toolbar">
        <label className="cleanup-select-all"><input ref={selectAll} type="checkbox" aria-label="Select all eligible records"
          checked={!!data?.total && selected.size === data.total} disabled={locked || loading || !data?.canDelete || !data.total}
          onChange={event => setSelected(new Set(event.target.checked ? data?.items.map(row => row.entityId) : []))} />
          <span>Select all <span className="muted">({data?.total || 0})</span></span></label>
        <span className="muted">{selected.size} selected</span>
        <div className="actions">
          <button className="button" disabled={busy || loading} onClick={() => void reload()}><RefreshCw size={16} />Refresh list</button>
          <button className="button danger" disabled={busy || loading || !data?.canDelete || (!unconfirmed && (!selected.size || data.cleanupInProgress))}
            onClick={() => { setConfirmation(unconfirmed ? 'DELETE' : ''); setDialog(true); }}>
            <Trash2 size={16} />{unconfirmed ? 'Retry same cleanup' : `Delete selected${selected.size ? ` (${selected.size})` : ''}`}
          </button>
        </div>
      </div>
      {loading && !data ? <Loading /> : !!data?.items.length ? <div className="table-scroll">
        <table className="cleanup-table"><thead><tr><th><span className="sr-only">Select</span></th><th>Company</th><th>Product / requirement</th><th>Eligible because</th><th>Closed on</th></tr></thead>
          <tbody>{data.items.map(row => <tr key={row.entityId} className={selected.has(row.entityId) ? 'cleanup-selected' : ''}>
            <td><input type="checkbox" aria-label={`Select ${row.accountName}, record ${row.entityId.slice(0, 8)}`}
              checked={selected.has(row.entityId)} disabled={locked || loading || !data.canDelete || data.cleanupInProgress}
              onChange={event => setSelected(previous => {
                const next = new Set(previous); if (event.target.checked) next.add(row.entityId); else next.delete(row.entityId); return next;
              })} /></td>
            <td><Link className="record-title" to={`/records/${row.entityId}`}>{row.accountName}</Link><span className="cell-sub">Record {row.entityId.slice(0, 8)}</span></td>
            <td><span className="record-summary" title={row.productNotes}>{row.productNotes || 'No product notes'}</span></td>
            <td><span className={`badge ${row.discarded ? 'discarded' : 'paid'}`}>{row.discarded ? 'Discarded' : 'Closed + feedback received'}</span></td>
            <td>{formatDate(row.closedOn)}</td>
          </tr>)}</tbody></table>
      </div> : !loading && data && <Empty title="Nothing to clean up" text="Completed records will appear here after feedback is received. Discarded records also appear here." />}
    </section>
    {dialog && <Modal title="Permanently delete sales records?" onClose={() => { if (!busy) setDialog(false); }}>
      <p>This removes {pending ? pending.request.records.length + pending.remaining.length : selected.size} selected sales record(s) and their complete saved history, including older Firestore copies. There is no per-record undo.</p>
      <p>Accounts, contacts and existing backup files will not be deleted. A full backup restore can bring these records back.</p>
      <Field label="Type DELETE to confirm permanent deletion"><input value={confirmation} disabled={busy} autoComplete="off"
        onChange={event => setConfirmation(event.target.value)} /></Field>
      {error && <Alert>{error}</Alert>}
      {busy && <div className="backup-progress" role="status"><LoaderCircle className="spin" size={18} />Deleting records and saved history. Keep this page open until confirmed.</div>}
      <div className="actions">
        <button className="button" disabled={busy} onClick={() => setDialog(false)}>Cancel</button>
        <button className="button danger" disabled={busy || confirmation !== 'DELETE'} onClick={() => void remove()}>
          {busy ? <LoaderCircle className="spin" size={16} /> : <Trash2 size={16} />}{busy ? 'Deleting...' : unconfirmed ? 'Retry same cleanup' : 'Permanently delete records'}
        </button>
      </div>
    </Modal>}
  </div>;
}
