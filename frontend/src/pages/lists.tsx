import { useContext, useRef, useState } from 'react';
import { ArrowDownLeft, ArrowRight, ArrowUpRight, CircleDollarSign, Leaf, MessageSquare, PackageCheck, Plus } from 'lucide-react';
import { Link, useOutletContext, useParams } from 'react-router-dom';
import { api, ApiError, generationHeaders, RefreshContext, useFormWorkspace, useResource } from '../api';
import { Alert, Completeness, Empty, Field, Loading, PageHeader, RecordTable, SaveButton } from '../components';
import { formatDate, labels, views } from '../types';
import type { Account, Dashboard, Detail, Kind, Me, Page, Revision, SalesRecord } from '../types';

export function DashboardPage() {
  const me = useOutletContext<Me | null>();
  const { data, error, loading } = useResource<Dashboard>('/dashboard');
  const recent = useResource<Page<SalesRecord>>('/records?limit=5');
  const cards = [
    ['leads', 'New leads', Leaf, 'teal', 'New enquiries to follow up'],
    ['followups', 'Follow-ups', MessageSquare, 'orange', 'Sales conversations in progress'],
    ['orders', 'Order in hand', PackageCheck, 'blue', 'Confirmed, awaiting dispatch'],
    ['pending', 'Payment pending', CircleDollarSign, 'purple', 'Outstanding confirmed orders'],
  ] as const;
  return <>
    <PageHeader eyebrow={me ? formatDate(me.today).toUpperCase() : 'YOUR WORKSPACE'} title="A clear view of your sales."
      description="From the first enquiry to the final payment. Keep every next step in sight."
      action={<Link className="button primary" to="/records/new"><Plus size={17} />New sales record</Link>} />
    {error && <Alert>{error}</Alert>}{loading && <Loading />}
    {data && <>
      <Completeness meta={data} />
      <div className="stats">{cards.map(([key, label, Icon, color, hint]) =>
        <Link to={`/view/${key}`} key={key} className="stat-card"><div className="stat-top"><span>{label}</span><div className={`stat-icon ${color}`}><Icon size={20} /></div></div>
          <strong>{data.counts[key]}</strong><div className="stat-bottom">{hint}<ArrowUpRight size={16} /></div></Link>)}</div>
      <div className="overview-grid">
        <section className="panel pipeline-panel"><div className="section-heading"><div><div className="eyebrow">AT A GLANCE</div><h2>Your active pipeline</h2></div><Link to="/records">View all <ArrowRight size={15} /></Link></div>
          <div className="pipeline-summary">{[['leads', 'New leads'], ['followups', 'Follow-ups'], ['orders', 'In hand'], ['dispatched', 'Dispatched'], ['delivered', 'Delivered'], ['closed', 'Closed']].map(([key, label], i) =>
            <Link to={`/view/${key}`} className="pipeline-step" key={key}><div className={`pipeline-marker step-${i}`} /><strong>{data.counts[key]}</strong><span>{label}</span></Link>)}</div>
          <div className="panel-note"><span className="online-dot" />Current status counts · all dates · no older work hidden</div>
        </section>
        <section className="collection-card"><div className="collection-top"><span>PAYMENT CONFIRMATION</span><ArrowDownLeft size={22} /></div><h2>{data.counts.pending} pending</h2>
          <p>Confirm receipt to complete your delivered sales.</p>
          <Link to="/view/pending">Review pending payments <ArrowRight size={17} /></Link></section>
      </div>
    </>}
    <section className="panel"><div className="section-heading"><div><h2>Latest enquiries</h2><p>A quick look at your most recently acquired leads.</p></div><Link to="/records">All records <ArrowRight size={15} /></Link></div>
      {recent.error && <Alert>{recent.error}</Alert>}
      {recent.loading ? <Loading /> : recent.data?.items.length ? <RecordTable rows={recent.data.items} /> : !recent.error &&
        <Empty title="Your next opportunity starts here" text="Create an account, then add your first sales record." action={<Link className="button primary" to="/records/new">Create a sales record</Link>} />}
    </section>
  </>;
}

function exclusiveNext(value: string) {
  const date = new Date(`${value}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString().slice(0, 10);
}

export function Records() {
  const { view = 'all' } = useParams();
  return views[view] ? <RecordList key={view} view={view} /> : <Alert>Unknown sales view.</Alert>;
}

export function RecordList({ view = 'all', accountId }: { view?: string; accountId?: string }) {
  const me = useOutletContext<Me | null>();
  const spec = views[view];
  const [search, setSearch] = useState('');
  const [period, setPeriod] = useState('all');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [selectedMonth, setMonth] = useState<string | null>(null);
  const month = selectedMonth ?? me?.today.slice(0, 7) ?? '';
  const [stage, setStage] = useState('');
  const [due, setDue] = useState('all');
  const [offset, setOffset] = useState(0);
  const query = new URLSearchParams({ view, search, offset: String(offset), limit: '25', due });
  if (accountId) query.set('accountId', accountId);
  if (stage) query.set('stage', stage);
  if (period === 'today' && me) { query.set('start', me.today); query.set('end', exclusiveNext(me.today)); }
  if (period === 'day' && start) { query.set('start', start); query.set('end', exclusiveNext(start)); }
  if (period === 'range') { if (start) query.set('start', start); if (end) query.set('end', exclusiveNext(end)); }
  if (period === 'month' && month) {
    query.set('start', `${month}-01`);
    const next = new Date(`${month}-01T00:00:00Z`);
    next.setUTCMonth(next.getUTCMonth() + 1);
    query.set('end', next.toISOString().slice(0, 10));
  }
  const dateReady = period === 'all' || (period === 'today' && !!me) || (period === 'month' && !!month)
    || (period === 'day' && !!start) || (period === 'range' && !!(start || end));
  const { data, error, loading } = useResource<Page<SalesRecord>>(dateReady ? `/records?${query}` : null);
  const reset = () => setOffset(0);
  return <>
    {!accountId && <PageHeader eyebrow="SALES WORKSPACE" title={spec.title} description={spec.description}
      action={<Link className="button primary" to="/records/new"><Plus size={16} />New sales record</Link>} />}
    {dateReady && data && <Completeness meta={data} />}
    <section className="panel">
      <div className="filters">
        <Field label="Search"><input value={search} onChange={e => { setSearch(e.target.value); reset(); }} placeholder="Company, location or product notes..." /></Field>
        <Field label="Lead acquired date" hint="Filter by when the lead came in; stages show its current progress."><select value={period} onChange={e => { setPeriod(e.target.value); reset(); }}>
          <option value="all">All dates</option><option value="today">Today</option><option value="day">Selected date</option><option value="month">Selected month</option><option value="range">Custom range</option>
        </select></Field>
        {period === 'month' && <Field label="Acquired month"><input type="month" value={month} onChange={e => { setMonth(e.target.value); reset(); }} /></Field>}
        {(period === 'day' || period === 'range') && <Field label={period === 'range' ? 'Acquired from' : 'Acquired on'}><input type="date" value={start} onChange={e => { setStart(e.target.value); reset(); }} /></Field>}
        {period === 'range' && <Field label="Through (inclusive)"><input type="date" value={end} onChange={e => { setEnd(e.target.value); reset(); }} /></Field>}
        {spec.stages.length > 1 && <Field label={view === 'discarded' ? 'Last sales stage' : 'Current sales stage'}><select value={stage} onChange={e => { setStage(e.target.value); reset(); }}><option value="">All stages in this view</option>{spec.stages.map(s => <option value={s} key={s}>{labels[s]}</option>)}</select></Field>}
        {view === 'followups' && <Field label="Follow-up due"><select value={due} onChange={e => { setDue(e.target.value); reset(); }}><option value="all">Any due date</option><option value="today">Due today</option><option value="overdue">Overdue</option></select></Field>}
      </div>
      {dateReady && error && <Alert>{error}</Alert>}
      {!dateReady ? <Empty title="Choose a lead acquisition date" text="Select a date or month to see those leads and their current progress." /> : loading ? <Loading /> : data && <>
        <div className="table-caption"><span><strong>{data.total}</strong> record{data.total === 1 ? '' : 's'}</span><small>{period === 'all' ? 'All lead acquisition dates' : 'Filtered by lead acquired date'} · newest first · India time</small></div>
        {data.items.length ? <RecordTable rows={data.items} showDiscardDetails={view === 'discarded'} /> : <Empty title="Nothing in this view" text="Try another date or status, or add a new sales record." />}
        <div className="pagination"><span>{data.total ? offset + 1 : 0}–{Math.min(offset + 25, data.total)} of {data.total}</span><div><button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous</button><button disabled={offset + 25 >= data.total} onClick={() => setOffset(offset + 25)}>Next</button></div></div>
      </>}
    </section>
  </>;
}

interface Conflict { entityId: string; entityType: Kind; versions: Revision<Record<string, unknown>>[] }
export function Conflicts() {
  const { data, error, loading } = useResource<{ items: Conflict[] }>('/conflicts');
  return <><PageHeader eyebrow="DATA INTEGRITY" title="Review conflicting versions" description="Both saves are safe. Choose the version to keep current; the other remains in history." />
    {error && <Alert>{error}</Alert>}{loading && <Loading />}
    {data?.items.map(item => <ConflictCard key={item.entityId} item={item} />)}
    {data && !data.items.length && <section className="panel"><Empty title="No conflicting versions" text="Your records have one current version each." /></section>}
  </>;
}
function ConflictCard({ item }: { item: Conflict }) {
  const { refresh } = useContext(RefreshContext);
  const workspace = useFormWorkspace();
  const [selected, setSelected] = useState(item.versions[0].id);
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const pending = useRef<{ id: string; key: string } | null>(null);
  const inFlight = useRef(false);
  const accountId = item.versions[0].data.accountId;
  const account = useResource<Detail<Account>>(item.entityType === 'record' && typeof accountId === 'string' ? `/entities/account/${accountId}` : null);
  return <section className="panel conflict-card"><h2>{item.entityType} · {item.entityType === 'record' ? account.data?.revision.data.name || item.entityId : String(item.versions[0].data.name || item.entityId)}</h2>
    {item.entityType === 'record' && <p>{account.data?.revision.data.address || 'Location not added'}</p>}
    {account.error && <Alert>{account.error}</Alert>}
    <div className="conflict-versions">{item.versions.map(v => <label key={v.id} className="version-option"><span><input type="radio" name={item.entityId} checked={selected === v.id} onChange={() => setSelected(v.id)} /> Recorded {formatDate(v.createdAt)}</span><pre>{JSON.stringify(v.data, null, 2)}</pre></label>)}</div>
    <form onSubmit={async e => {
      e.preventDefault();
      if (inFlight.current) return;
      inFlight.current = true; setBusy(true); setError('');
      const key = JSON.stringify({ selected, reason, parents: item.versions.map(v => v.id) });
      try {
        const headers = generationHeaders(workspace);
        if (pending.current && pending.current.key !== key) throw new Error('Retry the original resolution before changing it.');
        if (!pending.current) {
          const prepared = await api<{ writeId: string }>('/writes/prepare', { method: 'POST', headers, body: JSON.stringify({ entityType: item.entityType, entityId: item.entityId }) });
          pending.current = { id: prepared.writeId, key };
        }
        await api(`/entities/${item.entityType}/${item.entityId}/resolve`, { method: 'POST', headers, body: JSON.stringify({ writeId: pending.current.id, parents: item.versions.map(v => v.id), selectedRevision: selected, reason }) });
        pending.current = null; refresh();
      } catch (e) {
        if (e instanceof ApiError && e.status < 500) pending.current = null;
        setError(e instanceof Error ? e.message : 'Could not resolve.');
      }
      finally { inFlight.current = false; setBusy(false); }
    }}><Field label="Resolution reason"><input required value={reason} onChange={e => setReason(e.target.value)} /></Field>
      {error && <Alert>{error}</Alert>}<SaveButton busy={busy}>Keep selected version</SaveButton></form>
  </section>;
}
