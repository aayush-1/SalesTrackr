import { ArrowRight, Check, ChevronLeft, ChevronRight, Lock, Plus, RefreshCw, Search } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { Alert, Back, Empty, Field, Loading, markFormSaved, Modal, PageHeader, SaveButton, TrackedForm } from '../components';
import { useManufacturing, useManufacturingDirectory, useManufacturingResource, useManufacturingSave } from '../manufacturing-api';
import type {
  ManufacturingRecordAction, ManufacturingRecordDetail, ManufacturingRecordEvent,
  ManufacturingRecordHistory, ManufacturingRecordPage, ManufacturingRecordPreview, ManufacturingRecordStatus,
  ManufacturingRecordSummary, ManufacturingRecordView, ManufacturingWorkStep, Product, WorkAssignment,
} from '../manufacturing-types';

const labels: Record<ManufacturingRecordView | 'pending', string> = {
  active: 'Work in process', all: 'All records', ready: 'Awaiting assignment',
  in_progress: 'In progress', completed: 'Completed', cancelled: 'Cancelled', pending: 'Pending',
};
const actionLabels: Record<ManufacturingRecordAction, string> = {
  assign: 'Assign & start', complete: 'Complete operation', reassign: 'Reassign supplier',
  undo: 'Undo last completion', cancel: 'Cancel record', quantity: 'Correct quantity',
};
const views: ManufacturingRecordView[] = ['active', 'all', 'ready', 'in_progress', 'completed', 'cancelled'];
const recordLink = (id: string) => `/manufacturing/records/${id}`;
const productLink = (id: string) => `/manufacturing/products/${id}`;
function workDate(value: string | null) {
  return value ? new Date(`${value}T00:00:00Z`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }) : 'Not recorded';
}
function Status({ status }: { status: ManufacturingRecordStatus | 'pending' }) {
  return <span className={`bt-record-status ${status}`}>{labels[status]}</span>;
}
function Progress({ record }: { record: ManufacturingRecordSummary }) {
  return <div className="bt-record-progress"><span>{record.completedCount} / {record.stepCount} operations</span>
    <div role="progressbar" aria-label={`${record.number} operation progress`} aria-valuemin={0} aria-valuemax={record.stepCount} aria-valuenow={record.completedCount}>
      <span style={{ width: `${100 * record.completedCount / record.stepCount}%` }} /></div></div>;
}
function RecordTable({ records }: { records: ManufacturingRecordSummary[] }) {
  return <div className="table-scroll"><table className="bt-record-table"><thead><tr>
    <th>Record / product</th><th>Current operation</th><th>Responsible supplier</th><th>Progress</th><th>Status</th><th>Updated</th>
  </tr></thead><tbody>{records.map(record => <tr key={record.id}>
    <td><Link className="record-title" to={recordLink(record.id)}>{record.number}</Link>
      <Link className="cell-sub" to={productLink(record.productId)}>{record.productName}</Link><span className="cell-sub">{record.productPartNumber || 'No part number'} / Quantity: {record.quantity ?? 'Not recorded'}</span></td>
    <td>{record.currentOperationName || 'All operations completed'}</td>
    <td>{record.currentSupplierId ? <><Link to={`/manufacturing/suppliers/${record.currentSupplierId}`}>{record.currentSupplierName}</Link>
      {record.currentContactName && <span className="cell-sub">{record.currentContactName}</span>}</>
      : <span className="muted">{record.status === 'ready' ? 'Awaiting assignment' : 'Not assigned'}</span>}</td>
    <td><Progress record={record} /></td><td><Status status={record.status} /></td>
    <td>{new Date(record.updatedAt).toLocaleDateString()}</td>
  </tr>)}</tbody></table></div>;
}
export function ConnectedManufacturingRecords({ productId, supplierId, overview = false }: { productId?: string; supplierId?: string; overview?: boolean }) {
  const query = new URLSearchParams({ view: productId ? 'all' : 'active', ...(productId ? { productId } : {}), ...(supplierId ? { supplierId } : {}) });
  const resource = useManufacturingResource<ManufacturingRecordPage>(`/records?${query}&limit=5`);
  const title = productId ? 'Manufacturing records' : overview ? 'Current work' : 'Current manufacturing work';
  return <section className="panel bt-directory"><div className="bt-panel-heading"><h2>{title}</h2>
    <Link className="text-button" to={`/manufacturing/records?${query}`}>View all<ArrowRight size={14} /></Link></div>
    {resource.error && <Alert>{resource.error}</Alert>}
    {resource.loading ? <Loading /> : resource.data && (resource.data.items.length ? <RecordTable records={resource.data.items} />
      : <Empty title={productId ? 'No manufacturing records yet' : 'No work in process'}
        text={productId ? 'Start a record to track a real job through a copy of this process.' : 'New jobs will appear here once a manufacturing record is created.'}
        action={!supplierId && <Link className="button" to={`/manufacturing/records/new${productId ? `?productId=${productId}` : ''}`}><Plus size={15} />New manufacturing record</Link>} />)}
  </section>;
}

export function ManufacturingRecordsPage() {
  const [params, setParams] = useSearchParams();
  const pendingParams = useRef(params);
  useEffect(() => { pendingParams.current = params; }, [params]);
  const { refresh } = useManufacturing();
  const rawView = params.get('view') as ManufacturingRecordView;
  const view = views.includes(rawView) ? rawView : 'active';
  const offset = Math.max(0, Math.floor(Number(params.get('offset')) || 0));
  const search = params.get('search') || '';
  const productId = params.get('productId') || '', supplierId = params.get('supplierId') || '', operationId = params.get('operationId') || '';
  const products = useManufacturingDirectory('products'), suppliers = useManufacturingDirectory('suppliers'), operations = useManufacturingDirectory('operations');
  const query = new URLSearchParams({ view, search, offset: String(offset), limit: '50',
    ...(productId ? { productId } : {}), ...(supplierId ? { supplierId } : {}), ...(operationId ? { operationId } : {}) });
  const resource = useManufacturingResource<ManufacturingRecordPage>(`/records?${query}`);
  const change = (key: string, value: string) => {
    const next = new URLSearchParams(pendingParams.current); value ? next.set(key, value) : next.delete(key);
    if (key !== 'offset') next.delete('offset');
    pendingParams.current = next; setParams(next);
  };
  return <>
    <PageHeader eyebrow="PRODUCTTRACKR / EXECUTION" title="Manufacturing records" description="One record per job. Know the current operation, who is responsible, and what comes next."
      action={<Link className="button primary" to="/manufacturing/records/new"><Plus size={16} />New manufacturing record</Link>} />
    <section className="panel bt-directory">
      <div className="bt-record-views" role="group" aria-label="Record views">{views.map(item =>
        <button key={item} aria-pressed={view === item} onClick={() => change('view', item)}>{labels[item]}</button>)}</div>
      <div className="bt-toolbar"><label className="search"><Search size={17} /><input aria-label="Search manufacturing records" placeholder="Record number, product name or Part number..." maxLength={200}
        value={search} onChange={event => change('search', event.target.value)} /></label>
        <button className="icon-button" aria-label="Refresh manufacturing records" onClick={refresh}><RefreshCw size={17} /></button></div>
      <div className="bt-record-filters">
        <select aria-label="Filter records by product" value={productId} onChange={e => change('productId', e.target.value)} disabled={products.loading}>
          <option value="">All products</option>{products.items.map(p => <option key={p.id} value={p.id}>{p.name}{(p as Product).partNumber ? ` · ${(p as Product).partNumber}` : ''}</option>)}</select>
        <select aria-label="Filter records by operation" value={operationId} onChange={e => change('operationId', e.target.value)} disabled={operations.loading}>
          <option value="">All current operations</option>{operations.items.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
        <select aria-label="Filter records by supplier" value={supplierId} onChange={e => change('supplierId', e.target.value)} disabled={suppliers.loading}>
          <option value="">All current suppliers</option>{suppliers.items.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
      </div>
      {(resource.error || products.error || operations.error || suppliers.error) && <Alert>{resource.error || products.error || operations.error || suppliers.error}</Alert>}
      {resource.loading ? <Loading /> : resource.data && (resource.data.items.length ? <RecordTable records={resource.data.items} />
        : <Empty title="No manufacturing records in this view" text="Create a job from a saved Product process, or try another view or filter." />)}
      {resource.data && <div className="bt-pagination"><span>{resource.data.total} manufacturing records</span><div>
        <button className="icon-button" aria-label="Previous record page" disabled={!offset} onClick={() => change('offset', String(Math.max(0, offset - 50)))}><ChevronLeft size={17} /></button>
        <button className="icon-button" aria-label="Next record page" disabled={offset + 50 >= resource.data.total} onClick={() => change('offset', String(offset + 50))}><ChevronRight size={17} /></button>
      </div></div>}
    </section>
  </>;
}

export function NewManufacturingRecordPage() {
  const [params] = useSearchParams();
  const [productId, setProduct] = useState(params.get('productId') || '');
  const [notes, setNotes] = useState('');
  const [quantity, setQuantity] = useState(params.get('quantity') || '');
  const products = useManufacturingDirectory('products');
  const preview = useManufacturingResource<ManufacturingRecordPreview>(productId ? `/records/preview/${productId}` : null);
  const save = useManufacturingSave({ refreshOnSave: false });
  const { refresh } = useManufacturing();
  const navigate = useNavigate();
  const source = preview.data?.process.productId === productId ? preview.data : null;
  const locked = save.busy || save.unconfirmed;
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = event.currentTarget; form.dataset.dirty = 'true';
    if (!source && !save.unconfirmed) return;
    const result = save.unconfirmed ? await save.retry() : await save.save('/records', {
      productId, sourceToken: source!.sourceToken, notes, quantity: Number(quantity),
    }, null);
    if (result) { markFormSaved(form); refresh(); navigate(recordLink(result.id)); }
  };
  return <>
    <Back to="/manufacturing/records">Manufacturing records</Back>
    <PageHeader eyebrow="PRODUCTTRACKR / NEW JOB" title="New manufacturing record" description="Copy a Product process into an independent job. The first operation will wait for your assignment." />
    <TrackedForm className="panel details-panel bt-new-record" onSubmit={submit}>
      {(save.error || products.error) && <Alert>{save.error || products.error}</Alert>}
      <div className="form-grid"><Field label="Product process" wide><select required disabled={products.loading || locked} value={productId} onChange={e => setProduct(e.target.value)}>
        <option value="">Select a product</option>{products.items.map(p => <option key={p.id} value={p.id}>{p.name}{(p as Product).partNumber ? ` · ${(p as Product).partNumber}` : ''}</option>)}</select></Field>
        <Field label="Quantity *"><input type="number" min={1} max={10 ** 12} step={1} required value={quantity} disabled={locked} onChange={event => setQuantity(event.target.value)} /></Field>
        <Field label="Job notes" wide><textarea rows={3} maxLength={10000} placeholder="Drawing reference, job instructions or internal notes..." value={notes} disabled={locked} onChange={e => setNotes(e.target.value)} /></Field></div>
      {preview.loading && productId ? <Loading /> : preview.error ? <Alert>{preview.error} <Link to={productLink(productId)}>Open Product process</Link></Alert> : source ? <>
        <div className="bt-panel-heading"><div><h2>Copied operation route</h2><p className="muted">{source.process.productName} · Part number: {source.process.productPartNumber || 'Not added'}</p></div>
          <button type="button" className="icon-button" aria-label="Reload process preview" disabled={locked} onClick={refresh}><RefreshCw size={17} /></button></div>
        <ol className="bt-route-preview">{source.process.operations.map((step, index) => <li key={step.sourceStepId}>
          <span className="bt-step-number">{String(index + 1).padStart(2, '0')}</span><div><strong>{step.name}</strong>
            {step.instructions && <p>{step.instructions}</p>}<small>Currently linked: {source.suggestions[step.sourceStepId]?.join(', ') || 'No suppliers yet'}</small></div>
        </li>)}</ol>
        <p className="muted bt-record-explanation">Operations and instructions are copied and will not change with the Product process. New supplier choices follow its current linked suppliers. Creating this record does not start work or notify anyone.</p>
      </> : <p className="muted bt-record-explanation">Select a product to review its ordered operations before creating this job.</p>}
      <div className="form-actions">
        <SaveButton busy={save.busy} disabled={!save.unconfirmed && (!source || preview.loading || !!preview.error || !!products.error)}>
          {save.unconfirmed ? 'Retry same save' : 'Create manufacturing record'}
        </SaveButton>
        {!save.unconfirmed && preview.error && <button className="button" type="button" disabled={save.busy} onClick={refresh}>Reload process preview</button>}
      </div>
    </TrackedForm>
  </>;
}

function Responsibility({ assignment }: { assignment: WorkAssignment }) {
  return <div className="bt-work-responsibility"><Link to={`/manufacturing/suppliers/${assignment.supplierId}`}>{assignment.supplierName}</Link>
    <span>{assignment.type === 'individual' ? 'Independent individual' : assignment.relationship === 'internal' ? 'In-house company' : 'Company supplier'}</span>
    {assignment.contact && <><Link to={`/manufacturing/contacts/${assignment.contact.id}`}>{assignment.contact.name}</Link>
      <small>{[assignment.contact.designation, assignment.contact.email, assignment.contact.phone].filter(Boolean).join(' · ')}</small></>}
    {!assignment.contact && <small>{[assignment.email, assignment.phone].filter(Boolean).join(' · ')}</small>}
  </div>;
}

function RecordActionForm({ detail, action, undoStep, onClose, onSaved }: {
  detail: ManufacturingRecordDetail; action: ManufacturingRecordAction; undoStep: ManufacturingWorkStep | null;
  onClose: () => void; onSaved: () => void;
}) {
  const record = detail.record, current = record.currentIndex === null ? null : record.operations[record.currentIndex];
  const isAssignment = action === 'assign' || action === 'reassign';
  const existingSupplier = action === 'reassign' ? current?.assignment?.supplierId || '' : '';
  const [supplierId, setSupplier] = useState(detail.eligibleSuppliers.some(p => p.supplier.id === existingSupplier) ? existingSupplier : '');
  const [contactId, setContact] = useState(supplierId && action === 'reassign' ? current?.assignment?.contact?.id || '' : '');
  const [occurredOn, setDate] = useState(detail.today), [note, setNote] = useState(''), [reason, setReason] = useState('');
  const [quantity, setQuantity] = useState(record.quantity?.toString() || '');
  const supplier = detail.eligibleSuppliers.find(p => p.supplier.id === supplierId)?.supplier;
  const contacts = useManufacturingDirectory(isAssignment && supplier?.type === 'company' ? 'contacts' : null, `&supplierId=${supplierId}`);
  const save = useManufacturingSave({ refreshOnSave: false });
  const locked = save.busy || save.unconfirmed;
  const needsReason = action === 'reassign' || action === 'undo' || action === 'cancel' || action === 'quantity';
  const previousCompletion = record.currentIndex && record.currentIndex > 0 ? record.operations[record.currentIndex - 1].completedOn : null;
  const minimum = action === 'assign' ? previousCompletion : current?.assignedOn;
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = event.currentTarget; form.dataset.dirty = 'true';
    const data = action === 'quantity' ? { reason, quantity: Number(quantity) } : action === 'cancel' ? { reason } : action === 'undo' ? { reason, stepId: undoStep!.id }
      : { stepId: current!.id, occurredOn, note, ...(isAssignment ? { supplierId, contactId: contactId || null } : {}), ...(needsReason ? { reason } : {}) };
    const result = save.unconfirmed ? await save.retry() : await save.save(`/records/${record.id}/${action}`, data, record.version, record.id);
    if (result) { markFormSaved(form); onSaved(); }
  };
  return <Modal title={actionLabels[action]} onClose={() => { if (!locked) onClose(); }}>
    <TrackedForm onSubmit={submit}>
      <p className="bt-action-intro">{record.number} · {action === 'undo' ? undoStep?.name : current?.name}</p>
      {(save.error || contacts.error) && <Alert>{save.error || contacts.error}</Alert>}
      {action === 'cancel' && <Alert tone="warning">This job will stop. Its completed work and history stay available, but the record cannot be advanced again.</Alert>}
      {action === 'undo' && <Alert tone="warning">Reopen only this last completed operation with its recorded supplier. The earlier completion stays in history.</Alert>}
      <div className="form-grid">{isAssignment && <>
        <Field label="Responsible supplier" wide hint="Only suppliers currently linked to this source Product operation can be selected.">
          <select required disabled={locked} value={supplierId} onChange={event => { setSupplier(event.target.value); setContact(''); }}>
            <option value="">Choose a linked supplier</option>{detail.eligibleSuppliers.map(choice => <option key={choice.supplier.id} value={choice.supplier.id}>
              {choice.supplier.name}{choice.primary ? ' · Suggested primary' : ''}{choice.supplier.relationship === 'internal' ? ' (in-house)' : choice.supplier.type === 'individual' ? ' (individual)' : ''}
            </option>)}</select>
        </Field>
        {supplier?.type === 'company' && <Field label="Company contact (optional)" wide><select value={contactId} disabled={locked || contacts.loading || !!contacts.error} onChange={e => setContact(e.target.value)}>
          <option value="">No contact selected</option>{contacts.items.map(contact => <option key={contact.id} value={contact.id}>{contact.name}</option>)}</select></Field>}
      </>}
        {action === 'quantity' && <Field label="Quantity *"><input type="number" required min={1} max={10 ** 12} step={1} value={quantity} disabled={locked} onChange={event => setQuantity(event.target.value)} /></Field>}
        {(isAssignment || action === 'complete') && <>
          <Field label={action === 'complete' ? 'Completed on' : action === 'reassign' ? 'Reassigned on' : 'Started on'} wide hint="Actual work date, not the time this change is recorded.">
            <input type="date" required value={occurredOn} max={detail.today} min={minimum || undefined} disabled={locked} onChange={event => setDate(event.target.value)} />
          </Field><Field label="Operation note (optional)" wide><textarea rows={3} maxLength={10000} value={note} disabled={locked} onChange={e => setNote(e.target.value)} /></Field>
        </>}
        {needsReason && <Field label="Reason" wide><textarea required rows={3} maxLength={10000} value={reason} disabled={locked} onChange={e => setReason(e.target.value)} /></Field>}
      </div>
      <div className="form-actions"><SaveButton busy={save.busy} disabled={!save.unconfirmed && (isAssignment && (!supplierId || contacts.loading || !!contacts.error))}>
        {save.unconfirmed ? 'Retry same save' : actionLabels[action]}
      </SaveButton></div>
    </TrackedForm>
  </Modal>;
}

function HistoryEntry({ event }: { event: ManufacturingRecordEvent }) {
  return <li><span className="bt-history-dot" /><div><strong>{event.action === 'create' ? 'Record created' : actionLabels[event.action]}</strong>
    {event.operationName && <span className="cell-sub">{event.operationName}</span>}
    {event.previousAssignment && <p>Previous responsibility: {event.previousAssignment.supplierName}{event.previousAssignment.contact ? ` · ${event.previousAssignment.contact.name}` : ''}</p>}
    {event.assignment && <p>Supplier: {event.assignment.supplierName}{event.assignment.contact ? ` · ${event.assignment.contact.name}` : ''}</p>}
    {event.occurredOn && <p>Actual work date: {workDate(event.occurredOn)}</p>}
    {event.previousCompletedOn && <p>Corrected completion date: {workDate(event.previousCompletedOn)}</p>}
    {event.quantity !== undefined && <p>Quantity: {event.previousQuantity ?? 'Not recorded'} → {event.quantity}</p>}
    {(event.reason || event.note) && <p className="bt-text">{event.reason || event.note}</p>}
    {event.reason && event.note && <p className="bt-text">{event.note}</p>}
    <small>Recorded {new Date(event.recordedAt).toLocaleString()} · {event.actor} · #{event.sequence}</small>
  </div></li>;
}
function RecordHistory({ recordId }: { recordId: string }) {
  const [offset, setOffset] = useState(0);
  const resource = useManufacturingResource<ManufacturingRecordHistory>(`/records/${recordId}/history?offset=${offset}&limit=20`);
  return <section className="panel bt-directory"><div className="bt-panel-heading"><h2>Activity history</h2><span>Corrections append; nothing is erased.</span></div>
    {resource.error && <Alert>{resource.error}</Alert>}
    {resource.loading ? <Loading /> : resource.data && <>
      <ol className="bt-record-history">{resource.data.items.map(event => <HistoryEntry key={event.id} event={event} />)}</ol>
      <div className="bt-pagination"><span>{resource.data.total} events</span><div>
        <button className="icon-button" aria-label="Newer history" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 20))}><ChevronLeft size={17} /></button>
        <button className="icon-button" aria-label="Older history" disabled={offset + 20 >= resource.data.total} onClick={() => setOffset(offset + 20)}><ChevronRight size={17} /></button>
      </div></div>
    </>}
  </section>;
}

function RecordDetailContent({ detail, error, refreshing }: { detail: ManufacturingRecordDetail; error: string; refreshing: boolean }) {
  const { record } = detail;
  const { refresh } = useManufacturing();
  const [dialog, setDialog] = useState<{ action: ManufacturingRecordAction; detail: ManufacturingRecordDetail; undoStep: ManufacturingWorkStep | null } | null>(null);
  const current = record.currentIndex === null ? null : record.operations[record.currentIndex];
  const undoIndex = record.status === 'completed' ? record.operations.length - 1 : record.status === 'ready' && record.currentIndex! > 0 ? record.currentIndex! - 1 : null;
  const undoStep = undoIndex === null ? null : record.operations[undoIndex];
  const open = (action: ManufacturingRecordAction) => setDialog({ action, detail, undoStep });
  return <>
    <Back to="/manufacturing/records">Manufacturing records</Back>
    <PageHeader eyebrow="PRODUCTTRACKR / MANUFACTURING RECORD" title={record.number} description={`${record.productName}${record.productPartNumber ? ` · ${record.productPartNumber}` : ''}`}
      action={<div className="bt-record-actions"><Link className="button" to={productLink(record.productId)}>Product process<ArrowRight size={15} /></Link>
        <button className="icon-button" aria-label="Refresh manufacturing record" disabled={refreshing} onClick={refresh}><RefreshCw size={17} /></button></div>} />
    {error && <Alert>{error}</Alert>}
    <section className="panel bt-record-summary"><div><Status status={record.status} /><p>{record.productName} · Part number: {record.productPartNumber || 'Not added'}</p><p>Quantity: <strong>{record.quantity ?? 'Not recorded'}</strong></p></div>
      <Progress record={record} /><div className="bt-record-dates"><span>First started <strong>{workDate(record.startedOn)}</strong></span>
        {record.completedOn && <span>Completed <strong>{workDate(record.completedOn)}</strong></span>}</div></section>
    {record.notes && <section className="panel details-panel bt-record-notes"><h2>Job notes</h2><p className="bt-text">{record.notes}</p></section>}
    {record.status === 'cancelled' && <Alert tone="warning">Cancelled: {record.cancelReason}. This record is read-only.</Alert>}
    <div className="bt-execution-grid">
      <section className="panel bt-directory"><div className="bt-panel-heading"><h2>Operation timeline</h2><span>Copied route · sequential work</span></div>
        <ol className="bt-execution-timeline" aria-label="Manufacturing operation timeline">{record.operations.map((step, index) =>
          <li key={step.id} className={`bt-work-step ${step.status}`} aria-current={index === record.currentIndex ? 'step' : undefined}>
            <span className="bt-work-marker">{step.status === 'completed' ? <Check size={17} /> : step.status === 'pending' ? <Lock size={14} /> : index + 1}</span>
            <div><div className="bt-work-step-title"><h3>{step.name}</h3><Status status={step.status} /></div>
              {step.instructions && <p className="bt-text">{step.instructions}</p>}
              {step.assignment && <><Responsibility assignment={step.assignment} /><p className="bt-work-step-dates">Started {workDate(step.startedOn)}
                {step.assignedOn !== step.startedOn && ` · Current assignment ${workDate(step.assignedOn)}`}{step.completedOn && ` · Completed ${workDate(step.completedOn)}`}</p></>}
              {step.assignmentNote && <p className="bt-text">Assignment note: {step.assignmentNote}</p>}
              {step.completionNote && <p className="bt-text">Completion note: {step.completionNote}</p>}
              {step.status === 'pending' && <small>Locked until the preceding operation is complete.</small>}
            </div>
          </li>)}</ol>
      </section>
      <aside className="bt-current-column">
        <section className="panel bt-current-work"><span className="eyebrow">{record.active ? 'CURRENT OPERATION' : 'RECORD STATUS'}</span>
          <h2>{current?.name || 'All operations complete'}</h2><Status status={record.status} />
          {current?.assignment ? <Responsibility assignment={current.assignment} /> : record.status === 'ready' && <p>No supplier has started this operation.</p>}
          {record.status === 'ready' && <>
            {(detail.eligibilityError || !detail.eligibleSuppliers.length) && <Alert tone="warning">{detail.eligibilityError || 'No suppliers are linked to this source operation yet.'} Add a supplier in the <Link to={`${productLink(record.productId)}#step-${current!.sourceStepId}`}>Product process</Link>, then refresh this record.</Alert>}
            <button className="button primary full" disabled={refreshing || !!detail.eligibilityError || !detail.eligibleSuppliers.length} onClick={() => open('assign')}>Assign & start<ArrowRight size={15} /></button>
          </>}
          {record.status === 'in_progress' && <div className="bt-current-actions"><button className="button primary full" disabled={refreshing} onClick={() => open('complete')}><Check size={16} />Complete operation</button>
            <button className="button full" disabled={refreshing || !!detail.eligibilityError || !detail.eligibleSuppliers.length} onClick={() => open('reassign')}>Reassign supplier</button>
            {(detail.eligibilityError || !detail.eligibleSuppliers.length) && <p className="muted">New assignments need a current linked supplier. Existing work can still be completed.</p>}
          </div>}
          <p className="bt-record-explanation">Responsibility tracks workflow, not verified physical location or supplier acknowledgement. No notifications are sent.</p>
        </section>
        {(record.active || record.status === 'completed') && <section className="panel bt-record-corrections"><h3>Corrections & exceptions</h3>
          <button className="button full" disabled={refreshing} onClick={() => open('quantity')}>Correct quantity</button>
          {record.completedCount > 0 && <><button className="button full" disabled={refreshing || !undoStep} onClick={() => open('undo')}>Undo last completion</button>
            {!undoStep && <p className="muted">The next operation has started, so earlier completions are locked.</p>}</>}
          {record.active && <button className="button danger full" disabled={refreshing} onClick={() => open('cancel')}>Cancel record</button>}
          <p className="muted">A reason is required. Earlier activity remains in history.</p>
        </section>}
      </aside>
    </div>
    <RecordHistory recordId={record.id} />
    {dialog && <RecordActionForm detail={dialog.detail} action={dialog.action} undoStep={dialog.undoStep}
      onClose={() => setDialog(null)} onSaved={() => { setDialog(null); refresh(); }} />}
  </>;
}

export function ManufacturingRecordDetailPage() {
  const { id } = useParams();
  const { refresh } = useManufacturing();
  const resource = useManufacturingResource<ManufacturingRecordDetail>(`/records/${id}`, { keepPreviousData: true });
  if (!resource.data || resource.data.record.id !== id) return resource.loading ? <Loading /> : <>
    <Back to="/manufacturing/records">Manufacturing records</Back><Alert>{resource.error}</Alert><button className="button" onClick={refresh}>Try loading again</button>
  </>;
  return <RecordDetailContent key={id} detail={resource.data} error={resource.error} refreshing={resource.loading} />;
}
