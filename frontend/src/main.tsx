import { StrictMode, useCallback, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Link, Navigate, NavLink, Outlet, Route, Routes } from 'react-router-dom';
import {
  ArchiveX, Building2, CheckCheck, CircleDollarSign, ClipboardList, LayoutDashboard,
  Leaf, Menu, MessageSquare, MessageSquareText, PackageCheck, PanelLeftClose, Plus, RefreshCw, Settings as SettingsIcon, Truck,
} from 'lucide-react';
import { RefreshContext, WorkspaceContext, useResource } from './api';
import { Alert, Loading } from './components';
import type { Me, Meta, Save } from './types';
import { views } from './types';
import { AccountDetail, AccountForm, Accounts } from './pages/accounts';
import { Conflicts, DashboardPage, Records } from './pages/lists';
import { NewRecordPage, RecordDetail } from './pages/record';
import { Settings } from './pages/settings';
import {
  ManufacturingNotFound, ManufacturingOverview,
  ManufacturingSettings, ManufacturingShell,
} from './pages/manufacturing';
import { BuildDetail, BuildDirectory } from './pages/buildtrackr';
import { ManufacturingRecordDetailPage, ManufacturingRecordsPage, NewManufacturingRecordPage } from './pages/manufacturing-records';
import { WorkspaceSwitcher } from './workspace-switcher';
import './styles.css';

function Shell() {
  const [state, setState] = useState<{ version: number; lastSave: Save<unknown> | null }>({ version: 0, lastSave: null });
  const refresh = useCallback((lastSave?: Save<unknown>) => setState(old => ({ version: old.version + 1, lastSave: lastSave ?? null })), []);
  return <RefreshContext.Provider value={{ ...state, refresh }}><Workspace onRefresh={() => refresh()} /></RefreshContext.Provider>;
}
function Workspace({ onRefresh }: { onRefresh: () => void }) {
  const identity = useResource<Me>('/me');
  const status = useResource<Meta & { error: string | null }>('/status');
  const initialIdentity = useRef<Me | null>(null);
  if (!initialIdentity.current && identity.data) initialIdentity.current = identity.data;
  const workspaceChanged = initialIdentity.current && [identity.data, status.data].some(value =>
    value?.storageBackend === 'firestore' && (initialIdentity.current?.storageBackend !== 'firestore' ||
      initialIdentity.current.generation !== value.generation));
  const [open, setOpen] = useState(false);
  const nav = [
    ['/', 'Overview', LayoutDashboard], ['/records', 'All sales records', ClipboardList],
    ['/accounts', 'Accounts', Building2],
  ] as const;
  const pipeline = [
    ['leads', Leaf], ['followups', MessageSquare], ['orders', PackageCheck],
    ['dispatched', Truck], ['delivered', CheckCheck],
    ['pending', CircleDollarSign], ['paid', CircleDollarSign], ['closed', CheckCheck],
    ['feedback-pending', MessageSquareText], ['discarded', ArchiveX],
  ] as const;
  return <div className={`app ${open ? 'navigation-open' : ''}`}>
    <aside className={`sidebar ${open ? 'open' : ''}`}>
      <WorkspaceSwitcher current="sales" onSwitch={() => setOpen(false)} />
      <button className="mobile-close icon-button" onClick={() => setOpen(false)} aria-label="Close navigation"><PanelLeftClose /></button>
      <div className="workspace-label">YOUR SALES WORKSPACE</div>
      <nav aria-label="Main navigation">
        {nav.map(([to, title, Icon]) => <NavLink key={to} end to={to} onClick={() => setOpen(false)}><Icon size={18} />{title}</NavLink>)}
        <div className="nav-label">PIPELINE & COLLECTIONS</div>
        {pipeline.map(([view, Icon]) => <NavLink key={view} to={`/view/${view}`} onClick={() => setOpen(false)}><Icon size={17} />{views[view].title}</NavLink>)}
      </nav>
      <div className="sidebar-bottom"><div className="avatar">S</div><div><strong>Sales workspace</strong><small>{identity.data?.mode === 'demo' ? 'Local demo · sample data' : 'Private workspace'}</small></div>
        <NavLink to="/settings" className="sidebar-settings" aria-label="Settings" title="Settings" onClick={() => setOpen(false)}><SettingsIcon size={17} /></NavLink>
        <span className="online-dot" />
      </div>
    </aside>
    <div className="workspace">
      <div className="workspace-mobile-header">
        <button className="icon-button mobile-menu" aria-label="Open navigation" onClick={() => setOpen(true)}><Menu /></button>
        <WorkspaceSwitcher current="sales" onSwitch={() => setOpen(false)} />
      </div>
      <main>
        {identity.error && <Alert>{identity.error}</Alert>}
        {status.error && <Alert>Connection check failed: {status.error}</Alert>}
        {status.data?.error && <Alert>{status.data.error} Refresh after resolving the connection problem.</Alert>}
        {workspaceChanged && <Alert>The workspace changed. Reload this page before editing or saving. <button className="text-button" onClick={() => window.location.reload()}>Reload workspace</button></Alert>}
        {identity.data?.mode === 'demo' && <div className="demo-banner"><span>DEMO WORKSPACE</span>Sample data is saved locally. Google Drive is not connected.</div>}
        {initialIdentity.current ? <WorkspaceContext.Provider value={initialIdentity.current}><Outlet context={identity.data || initialIdentity.current} /></WorkspaceContext.Provider> : identity.loading ? <Loading /> : <Alert>Workspace identity is unavailable. Refresh this page before continuing.</Alert>}
      </main>
      <footer>SalesTrackr <span className="sync-status"><span>{status.data?.syncedAt
        ? `Last synchronized ${new Date(status.data.syncedAt).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata' })} IST${status.data.complete ? '' : ' · incomplete'}`
        : 'Checking connection...'}</span>
        <button className="icon-button" title="Refresh data" aria-label="Refresh data" onClick={onRefresh}><RefreshCw size={14} /></button>
      </span></footer>
    </div>
  </div>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><BrowserRouter><Routes>
  <Route path="manufacturing" element={<ManufacturingShell />}>
    <Route index element={<ManufacturingOverview />} />
    {(['products', 'operations', 'partners', 'contacts'] as const).map(kind => <Route key={kind}>
      <Route path={kind} element={<BuildDirectory key={kind} kind={kind} />} />
      <Route path={`${kind}/:id`} element={<BuildDetail key={kind} kind={kind} />} />
    </Route>)}
    <Route path="records" element={<ManufacturingRecordsPage />} />
    <Route path="records/new" element={<NewManufacturingRecordPage />} />
    <Route path="records/:id" element={<ManufacturingRecordDetailPage />} />
    <Route path="blueprint" element={<Navigate replace to="/manufacturing/operations" />} />
    <Route path="settings" element={<ManufacturingSettings />} />
    <Route path="*" element={<ManufacturingNotFound />} />
  </Route>
  <Route element={<Shell />}>
    <Route index element={<DashboardPage />} />
    <Route path="accounts" element={<Accounts />} />
    <Route path="accounts/new" element={<AccountForm />} />
    <Route path="accounts/:id" element={<AccountDetail />} />
    <Route path="records" element={<Records />} />
    <Route path="view/:view" element={<Records />} />
    <Route path="records/new" element={<NewRecordPage />} />
    <Route path="records/:id" element={<RecordDetail />} />
    <Route path="conflicts" element={<Conflicts />} />
    <Route path="settings" element={<Settings />} />
    <Route path="*" element={<div className="empty"><h1>Page not found</h1><Link className="button primary" to="/"><Plus size={16} />Return to workspace</Link></div>} />
  </Route>
</Routes></BrowserRouter></StrictMode>);
