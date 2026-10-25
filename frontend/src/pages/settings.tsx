import { useEffect, useRef, useState } from 'react';
import { Download, ExternalLink, HardDrive, LoaderCircle, RefreshCw, Upload } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import { api, ApiError } from '../api';
import { Alert, Field, Loading, PageHeader } from '../components';
import type { Backup, BackupPreview, BackupSettings } from '../types';
import { RecordCleanup } from './record-cleanup';

const maxArchiveSize = 64 * 1024 * 1024;
type RestoreAttempt = { writeId: string; preview: BackupPreview; backupId: string; file: File | null };
// Keep unconfirmed operations intact when moving between Settings and other workspace pages.
let pendingBackup: string | null = null;
let pendingRestore: RestoreAttempt | null = null;
const message = (error: unknown) => error instanceof Error ? error.message : 'The operation failed.';
const unknown = (error: unknown) => !(error instanceof ApiError) || error.status >= 500;
const operationId = () => {
  if (!globalThis.crypto?.randomUUID) throw new Error('Secure operation IDs are unavailable. Use a supported browser on HTTPS or localhost.');
  return globalThis.crypto.randomUUID().replaceAll('-', '');
};
const dateTime = (value: string | null) => value
  ? `${new Date(value).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' })} IST` : 'Not yet';
const size = (bytes: number) => bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024
  ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
const source = (backup: Backup) => ({ manual: 'Manual', scheduled: 'Weekly scheduled', migration: 'Original migration', 'pre-restore': 'Pre-restore recovery' })[backup.source];

function BackupSummary({ backup }: { backup: Backup }) {
  return <dl className="backup-summary">
    <div><dt>Created</dt><dd>{dateTime(backup.createdAt)}</dd></div>
    <div><dt>Source</dt><dd>{source(backup)}</dd></div>
    <div><dt>Accounts</dt><dd>{backup.counts.account}</dd></div>
    <div><dt>Contacts</dt><dd>{backup.counts.contact}</dd></div>
    <div><dt>Sales records</dt><dd>{backup.counts.record}</dd></div>
    <div><dt>Revisions</dt><dd>{backup.counts.revisions}</dd></div>
  </dl>;
}

export function Settings() {
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') === 'general' ? 'general' : params.get('tab') === 'cleanup' ? 'cleanup' : 'backup';
  const [settings, setSettings] = useState<BackupSettings | null>(null);
  const [loadError, setLoadError] = useState('');
  const [loading, setLoading] = useState(true);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setLoadError('');
    api<BackupSettings>('/settings/backup-recovery', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setSettings(value); })
      .catch(error => { if (!controller.signal.aborted) setLoadError(message(error)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [version]);
  return <>
    <PageHeader eyebrow="WORKSPACE PREFERENCES" title="Settings" description="Manage your workspace storage, backups and recovery."
      action={<button className="button" disabled={loading} onClick={() => setVersion(value => value + 1)}><RefreshCw size={16} />Refresh backup status</button>} />
    <div className="settings-tabs" role="tablist" aria-label="Settings sections">
      <button role="tab" id="general-tab" aria-controls="general-panel" aria-selected={tab === 'general'} onClick={() => setParams({ tab: 'general' })}>General</button>
      <button role="tab" id="backup-tab" aria-controls="backup-panel" aria-selected={tab === 'backup'} onClick={() => setParams({ tab: 'backup' })}>Backup and recovery</button>
      <button role="tab" id="cleanup-tab" aria-controls="cleanup-panel" aria-selected={tab === 'cleanup'} onClick={() => setParams({ tab: 'cleanup' })}>Record cleanup</button>
    </div>
    {tab === 'cleanup' && <div role="tabpanel" id="cleanup-panel" aria-labelledby="cleanup-tab"><RecordCleanup demo={settings?.mode === 'demo'} /></div>}
    {loadError && <Alert>{loadError} Retry using Refresh backup status. Loading status does not start a backup or restore.</Alert>}
    {loading && !settings && <Loading />}
    {settings && <>
      <section className="panel details-panel settings-section" role="tabpanel" id="general-panel" aria-labelledby="general-tab" hidden={tab !== 'general'}>
        <h2>Workspace storage</h2>
        <p>{settings.mode === 'demo' ? 'This local demo simulates storage and recovery. It is not connected to Google Drive.' : settings.storageBackend === 'firestore' ? 'Firestore is the live workspace database. Backups are portable ZIP files containing plain JSON.' : 'This workspace currently uses Google Drive storage. Firestore migration is not enabled.'}</p>
        <dl><dt>Backup location</dt><dd>{settings.location}</dd><dt>Workspace mode</dt><dd>{settings.mode === 'demo' ? 'Local simulation — Google Drive is not connected' : 'Private production workspace'}</dd></dl>
      </section>
      <div role="tabpanel" id="backup-panel" aria-labelledby="backup-tab" hidden={tab !== 'backup'}>
        <BackupRecovery settings={settings} onSettings={setSettings} />
      </div>
    </>}
  </>;
}

function BackupRecovery({ settings, onSettings }: { settings: BackupSettings; onSettings: (value: BackupSettings) => void }) {
  const [busy, setBusy] = useState('');
  const inFlight = useRef(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [backupUnconfirmed, setBackupUnconfirmed] = useState(!!pendingBackup);
  const [restoreUnconfirmed, setRestoreUnconfirmed] = useState(!!pendingRestore);
  const [weeklyEnabled, setWeeklyEnabled] = useState(settings.schedule.enabled);
  const [backupId, setBackupId] = useState(pendingRestore?.backupId || '');
  const [file, setFile] = useState<File | null>(pendingRestore?.file || null);
  const [preview, setPreview] = useState<BackupPreview | null>(pendingRestore?.preview || null);
  const [confirmation, setConfirmation] = useState(pendingRestore ? 'RESTORE' : '');
  const [now, setNow] = useState(Date.now());
  const input = useRef<HTMLInputElement>(null);
  const locked = !!busy || backupUnconfirmed || restoreUnconfirmed;
  const expired = !!preview && Date.parse(preview.expiresAt) <= now;
  const stale = !!preview && settings.generation !== preview.generation;
  const demo = settings.mode === 'demo';
  const disabled = !demo && settings.storageBackend !== 'firestore';
  useEffect(() => setWeeklyEnabled(settings.schedule.enabled), [settings.schedule.enabled]);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    const unload = (event: BeforeUnloadEvent) => {
      if (inFlight.current || pendingBackup || pendingRestore) { event.preventDefault(); event.returnValue = ''; }
    };
    const navigate = (event: MouseEvent) => {
      const anchor = event.target instanceof Element ? event.target.closest('a[href]') : null;
      if (anchor && !anchor.hasAttribute('download') && (inFlight.current || pendingBackup || pendingRestore)) {
        event.preventDefault(); event.stopImmediatePropagation();
        setError('Keep this page open until the current operation is confirmed. Retry the same operation if its response was interrupted.');
      }
    };
    window.addEventListener('beforeunload', unload);
    document.addEventListener('click', navigate, true);
    return () => { window.removeEventListener('beforeunload', unload); document.removeEventListener('click', navigate, true); };
  }, []);
  const begin = (operation: string) => {
    if (inFlight.current) return false;
    inFlight.current = true; setBusy(operation); setError(''); setNotice('');
    return true;
  };
  const end = () => { inFlight.current = false; setBusy(''); };
  const clearPreview = () => { setPreview(null); setConfirmation(''); setError(''); setNotice(''); };
  const createBackup = async () => {
    if (!settings.canBackup || disabled || pendingRestore || !begin('backup')) return;
    try {
      pendingBackup ||= operationId();
      const result = await api<{ backup: Backup }>('/backups', { method: 'POST', body: JSON.stringify({ writeId: pendingBackup }) });
      if (!result.backup?.id) throw new ApiError('Backup response is unconfirmed. Retry the same backup.', 503, 'response');
      pendingBackup = null; setBackupUnconfirmed(false);
      onSettings({ ...settings, backups: [result.backup, ...settings.backups.filter(row => row.id !== result.backup.id)], lastBackupAt: result.backup.createdAt });
      setNotice(demo ? 'Local simulated backup created. Nothing was uploaded to Google Drive.' : 'Backup created directly in your private Google Drive folder.');
      try { onSettings(await api<BackupSettings>('/settings/backup-recovery')); }
      catch (error) { setError(`Backup creation was confirmed, but its updated status could not be loaded: ${message(error)}`); }
    } catch (error) {
      if (!unknown(error)) pendingBackup = null;
      setBackupUnconfirmed(!!pendingBackup); setError(message(error));
    } finally { end(); }
  };
  const saveSchedule = async (weeklyEnabled: boolean) => {
    if (disabled || !settings.canRestore || locked || !begin('schedule')) return;
    setWeeklyEnabled(weeklyEnabled);
    try {
      const updated = await api<BackupSettings>('/settings/backup-recovery', { method: 'POST', body: JSON.stringify({ weeklyEnabled }) });
      onSettings(updated); setWeeklyEnabled(updated.schedule.enabled);
      setNotice(demo ? 'Schedule preference saved locally. No cloud scheduler is running.'
        : updated.schedule.active ? 'Weekly schedule preference saved. The server reports the scheduler as active.'
          : 'Weekly schedule preference saved. The production scheduler still requires provisioning; scheduled backups are not active.');
    } catch (error) {
      setWeeklyEnabled(settings.schedule.enabled);
      setError(`${message(error)} ${unknown(error) ? 'The schedule preference is unconfirmed. Refresh backup status before relying on this setting.' : ''}`);
    }
    finally { end(); }
  };
  const inspect = async () => {
    if (disabled || !settings.canRestore || locked || (!file && !backupId) || !begin('inspect')) return;
    setPreview(null); setConfirmation('');
    try {
      const result = await api<BackupPreview>('/backups/inspect', {
        method: 'POST', headers: { 'Content-Type': file ? 'application/zip' : 'application/json' },
        body: file || JSON.stringify({ backupId }),
      });
      if (!result.token || !result.generation || !result.backup?.id || !Number.isFinite(Date.parse(result.expiresAt))) {
        throw new Error('The server returned an invalid backup preview. Inspect the backup again.');
      }
      setNow(Date.now()); setPreview(result);
    } catch (error) { setError(message(error)); }
    finally { end(); }
  };
  const restore = async () => {
    if (disabled || !settings.canRestore || pendingBackup || !preview ||
        (!pendingRestore && (confirmation !== 'RESTORE' || expired || stale)) || !begin('restore')) return;
    try {
      pendingRestore ||= { writeId: operationId(), preview, backupId, file };
      const attempt = pendingRestore;
      const result = await api<{ restored: boolean; generation: string }>('/backups/restore', {
        method: 'POST',
        headers: attempt.file ? {
          'Content-Type': 'application/zip', 'X-SalesTrackr-Restore-Token': attempt.preview.token,
          'X-SalesTrackr-Restore-Confirm': 'RESTORE', 'X-SalesTrackr-Write-Id': attempt.writeId,
        } : { 'Content-Type': 'application/json' },
        body: attempt.file || JSON.stringify({
          backupId: attempt.backupId, previewToken: attempt.preview.token, confirmation: 'RESTORE', writeId: attempt.writeId,
        }),
      });
      if (result.restored !== true || !result.generation) throw new ApiError('Restore response is unconfirmed. Retry the same restore; do not start another one.', 503, 'response');
      pendingRestore = null; setRestoreUnconfirmed(false);
      // A generation switch must discard every mounted form and cached resource.
      inFlight.current = false;
      setBusy('reload'); setNotice('Restore confirmed. Reloading the workspace to discard old forms...');
      window.location.reload();
    } catch (error) {
      if (!unknown(error)) { pendingRestore = null; setPreview(null); setConfirmation(''); }
      setRestoreUnconfirmed(!!pendingRestore); setError(message(error));
      end();
    }
  };
  return <div className="backup-settings">
    {demo && <Alert tone="warning">Local simulation only. Backups and restores use local demo data, not real Google Drive. No cloud scheduler is running.</Alert>}
    {disabled && <Alert tone="warning">Firestore migration is not enabled. Backup, scheduling and restore actions are unavailable for this Drive-backed workspace.</Alert>}
    {!demo && !disabled && !settings.canRestore && <Alert tone="warning">Restore and schedule changes require a recovery administrator. Manual backup and download remain available when permitted.</Alert>}
    {error && <Alert>{error}</Alert>}
    {notice && <Alert tone="success">{notice}</Alert>}
    {(backupUnconfirmed || restoreUnconfirmed) && <Alert tone="warning">The {restoreUnconfirmed ? 'restore' : 'backup'} is unconfirmed. Retry the same operation below. Its operation ID and exact request are retained; do not start a different operation or reload this page.</Alert>}
    {!!busy && <div className="backup-progress" role="status" aria-live="polite"><LoaderCircle size={18} className="spin" />{
      busy === 'reload' ? 'Restore confirmed. Reloading the workspace...'
        : busy === 'restore' ? 'Restoring and verifying your workspace. This can take time. Keep this page open; success is not yet confirmed.'
        : busy === 'inspect' ? 'Validating archive and preparing a preview. No restore has started.'
          : busy === 'backup' ? 'Creating backup. Keep this page open until the server confirms.' : 'Saving schedule preference...'
    }</div>}
    <section className="panel details-panel settings-section" aria-label="Backup status">
      <div className="section-heading compact"><h2><HardDrive size={19} />Backups {demo ? '· Local simulation' : '· Google Drive'}</h2>
        {settings.folderUrl && !demo && <a className="text-button" href={settings.folderUrl} target="_blank" rel="noreferrer">Open private Drive folder <ExternalLink size={14} /></a>}
      </div>
      <p>Portable, plain ZIP archives containing JSON. {demo ? 'Created locally for preview only.' : 'Saved directly to your private Drive folder, without a browser download step.'} These archives are not encrypted; keep them private.</p>
      <p>New backups contain readable <code>accounts.json</code>, <code>contacts.json</code> and <code>sales-records.json</code>. Each includes current data and full revision history. The manifest records backup details, counts and integrity checks.</p>
      <dl className="backup-summary"><div><dt>Backup location</dt><dd>{settings.location}</dd></div><div><dt>Last backup</dt><dd>{dateTime(settings.lastBackupAt)}</dd></div></dl>
      {settings.lastError && <Alert>Last backup error: {settings.lastError}</Alert>}
      <button className="button primary" disabled={!!busy || disabled || !settings.canBackup || restoreUnconfirmed} onClick={() => void createBackup()}><HardDrive size={16} />{backupUnconfirmed ? 'Retry same backup' : demo ? 'Create simulated backup' : 'Back up now to Drive'}</button>
    </section>
    <section className="panel details-panel settings-section" aria-label="Weekly backups">
      <h2>Weekly backups</h2>
      <p>Sunday at 02:00 Asia/Kolkata (IST). Keep only the {settings.retention} newest backups in total, including manual, weekly, migration and pre-restore copies. There is no protected exception. With weekly backups only, these cover the last {settings.retention} weeks; additional backups rotate older copies out sooner.</p>
      <label className="schedule-toggle"><input type="checkbox" checked={weeklyEnabled} disabled={locked || disabled || !settings.canRestore}
        onChange={event => void saveSchedule(event.target.checked)} /><span>Enable weekly backups</span></label>
      <p className="help-text">{demo ? 'This saves a local preference only. No cloud scheduler is running in the demo.'
        : settings.schedule.active ? 'The server reports the weekly scheduler as active.'
          : 'Scheduled backups are not active. The production scheduler requires separate provisioning; enabling this preference does not create a scheduler.'}</p>
    </section>
    <section className="panel details-panel settings-section" aria-label="Available backups">
      <h2>Available backups {demo && '(simulated)'}</h2>
      <p>{demo ? 'This list comes from the local simulated backup folder, not Google Drive.' : 'This list comes from your private Drive backup folder. Refresh backup status to discover valid, accessible backups and remove missing files from this list.'}</p>
      {!settings.backups.length ? <p>No backups yet.</p> : <div className="table-scroll"><table className="backup-table"><thead><tr><th>Archive</th><th>Created (IST)</th><th>Source</th><th>Size</th><th>Download</th></tr></thead>
        <tbody>{settings.backups.map(backup => <tr key={backup.id}><td><strong>{backup.name}</strong></td>
          <td>{dateTime(backup.createdAt)}</td><td>{source(backup)}</td><td>{size(backup.sizeBytes)}</td>
          <td><a className="text-button" href={`/api/backups/${encodeURIComponent(backup.id)}/download`} download><Download size={15} /><span className="sr-only">{backup.name} </span>{backup.name.toLowerCase().endsWith('.json') ? 'JSON' : 'ZIP'}</a></td></tr>)}</tbody></table></div>}
    </section>
    <section className="panel details-panel settings-section" aria-label="Restore workspace">
      <h2>Restore workspace</h2>
      <Alert tone="warning">Full rollback, not a merge. All current accounts, contacts, sales records and revision history will be replaced by the selected backup. Changes made after that backup will no longer be live.</Alert>
      <p>The server saves current state as a pre-restore recovery backup, stages and verifies the restored data, then switches the live workspace atomically. After confirmed success this page reloads to discard old forms.</p>
      {!demo && <p>Selecting a Drive backup fetches and validates that archive on the server when you preview it. Nothing is restored until you explicitly confirm.</p>}
      <div className="form-grid restore-selection">
        <Field label={demo ? 'Choose a simulated backup' : 'Choose a Drive backup'}><select value={backupId} disabled={locked || disabled || !settings.canRestore} onChange={event => {
          setBackupId(event.target.value); setFile(null); if (input.current) input.current.value = ''; clearPreview();
        }}><option value="">Select an archive</option>{settings.backups.map(backup => <option key={backup.id} value={backup.id}>{backup.name} · {source(backup)}</option>)}</select></Field>
        <Field label="Or upload a ZIP or legacy JSON archive" hint="Plain .zip or validated legacy .json. Maximum file size: 64 MiB (compressed for ZIP). The server validates archive contents.">
          <input ref={input} type="file" accept=".zip,.json,application/zip,application/json" disabled={locked || disabled || !settings.canRestore} onChange={event => {
            clearPreview(); setFile(null); setBackupId('');
            const selected = event.target.files?.[0];
            if (!selected) return;
            if (!/\.(zip|json)$/i.test(selected.name)) { setError('Choose a .zip or legacy .json backup archive.'); event.target.value = ''; return; }
            if (selected.size > maxArchiveSize) { setError('Backup archive exceeds the 64 MiB file size limit.'); event.target.value = ''; return; }
            setFile(selected);
          }} />
        </Field>
      </div>
      {file && <p className="help-text">Selected: {file.name} · {size(file.size)}</p>}
      <button className="button" disabled={locked || disabled || !settings.canRestore || (!file && !backupId)} onClick={() => void inspect()}><Upload size={16} />Preview and validate backup</button>
      {preview && <div className="restore-preview" aria-label="Restore preview">
        <h3>{preview.backup.name}</h3><BackupSummary backup={preview.backup} />
        {preview.warnings.map((warning, index) => <Alert key={index} tone="warning">{warning}</Alert>)}
        <p className="help-text">Preview expires {dateTime(preview.expiresAt)}. Changes to live data can invalidate it; inspect again if rejected.</p>
        {!restoreUnconfirmed && (expired || stale) && <Alert>This preview has expired or the workspace has changed. Preview and validate the backup again before restoring.</Alert>}
        <Field label="Type RESTORE to confirm full rollback"><input autoComplete="off" value={confirmation} disabled={locked || expired || stale} onChange={event => setConfirmation(event.target.value)} /></Field>
        <button className="button danger" disabled={!!busy || disabled || !settings.canRestore || backupUnconfirmed ||
          (!restoreUnconfirmed && (confirmation !== 'RESTORE' || expired || stale))} onClick={() => void restore()}>
          {restoreUnconfirmed ? 'Retry same restore' : demo ? 'Restore local demo workspace' : 'Restore entire workspace'}
        </button>
      </div>}
    </section>
  </div>;
}
