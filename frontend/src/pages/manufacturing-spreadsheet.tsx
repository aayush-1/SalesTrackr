import { useEffect, useRef, useState } from 'react';
import { Download, ExternalLink, FileSpreadsheet, LoaderCircle, RefreshCw } from 'lucide-react';
import { Alert, Loading } from '../components';
import { ManufacturingError, manufacturingApi, useManufacturing, useManufacturingResource } from '../manufacturing-api';
import type { ManufacturingMeta } from '../manufacturing-types';

type Spreadsheet = {
  id: string; name: string; provider: 'local' | 'drive'; generation: string; workspaceId: string;
  exportedAt: string; counts: Record<string, number>; url: string | null;
};
type ExportStatus = ManufacturingMeta & {
  provider: 'local' | 'drive'; name: string; folderUrl: string | null; canExport: boolean;
  spreadsheet: Spreadsheet | null; lastError: string | null; pendingWriteId: string | null;
};
const pending = new Map<string, string>();
const labels: [string, string, string][] = [
  ['products', 'Products', 'Part numbers, names and ordered operation summaries.'],
  ['steps', 'Product operations', 'Step order, instructions and all supplier choices, with the primary highlighted.'],
  ['assignments', 'Supplier links', 'Every supplier option and its selected company contact.'],
  ['operations', 'Operations', 'All reusable operations, including unused ones.'],
  ['suppliers', 'Suppliers', 'Companies, individuals, contact details and internal/external relationships.'],
  ['contacts', 'Contacts', 'All company contacts, with their owning supplier.'],
];
const keyDescriptions = [
  ['Product key', 'The Product process this row belongs to. Not the user-entered Part number.'],
  ['Step key', 'One particular step in that Product process, even if an operation is repeated.'],
  ['Operation key', 'The reusable operation definition.'],
  ['Supplier key', 'The company or independent person.'],
  ['Contact key', 'The optional person at that company.'],
  ['Link key', 'One supplier/contact option linked to a particular Product step.'],
  ['Primary link key', 'The preferred supplier option for that Product step.'],
];

function RelationshipKeyHelp() {
  return <details className="bt-sheet-keys">
    <summary>What are the relationship keys?</summary>
    <p>These stable internal IDs connect the tabs even when names change or repeat. You do not need to read or enter them.
      They are retained but hidden by default, rather than deleting the relationships.</p>
    <dl>{keyDescriptions.map(([name, meaning]) => <div key={name}><dt>{name}</dt><dd>{meaning}</dd></div>)}</dl>
  </details>;
}

export function ManufacturingSpreadsheet() {
  const resource = useManufacturingResource<ExportStatus>('/settings/spreadsheet', { keepPreviousData: true });
  return <>
    {resource.error && <Alert>{resource.error}</Alert>}
    {resource.loading && !resource.data && <Loading />}
    {resource.data && <SpreadsheetControls initial={resource.data} />}
  </>;
}

function SpreadsheetControls({ initial }: { initial: ExportStatus }) {
  const { meta, refresh, workspaceChanged, noteWorkspaceChange } = useManufacturing();
  const identity = useRef(meta).current;
  const key = `${identity.workspaceId}:${identity.generation}`;
  const ticket = useRef(pending.get(key) || initial.pendingWriteId || null);
  const inFlight = useRef(false);
  const [status, setStatus] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [unconfirmed, setUnconfirmed] = useState(!!ticket.current);
  const [error, setError] = useState(initial.lastError || '');
  const [notice, setNotice] = useState('');
  useEffect(() => setStatus(initial), [initial]);
  useEffect(() => {
    const unload = (event: BeforeUnloadEvent) => {
      if (inFlight.current) { event.preventDefault(); event.returnValue = ''; }
    };
    window.addEventListener('beforeunload', unload);
    return () => window.removeEventListener('beforeunload', unload);
  }, []);
  const exportSpreadsheet = async () => {
    if (inFlight.current || workspaceChanged || !status.canExport) return;
    inFlight.current = true; setBusy(true); setError(''); setNotice('');
    ticket.current ||= crypto.randomUUID().replaceAll('-', '');
    pending.set(key, ticket.current); setUnconfirmed(true);
    try {
      const result = await manufacturingApi<Spreadsheet>('/settings/spreadsheet', {
        method: 'POST', body: JSON.stringify({ writeId: ticket.current }),
      }, identity);
      if (!result.id || !result.counts || !Number.isFinite(Date.parse(result.exportedAt)) ||
          result.provider !== status.provider) {
        throw new ManufacturingError('Export is unconfirmed. Retry export.', 503);
      }
      if (result.workspaceId !== identity.workspaceId || result.generation !== identity.generation) {
        throw new ManufacturingError('ProductTrackr changed. Reload before exporting.', 409, 'workspace_changed');
      }
      ticket.current = null; pending.delete(key); setUnconfirmed(false);
      setStatus(old => ({ ...old, spreadsheet: result, lastError: null }));
      setNotice(result.provider === 'local'
        ? 'Spreadsheet updated and verified locally. Nothing was uploaded to Google Drive.'
        : 'The same directory workbook was updated and verified in your SalesTrackr Drive folder.');
      refresh();
    } catch (reason) {
      if (reason instanceof ManufacturingError && reason.code === 'workspace_changed') noteWorkspaceChange();
      if (reason instanceof ManufacturingError && reason.status < 500 && reason.status !== 408) {
        ticket.current = null; pending.delete(key); setUnconfirmed(false);
      }
      setError(reason instanceof Error ? reason.message : 'Export could not be confirmed.');
    } finally { inFlight.current = false; setBusy(false); }
  };
  const local = status.provider === 'local';
  const exported = status.spreadsheet;
  return <div className="backup-settings">
    <div className="alert warning"><FileSpreadsheet size={18} /><span>{local
      ? 'Local preview only. This updates a downloadable XLSX on this computer, not Google Drive.'
      : 'Exports use your private SalesTrackr folder. The same managed workbook is updated, not a new copy each time.'}</span></div>
    <section className="panel details-panel settings-section">
      <div className="section-heading compact">
        <div><h2><FileSpreadsheet size={19} />Directory spreadsheet</h2>
          <p>Products first, with the complete process and supplier/contact relationships in linked worksheet tabs.</p></div>
        <button className="button primary" onClick={exportSpreadsheet} disabled={busy || workspaceChanged || !status.canExport}>
          {busy ? <LoaderCircle size={16} className="spin" /> : exported || unconfirmed ? <RefreshCw size={16} /> : <FileSpreadsheet size={16} />}
          {busy ? 'Updating spreadsheet...' : unconfirmed ? 'Retry export' : exported ? 'Update spreadsheet' : 'Create spreadsheet'}
        </button>
      </div>
      {error && <Alert>{error}</Alert>}
      {notice && <Alert tone="success">{notice}</Alert>}
      <p><strong>{status.name}</strong> is a snapshot, not a two-way sync. Editing the spreadsheet does not change ProductTrackr.
        Every export replaces its contents, including any manual spreadsheet edits.</p>
      <p>Stable internal keys preserve relationships even when names or Part numbers repeat. Click the linked cells to jump
        between products, operation steps, supplier options and company contacts. Empty processes and unlinked directory entries are included.</p>
      <p>The Products sheet includes clickable Drive-folder links where a folder is already connected. Exporting does not create folders or include their files.</p>
      <p><strong>All suppliers are included.</strong> Product operations lists every available supplier name; Supplier links
        has one row per option, with its contact and a Yes/No primary flag. Technical key columns are hidden by default.</p>
      <div className="bt-export-sheets">{labels.map(([kind, label, description]) => <div key={kind}>
        <strong>{label}{exported ? ` (${exported.counts[kind]})` : ''}</strong><p>{description}</p>
      </div>)}</div>
      {exported && <div className="bt-export-result">
        <p>Last verified export: {new Date(exported.exportedAt).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' })} IST.</p>
        {exported.generation !== meta.generation && <Alert>This workbook predates a restore. Update it to export the current workspace.</Alert>}
        {!busy && !unconfirmed && <div className="actions">
          {!local && exported.url
            ? <a className="button" href={exported.url} target="_blank" rel="noreferrer"><ExternalLink size={16} />Open spreadsheet</a>
            : <button type="button" className="button" disabled title="Available after a Google Drive export. Local preview does not upload files."><ExternalLink size={16} />Open spreadsheet</button>}
          <a className="button" href="/api/manufacturing/settings/spreadsheet/download" download><Download size={16} />Download spreadsheet</a>
        </div>}
        {local && <p className="muted">Open spreadsheet is available after a Google Drive export. This local preview only provides a download.</p>}
      </div>}
      <RelationshipKeyHelp />
      {status.folderUrl && <p><a href={status.folderUrl} target="_blank" rel="noreferrer">Open SalesTrackr folder</a>
        {local && ' (not used by this local export)'}</p>}
      <p className="muted">Manufacturing jobs, history and write receipts are not included. This report is not a recovery
        backup; use Data &amp; recovery for a full workspace backup. SalesTrackr is not changed.</p>
    </section>
  </div>;
}
