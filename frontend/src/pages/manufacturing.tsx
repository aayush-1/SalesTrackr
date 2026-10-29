import { useCallback, useEffect, useState } from 'react';
import { Link, Navigate, NavLink, Outlet, useSearchParams } from 'react-router-dom';
import { ArrowRight, ClipboardList, Factory, LayoutDashboard, Menu, PanelLeftClose, Plus, RefreshCw, Settings as SettingsIcon } from 'lucide-react';
import { Alert, Loading, PageHeader } from '../components';
import { ManufacturingContext, manufacturingApi, useManufacturingResource } from '../manufacturing-api';
import type { Directory, ManufacturingMeta, ManufacturingPage, ManufacturingRecordCounts, ManufacturingRecordView } from '../manufacturing-types';
import { WorkspaceSwitcher } from '../workspace-switcher';
import { directories } from './producttrackr';
import { ConnectedManufacturingRecords } from './manufacturing-records';
import { SupplierWorkSheet } from './supplier-work';

export function ManufacturingShell() {
  const [open, setOpen] = useState(false);
  const [meta, setMeta] = useState<ManufacturingMeta | null>(null);
  const [error, setError] = useState('');
  const [version, setVersion] = useState(0);
  const [connection, setConnection] = useState(0);
  const [workspaceChanged, setWorkspaceChanged] = useState(false);
  const noteWorkspaceChange = useCallback(() => setWorkspaceChanged(true), []);
  const refresh = useCallback(() => setVersion(v => v + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    manufacturingApi<ManufacturingMeta>('/meta', { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) { setMeta(result); setError(''); } })
      .catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [connection]);
  return <div className={`app manufacturing-workspace ${open ? 'navigation-open' : ''}`}>
    <aside className={`sidebar ${open ? 'open' : ''}`}>
      <WorkspaceSwitcher current="manufacturing" onSwitch={() => setOpen(false)} />
      <button className="mobile-close icon-button" onClick={() => setOpen(false)} aria-label="Close navigation"><PanelLeftClose /></button>
      <div className="workspace-label">YOUR MANUFACTURING SPACE</div>
      <nav aria-label="Manufacturing navigation">
        <NavLink end to="/manufacturing" onClick={() => setOpen(false)}><LayoutDashboard size={18} />Overview</NavLink>
        <NavLink to="/manufacturing/records" onClick={() => setOpen(false)}><ClipboardList size={18} />Manufacturing records</NavLink>
        <NavLink to="/manufacturing/supplier-work" onClick={() => setOpen(false)}><Factory size={18} />Supplier work</NavLink>
        <div className="nav-label">BUILD & CONNECT</div>
        {(Object.entries(directories) as [Directory, typeof directories[Directory]][]).map(([kind, { title, Icon }]) => <NavLink key={kind} to={`/manufacturing/${kind}`} onClick={() => setOpen(false)}><Icon size={18} />{title}</NavLink>)}
      </nav>
      <div className="unit-sidebar-note"><Factory size={18} /><strong>From process to progress</strong><p>Independent jobs. Clear responsibility. One operation at a time.</p></div>
      <div className="sidebar-bottom"><div className="avatar">P</div><div><strong>ProductTrackr</strong><small>Independent workspace</small></div>
        <NavLink to="/settings" className="sidebar-settings" aria-label="Settings" title="Settings" onClick={() => setOpen(false)}><SettingsIcon size={17} /></NavLink>
      </div>
    </aside>
    <div className="workspace">
      <div className="workspace-mobile-header"><button className="icon-button mobile-menu" aria-label="Open navigation" onClick={() => setOpen(true)}><Menu /></button><WorkspaceSwitcher current="manufacturing" onSwitch={() => setOpen(false)} /></div>
      <main>
        {error ? <><Alert>{error}</Alert><button className="button" onClick={() => { setError(''); setConnection(v => v + 1); }}>Retry connection</button></>
          : !meta ? <Loading /> : <ManufacturingContext.Provider value={{ meta, version, refresh, workspaceChanged, noteWorkspaceChange }}>
            {meta.mode === 'demo' && <div className="unit-preview-notice"><span>LOCAL PRODUCTTRACKR</span>Saved in a local Firestore emulator. Shared Settings manages backup, recovery and exports.</div>}
            {workspaceChanged && <Alert>ProductTrackr was restored in another session. Old forms cannot be saved. <button className="text-button" onClick={() => window.location.reload()}>Reload ProductTrackr</button></Alert>}
            <Outlet />
          </ManufacturingContext.Provider>}
      </main>
      <footer>ProductTrackr<span className="sync-status"><span>Independent manufacturing workspace</span><button className="icon-button" aria-label="Refresh manufacturing data" title="Refresh manufacturing data" onClick={refresh}><RefreshCw size={14} /></button></span></footer>
    </div>
  </div>;
}

function DirectoryCard({ kind }: { kind: Directory }) {
  const resource = useManufacturingResource<ManufacturingPage>(`/${kind}?limit=1`);
  const { title, description, Icon } = directories[kind];
  return <Link className="unit-area-card" to={`/manufacturing/${kind}`}><span className="unit-area-icon"><Icon size={22} /></span>
    <div className="bt-card-title"><h3>{title}</h3><span>{resource.loading ? '...' : resource.error ? 'Unavailable' : resource.data?.total}</span></div>
    <p>{description}</p><span className="unit-area-action">Open {title.toLowerCase()}<ArrowRight size={15} /></span>
  </Link>;
}
export function ManufacturingOverview() {
  const summary = useManufacturingResource<ManufacturingRecordCounts>('/records/summary');
  const cards: [ManufacturingRecordView, string, string][] = [
    ['active', 'Work in process', 'All active manufacturing jobs'], ['ready', 'Awaiting assignment', 'Ready for the next supplier'],
    ['in_progress', 'With a supplier', 'An operation is underway'], ['completed', 'Completed', 'Every operation is finished'],
  ];
  return <>
    <PageHeader eyebrow="PRODUCTTRACKR / OVERVIEW" title="Every job. One clear next step." description="See what's in process, which operation is current, and who is responsible."
      action={<Link className="button primary" to="/manufacturing/records/new"><Plus size={16} />New manufacturing record</Link>} />
    {summary.error && <Alert>{summary.error}</Alert>}
    <div className="stats bt-work-stats">{cards.map(([view, title, description]) => <Link className="stat-card" key={view} to={`/manufacturing/records?view=${view}`}>
      <div className="stat-top">{title}<ArrowRight size={17} /></div><strong>{summary.loading ? '...' : summary.data?.counts[view] ?? '—'}</strong><div className="stat-bottom">{description}</div>
    </Link>)}</div>
    <ConnectedManufacturingRecords overview />
    <SupplierWorkSheet overview />
    <div className="unit-section-title"><h2>Your manufacturing directories</h2><span>Reusable processes. Connected people.</span></div>
    <div className="unit-area-grid bt-area-grid">{(Object.keys(directories) as Directory[]).map(kind => <DirectoryCard key={kind} kind={kind} />)}</div>
    <section className="unit-next-step"><span className="unit-area-icon"><Factory size={21} /></span><div><h2>A process defines the route. A record tracks the work.</h2>
      <p>Products hold reusable operation sequences and linked supplier choices. Manufacturing records copy those operations into independent jobs, then track explicit assignments and completions. Inventory, batching and costing are not included.</p></div></section>
  </>;
}

export function ManufacturingSettings() {
  const [params] = useSearchParams();
  const legacy = params.get('tab') || 'general';
  const tab = ({ recovery: 'backup', cleanup: 'manufacturing-cleanup', records: 'general' } as Record<string, string>)[legacy] || legacy;
  return <Navigate replace to={`/settings?tab=${encodeURIComponent(tab)}`} />;
}
export function ManufacturingNotFound() {
  return <div className="empty"><h1>ProductTrackr page not found</h1><Link className="button primary" to="/manufacturing">Return to ProductTrackr<ArrowRight size={16} /></Link></div>;
}
