import { useEffect, useRef, useState } from 'react';
import { Download, ExternalLink, HardDrive, LoaderCircle, RefreshCw, Upload } from 'lucide-react';
import { Alert, Field, Loading } from '../components';
import { ManufacturingError, manufacturingApi, useManufacturing, useManufacturingResource } from '../manufacturing-api';
import type { ManufacturingMeta } from '../manufacturing-types';

export type ManufacturingBackup = {
  id: string; name: string; createdAt: string; source: string; sizeBytes: number;
  counts: Record<string, number>;
};
type RecoveryStatus = ManufacturingMeta & {
  provider: 'local' | 'drive'; retention: number; location: string;
  folderUrl: string | null; configuredFolderUrl: string | null;
  lastBackupAt: string | null; lastError: string | null;
  canBackup: boolean; canRestore: boolean; backups: ManufacturingBackup[];
};
type Preview = {
  token: string; generation: string; expiresAt: string; backup: ManufacturingBackup; warnings: string[];
};
type RestoreAttempt = { writeId: string; preview: Preview; backupId: string; file: File | null };
type Pending = { backup: string | null; restore: RestoreAttempt | null };
const pending = new Map<string, Pending>();
const maxSize = 64 * 1024 * 1024;
const message = (error: unknown) => error instanceof Error ? error.message : 'Recovery could not be confirmed.';
const uncertain = (error: unknown) => !(error instanceof ManufacturingError) || error.status >= 500 || error.status === 408;
const identifier = () => crypto.randomUUID().replaceAll('-', '');
const dateTime = (value: string | null) => value ? `${new Date(value).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' })} IST` : 'Not yet';
const source = (value: string) => ({ manual: 'Manual', scheduled: 'Scheduled', 'pre-restore': 'Pre-restore recovery' }[value] || value);
const labels: [string, string][] = [
  ['products', 'Products'], ['operations', 'Operations'], ['partners', 'Suppliers'], ['contacts', 'Contacts'],
  ['steps', 'Product steps'], ['assignments', 'Supplier links'], ['manufacturingRecords', 'Manufacturing records'],
  ['manufacturingRecordEvents', 'Activity events'], ['sequences', 'Number counters'], ['writes', 'Write receipts'],
];

export function ManufacturingRecovery() {
  const resource = useManufacturingResource<RecoveryStatus>('/settings/backup-recovery', { keepPreviousData: true });
  return <>
    {resource.error && <Alert>{resource.error}</Alert>}
    {resource.loading && !resource.data && <Loading />}
    {resource.data && <RecoveryControls initial={resource.data} />}
  </>;
}

function RecoveryControls({ initial }: { initial: RecoveryStatus }) {
  const { meta, refresh, workspaceChanged, noteWorkspaceChange } = useManufacturing();
  const identity = useRef(meta).current;
  const key = `${identity.workspaceId}:${identity.generation}`;
  const operations = useRef(pending.get(key) || { backup: null, restore: null }).current;
  const [settings, setSettings] = useState(initial);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [unconfirmed, setUnconfirmed] = useState(!!operations.backup || !!operations.restore);
  const [backupId, setBackupId] = useState(operations.restore?.backupId || '');
  const [file, setFile] = useState<File | null>(operations.restore?.file || null);
  const [preview, setPreview] = useState<Preview | null>(operations.restore?.preview || null);
  const [confirmation, setConfirmation] = useState(operations.restore ? 'RESTORE' : '');
  const [now, setNow] = useState(Date.now());
  const input = useRef<HTMLInputElement>(null);
  const inFlight = useRef(false);
  const local = settings.provider === 'local';
  const locked = !!busy || unconfirmed || workspaceChanged;
  const expired = !!preview && Date.parse(preview.expiresAt) <= now;
  useEffect(() => setSettings(initial), [initial]);
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  useEffect(() => {
    const unload = (event: BeforeUnloadEvent) => {
      if (inFlight.current || operations.backup || operations.restore) { event.preventDefault(); event.returnValue = ''; }
    };
    const navigate = (event: MouseEvent) => {
      const target = event.target instanceof Element
        ? event.target.closest('a[href], [role="tab"], [aria-label="Refresh manufacturing data"]') : null;
      if (target && !target.hasAttribute('download') && (inFlight.current || operations.backup || operations.restore)) {
        event.preventDefault(); event.stopImmediatePropagation();
        setError('Keep this page open until recovery is confirmed. Retry the same operation below.');
      }
    };
    window.addEventListener('beforeunload', unload); document.addEventListener('click', navigate, true);
    return () => { window.removeEventListener('beforeunload', unload); document.removeEventListener('click', navigate, true); };
  }, [operations]);
  const remember = () => {
    if (operations.backup || operations.restore) pending.set(key, operations); else pending.delete(key);
    setUnconfirmed(!!operations.backup || !!operations.restore);
  };
  const begin = (action: string) => {
    if (inFlight.current || workspaceChanged) return false;
    inFlight.current = true; setBusy(action); setError(''); setNotice(''); return true;
  };
  const end = () => { inFlight.current = false; setBusy(''); };
  const fail = (reason: unknown) => {
    if (reason instanceof ManufacturingError && reason.code === 'workspace_changed') noteWorkspaceChange();
    setError(message(reason)); remember();
  };
  const clearPreview = () => { setPreview(null); setConfirmation(''); setError(''); setNotice(''); };
  const createBackup = async () => {
    if (!settings.canBackup || operations.restore || !begin('backup')) return;
    try {
      operations.backup ||= identifier(); remember();
      const result = await manufacturingApi<{ backup: ManufacturingBackup }>('/backups', {
        method: 'POST', body: JSON.stringify({ writeId: operations.backup }),
      }, identity);
      if (!result.backup?.id) throw new ManufacturingError('Backup is unconfirmed. Retry the same backup.', 503);
      operations.backup = null; remember();
      setSettings(old => ({ ...old, lastBackupAt: result.backup.createdAt,
        backups: [result.backup, ...old.backups.filter(row => row.id !== result.backup.id)].slice(0, old.retention) }));
      setNotice(local ? 'ProductTrackr backup verified locally. Nothing was uploaded to Google Drive.' : 'ProductTrackr backup saved and verified in your private Drive folder.');
      refresh();
    } catch (reason) {
      if (!uncertain(reason)) operations.backup = null;
      fail(reason);
    } finally { end(); }
  };
  const inspect = async () => {
    if (!settings.canRestore || locked || (!file && !backupId) || !begin('inspect')) return;
    clearPreview();
    try {
      const result = await manufacturingApi<Preview>('/backups/inspect', {
        method: 'POST', headers: { 'Content-Type': file ? 'application/zip' : 'application/json' },
        body: file || JSON.stringify({ backupId }),
      }, identity);
      if (!result.token || !result.backup?.id || !result.generation || !Number.isFinite(Date.parse(result.expiresAt))) {
        throw new ManufacturingError('Invalid preview response. Validate the archive again.', 503);
      }
      if (result.generation !== identity.generation) throw new ManufacturingError('ProductTrackr changed. Reload before restoring.', 409, 'workspace_changed');
      setNow(Date.now()); setPreview(result);
    } catch (reason) { fail(reason); }
    finally { end(); }
  };
  const restore = async () => {
    if (!settings.canRestore || operations.backup || !preview ||
        (!operations.restore && (confirmation !== 'RESTORE' || expired)) || !begin('restore')) return;
    try {
      operations.restore ||= { writeId: identifier(), preview, backupId, file }; remember();
      const attempt = operations.restore;
      const result = await manufacturingApi<{ restored: boolean; generation: string }>('/backups/restore', {
        method: 'POST',
        headers: attempt.file ? {
          'Content-Type': 'application/zip', 'X-ProductTrackr-Restore-Token': attempt.preview.token,
          'X-ProductTrackr-Restore-Confirm': 'RESTORE', 'X-ProductTrackr-Write-Id': attempt.writeId,
        } : { 'Content-Type': 'application/json' },
        body: attempt.file || JSON.stringify({ writeId: attempt.writeId, previewToken: attempt.preview.token, backupId: attempt.backupId, confirmation: 'RESTORE' }),
      }, identity);
      if (result.restored !== true || !result.generation || result.generation === identity.generation) {
        throw new ManufacturingError('Restore is unconfirmed. Retry the same restore.', 503);
      }
      operations.restore = null; remember(); inFlight.current = false;
      setBusy('reload'); setNotice('Restore confirmed. Reloading ProductTrackr to discard old forms...');
      window.location.reload();
    } catch (reason) {
      if (!uncertain(reason)) { operations.restore = null; clearPreview(); }
      fail(reason); end();
    }
  };
  return <div className="backup-settings">
    {local && <Alert tone="warning">Local simulation only. Archives are saved on this computer, not uploaded to Google Drive. Product files may still use the configured Drive root; ZIP backup storage is configured separately.</Alert>}
    <p>SalesTrackr's Drive backups do not include this workspace. ProductTrackr archives are separate.</p>
    {error && <Alert>{error}</Alert>}
    {notice && <Alert tone="success">{notice}</Alert>}
    {unconfirmed && <Alert tone="warning">The {operations.restore ? 'restore' : 'backup'} is unconfirmed. Its exact request and operation ID are retained. Retry the same operation; do not reload or start another one.</Alert>}
    {busy && <div className="backup-progress" role="status" aria-live="polite"><LoaderCircle size={18} className="spin" />{
      busy === 'restore' ? 'Backing up current state, staging and verifying recovery. Keep this page open.'
        : busy === 'inspect' ? 'Validating the complete archive. No live data has changed.'
          : busy === 'reload' ? 'Restore confirmed. Reloading ProductTrackr...' : 'Creating and verifying a complete ProductTrackr backup...'
    }</div>}
    <section className="panel details-panel settings-section" aria-label="ProductTrackr backup status">
      <div className="section-heading compact"><h2><HardDrive size={19} />ProductTrackr backups</h2>
        <button className="text-button" disabled={locked} onClick={refresh}><RefreshCw size={14} />Refresh backup status</button>
      </div>
      <p>One portable ZIP includes every Product, Operation, Supplier and Contact; all process steps and supplier links; manufacturing records with their copied operations and progress; complete activity history, number counters, write receipts and workspace preferences.</p>
      <p>Drawings and files in product Drive folders are not included in ZIP backups. Restoring this workspace or cleaning up jobs does not delete those files.</p>
      <p>Keep the <strong>{settings.retention} newest verified backups total</strong>, including manual and pre-restore copies. Older copies rotate out only after a new backup is verified. No original is permanently protected.</p>
      <p className="help-text">Backups run manually and before restores. No automatic or weekly scheduler is configured. ZIP files contain readable JSON and are not encrypted; keep them private. Credentials and environment configuration are never included.</p>
      <dl className="backup-summary"><div><dt>Backup storage</dt><dd>{settings.location}</dd></div><div><dt>Last verified backup</dt><dd>{dateTime(settings.lastBackupAt)}</dd></div></dl>
      {(settings.folderUrl || settings.configuredFolderUrl) && <p><a className="text-button" href={settings.folderUrl || settings.configuredFolderUrl!} target="_blank" rel="noreferrer">{local ? 'Configured ProductTrackr root (backups are local)' : 'Open ProductTrackr / Backups in Drive'}<ExternalLink size={14} /></a></p>}
      {settings.lastError && <Alert>{settings.lastError}</Alert>}
      <button className="button primary" disabled={!!busy || workspaceChanged || !settings.canBackup || !!operations.restore} onClick={() => void createBackup()}>
        <HardDrive size={16} />{operations.backup ? 'Retry same backup' : local ? 'Create local backup' : 'Back up now to Drive'}
      </button>
    </section>
    <section className="panel details-panel settings-section" aria-label="Available ProductTrackr backups">
      <h2>Available backups</h2>
      {!settings.backups.length ? <p>No ProductTrackr backups yet.</p> : <div className="table-scroll"><table className="backup-table">
        <thead><tr><th>Archive</th><th>Created (IST)</th><th>Source</th><th>Size</th><th>Download</th></tr></thead>
        <tbody>{settings.backups.map(row => <tr key={row.id}><td><strong>{row.name}</strong></td><td>{dateTime(row.createdAt)}</td><td>{source(row.source)}</td><td>{(row.sizeBytes / 1024).toFixed(1)} KiB</td>
          <td><a className="text-button" href={`/api/manufacturing/backups/${encodeURIComponent(row.id)}/download`} download><Download size={15} />ZIP<span className="sr-only"> {row.name}</span></a></td>
        </tr>)}</tbody></table></div>}
    </section>
    <section className="panel details-panel settings-section" aria-label="Restore ProductTrackr">
      <h2>Restore ProductTrackr</h2>
      <Alert tone="warning">Full rollback, not a merge. Current ProductTrackr entities, relationships, job progress and history will be replaced. SalesTrackr is not changed.</Alert>
      <p>Current data is backed up and verified before switching to the restored dataset. Archived data is removed only after its matching safety backup is physically verified. Successful recovery reloads ProductTrackr and discards old forms.</p>
      <div className="form-grid restore-selection">
        <Field label="Choose a ProductTrackr backup"><select value={backupId} disabled={locked || !settings.canRestore} onChange={event => {
          setBackupId(event.target.value); setFile(null); if (input.current) input.current.value = ''; clearPreview();
        }}><option value="">Select an archive</option>{settings.backups.map(row => <option key={row.id} value={row.id}>{row.name} - {source(row.source)}</option>)}</select></Field>
        <Field label="Or upload a ProductTrackr ZIP" hint="Complete ProductTrackr archives only; SalesTrackr backups cannot be used. Maximum 64 MiB."><input ref={input} type="file" accept=".zip,application/zip" disabled={locked || !settings.canRestore} onChange={event => {
          clearPreview(); setFile(null); setBackupId('');
          const selected = event.target.files?.[0];
          if (!selected) return;
          if (!selected.name.toLowerCase().endsWith('.zip') || selected.size > maxSize) {
            setError('Choose a ProductTrackr .zip archive no larger than 64 MiB.'); event.target.value = ''; return;
          }
          setFile(selected);
        }} /></Field>
      </div>
      {file && <p className="help-text">Selected archive: {file.name}</p>}
      <button className="button" disabled={locked || !settings.canRestore || (!file && !backupId)} onClick={() => void inspect()}><Upload size={16} />Preview and validate backup</button>
      {preview && <div className="restore-preview" aria-label="ProductTrackr restore preview">
        <h3>{preview.backup.name}</h3><dl className="backup-summary">
          {labels.map(([field, label]) => <div key={field}><dt>{label}</dt><dd>{preview.backup.counts[field]}</dd></div>)}
        </dl>
        {preview.warnings.map(warning => <Alert key={warning} tone="warning">{warning}</Alert>)}
        <p className="help-text">Preview expires {dateTime(preview.expiresAt)}. Changes to live data invalidate it; preview again if rejected.</p>
        {expired && !operations.restore && <Alert>This preview has expired. Preview and validate the backup again.</Alert>}
        <Field label="Type RESTORE to confirm full rollback"><input autoComplete="off" value={confirmation} disabled={locked || expired} onChange={event => setConfirmation(event.target.value)} /></Field>
        <button className="button danger" disabled={!!busy || workspaceChanged || !settings.canRestore || !!operations.backup ||
          (!operations.restore && (confirmation !== 'RESTORE' || expired))} onClick={() => void restore()}>
          {operations.restore ? 'Retry same restore' : 'Restore ProductTrackr workspace'}
        </button>
      </div>}
    </section>
  </div>;
}
