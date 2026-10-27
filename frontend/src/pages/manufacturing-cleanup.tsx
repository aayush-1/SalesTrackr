import { useEffect, useRef, useState } from 'react';
import { LoaderCircle, RefreshCw, Trash2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { Alert, Empty, Field, Loading, Modal } from '../components';
import { ManufacturingError, manufacturingApi, useManufacturing, useManufacturingResource } from '../manufacturing-api';
import type { ManufacturingCleanupPage, ManufacturingCleanupRequest, ManufacturingCleanupResult } from '../manufacturing-types';
import { formatDate } from '../types';

type Attempt = { request: ManufacturingCleanupRequest; generation: string };
const pending = new Map<string, Attempt>();
const message = (error: unknown) => error instanceof Error ? error.message : 'Cleanup could not be confirmed.';

export function ManufacturingCleanup() {
  const { meta, refresh, workspaceChanged, noteWorkspaceChange } = useManufacturing();
  const identity = useRef(meta).current;
  const key = `${identity.workspaceId}:${identity.generation}`;
  const attempt = useRef<Attempt | null>(pending.get(key) || null);
  const [page, setPage] = useState(0);
  const resource = useManufacturingResource<ManufacturingCleanupPage>(
    `/settings/record-cleanup?limit=50&offset=${page * 50}`, { keepPreviousData: true },
  );
  const data = resource.data;
  const [selected, setSelected] = useState(new Set<string>());
  const [dialogRecords, setDialogRecords] = useState<ManufacturingCleanupRequest['records']>([]);
  const [confirmation, setConfirmation] = useState('');
  const [dialog, setDialog] = useState(false);
  const [busy, setBusy] = useState(false);
  const [unconfirmed, setUnconfirmed] = useState(!!attempt.current);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const inFlight = useRef(false);
  const selectAll = useRef<HTMLInputElement>(null);
  const locked = busy || unconfirmed || workspaceChanged;
  const remember = () => {
    if (attempt.current) pending.set(key, attempt.current); else pending.delete(key);
    setUnconfirmed(!!attempt.current);
  };
  useEffect(() => {
    if (!data || inFlight.current) return;
    if (data.pending && !attempt.current) {
      attempt.current = data.pending; pending.set(key, data.pending); setUnconfirmed(true);
    }
    if (!attempt.current) setSelected(new Set());
  }, [data, key]);
  useEffect(() => {
    if (selectAll.current) selectAll.current.indeterminate = selected.size > 0 && selected.size < (data?.items.length || 0);
  }, [selected, data]);
  useEffect(() => {
    const unload = (event: BeforeUnloadEvent) => {
      if (inFlight.current || attempt.current) { event.preventDefault(); event.returnValue = ''; }
    };
    const navigate = (event: MouseEvent) => {
      const target = event.target instanceof Element
        ? event.target.closest('a[href], [role="tab"], [aria-label="Refresh manufacturing data"]') : null;
      if (target && (inFlight.current || attempt.current)) {
        event.preventDefault(); event.stopImmediatePropagation();
        setError('Keep this page open until cleanup is confirmed. Retry the same cleanup if interrupted.');
      }
    };
    window.addEventListener('beforeunload', unload); document.addEventListener('click', navigate, true);
    return () => { window.removeEventListener('beforeunload', unload); document.removeEventListener('click', navigate, true); };
  }, []);
  const remove = async () => {
    if (inFlight.current || workspaceChanged || !data?.canDelete ||
        (!attempt.current && (confirmation !== 'DELETE' || !dialogRecords.length))) return;
    inFlight.current = true; setBusy(true); setError(''); setNotice('');
    try {
      attempt.current ||= {
        generation: identity.generation,
        request: { writeId: crypto.randomUUID().replaceAll('-', ''), confirmation: 'DELETE', records: dialogRecords },
      };
      remember();
      const current = attempt.current;
      const result = await manufacturingApi<ManufacturingCleanupResult>('/settings/record-cleanup', {
        method: 'POST', body: JSON.stringify(current.request),
      }, { ...identity, generation: current.generation });
      const ids = new Set(current.request.records.map(row => row.id));
      if (result.workspaceId !== identity.workspaceId || result.generation !== current.generation ||
          result.deletedCount !== ids.size || result.deletedIds?.length !== ids.size ||
          new Set(result.deletedIds).size !== ids.size || result.deletedIds.some(id => !ids.has(id)) ||
          result.retryMarkersKept !== ids.size || !result.recoveryBackup?.id ||
          ![result.activityEventsRemoved, result.writeReceiptsRemoved].every(value => Number.isInteger(value) && value >= 0)) {
        throw new ManufacturingError('Cleanup response is unconfirmed. Retry the same cleanup.', 503);
      }
      attempt.current = null; remember(); setSelected(new Set()); setDialog(false); setConfirmation(''); setPage(0);
      setNotice(`Deleted ${result.deletedCount} completed job(s), ${result.activityEventsRemoved} activity event(s) and ${result.writeReceiptsRemoved} action receipt(s). A verified recovery backup was saved. Products, partners, active jobs and numbering were kept.`);
      refresh();
    } catch (reason) {
      setError(message(reason));
      if (reason instanceof ManufacturingError && reason.code === 'workspace_changed') noteWorkspaceChange();
      if (reason instanceof ManufacturingError && reason.status < 500 && reason.status !== 408) {
        try {
          const status = await manufacturingApi<ManufacturingCleanupPage>('/settings/record-cleanup');
          if (status.generation !== identity.generation) noteWorkspaceChange();
          if (!status.pending || status.pending.request.writeId !== attempt.current?.request.writeId) {
            attempt.current = status.pending; setDialog(false); setConfirmation(''); refresh();
          }
        } catch { /* Keep the exact attempt if its server state cannot be checked. */ }
      }
      remember();
    } finally { inFlight.current = false; setBusy(false); }
  };
  return <div className="backup-settings">
    <Alert tone="warning">Local BuildTrackr storage only. SalesTrackr is not changed. Safety backups use your configured backup storage and ten-copy retention.</Alert>
    <section className="panel details-panel settings-section" aria-label="Completed manufacturing record cleanup">
      <h2><Trash2 size={19} />Completed record cleanup</h2>
      <p>Choose completed manufacturing jobs you no longer need. Awaiting-assignment, in-progress and cancelled jobs are protected. Nothing is deleted automatically.</p>
      <p>This frees database storage, not the application's RAM limit. Selected jobs, their copied operation routes, activity history and action receipts are removed. Products, process links, partners, contacts and the numbering counter stay unchanged.</p>
      <p className="help-text">One small anti-replay marker is retained per deleted job, so an old retry cannot recreate it. A full workspace backup is created and verified before deletion. Backups may still contain deleted jobs until the normal ten-backup rotation removes older copies.</p>
      <Alert tone="warning">Permanent deletion has no per-job undo. Recovering a deleted job requires a full workspace backup restore, which also rolls back other BuildTrackr changes.</Alert>
      {(error || resource.error) && <Alert>{error || resource.error}</Alert>}
      {notice && <Alert tone="success">{notice}</Alert>}
      {data?.lastError && !error && <Alert tone="warning">{data.lastError}</Alert>}
      {unconfirmed && <Alert tone="warning">Cleanup is unconfirmed or interrupted. Its exact selection and operation ID are retained. Retry the same cleanup; already removed data will not be counted twice.</Alert>}
      {data && !data.canDelete && <Alert tone="warning">Cleanup requires a recovery administrator.</Alert>}
      {data?.cleanupInProgress && !unconfirmed && <Alert tone="warning">Another cleanup is in progress. Resume it before starting a different cleanup.</Alert>}
      <div className="cleanup-toolbar">
        <label className="cleanup-select-all"><input ref={selectAll} type="checkbox" aria-label="Select all completed jobs on this page"
          checked={!!data?.items.length && selected.size === data.items.length}
          disabled={locked || resource.loading || !data?.canDelete || data.cleanupInProgress || !data.items.length}
          onChange={event => setSelected(new Set(event.target.checked ? data?.items.map(row => row.id) : []))} />
          <span>Select this page ({data?.items.length || 0})</span></label>
        <span className="muted">{selected.size} selected · {data?.total || 0} completed</span>
        <div className="actions">
          <button className="button" disabled={locked || resource.loading} onClick={refresh}><RefreshCw size={16} />Refresh list</button>
          <button className="button danger" disabled={busy || workspaceChanged || resource.loading || !data?.canDelete ||
            (!unconfirmed && (!selected.size || data.cleanupInProgress))}
            onClick={() => {
              setDialogRecords(data?.items.filter(row => selected.has(row.id)).map(row => ({ id: row.id, baseVersion: row.version })) || []);
              setConfirmation(unconfirmed ? 'DELETE' : ''); setDialog(true);
            }}><Trash2 size={16} />{unconfirmed ? 'Retry same cleanup' : `Delete selected${selected.size ? ` (${selected.size})` : ''}`}</button>
        </div>
      </div>
      {resource.loading && !data ? <Loading /> : !!data?.items.length ? <div className="table-scroll"><table className="cleanup-table">
        <thead><tr><th><span className="sr-only">Select</span></th><th>Manufacturing record</th><th>Product</th><th>Progress</th><th>Completed on</th></tr></thead>
        <tbody>{data.items.map(row => <tr key={row.id} className={selected.has(row.id) ? 'cleanup-selected' : ''}>
          <td><input type="checkbox" aria-label={`Select ${row.number}`} checked={selected.has(row.id)}
            disabled={locked || resource.loading || !data.canDelete || data.cleanupInProgress}
            onChange={event => setSelected(old => { const next = new Set(old); if (event.target.checked) next.add(row.id); else next.delete(row.id); return next; })} /></td>
          <td><Link className="record-title" to={`/manufacturing/records/${row.id}`}>{row.number}</Link></td>
          <td><strong>{row.productName}</strong><span className="cell-sub">{row.productReference || 'No Product ID'}</span></td>
          <td>{row.completedCount} / {row.stepCount} operations</td><td>{formatDate(row.completedOn)}</td>
        </tr>)}</tbody>
      </table></div> : !resource.loading && data && <Empty title="Nothing to clean up" text="Completed manufacturing jobs will appear here. Active and cancelled jobs stay protected." />}
      {!!data && data.total > 50 && <div className="cleanup-toolbar"><span className="muted">Page {page + 1} · Up to 50 jobs per cleanup</span><div className="actions">
        <button className="button" disabled={locked || resource.loading || page === 0} onClick={() => setPage(value => value - 1)}>Previous</button>
        <button className="button" disabled={locked || resource.loading || (page + 1) * 50 >= data.total} onClick={() => setPage(value => value + 1)}>Next</button>
      </div></div>}
    </section>
    {dialog && <Modal title="Permanently delete manufacturing records?" onClose={() => { if (!busy) setDialog(false); }}>
      <p>This permanently removes {attempt.current?.request.records.length || dialogRecords.length} selected completed job(s), their operation snapshots, activity history and action receipts.</p>
      <p>A complete backup must be verified first. Products, operations, partners, contacts, process links, active jobs and numbering are not deleted. One small anti-replay marker remains per deleted job.</p>
      <Field label="Type DELETE to confirm permanent deletion"><input autoComplete="off" value={confirmation} disabled={busy || unconfirmed} onChange={event => setConfirmation(event.target.value)} /></Field>
      {error && <Alert>{error}</Alert>}
      {busy && <div className="backup-progress" role="status"><LoaderCircle className="spin" size={18} />Verifying a safety backup and deleting selected jobs. Keep this page open.</div>}
      <div className="actions"><button className="button" disabled={busy} onClick={() => setDialog(false)}>Close</button>
        <button className="button danger" disabled={busy || workspaceChanged || confirmation !== 'DELETE'} onClick={() => void remove()}>
          {busy ? <LoaderCircle className="spin" size={16} /> : <Trash2 size={16} />}{busy ? 'Deleting...' : unconfirmed ? 'Retry same cleanup' : 'Permanently delete records'}
        </button>
      </div>
    </Modal>}
  </div>;
}
