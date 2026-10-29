import { useContext, useEffect, useState } from 'react';
import { Check, Download, History, Pencil, Plus, Trash2 } from 'lucide-react';
import { Link, useOutletContext, useSearchParams } from 'react-router-dom';
import { api, RefreshContext, usePdfDownload, useResource, useSave } from '../api';
import { Alert, Empty, Field, Loading, Modal, PageHeader, SaveButton, TrackedForm, markFormSaved } from '../components';
import { formatDate } from '../types';
import type { Account, AccountMachine, Detail, MachineModel, Me, Page, Revision, SalePart, SalesRecord, SentPart } from '../types';
import type { Product } from '../manufacturing-types';

type DirectoryPage<T> = { items: T[]; total: number; generation?: string; catalog?: { generation: string } };
type DeliveryPage = Omit<Page<SentPart>, 'items'> & {
  items: (Revision<SentPart> & { sourceRecordAvailable?: boolean })[];
};
export function useDirectory<T>(path: string | null) {
  const { version } = useContext(RefreshContext);
  const [state, setState] = useState<{ items: T[]; loading: boolean; error: string }>({ items: [], loading: !!path, error: '' });
  useEffect(() => {
    const controller = new AbortController();
    if (!path) { setState({ items: [], loading: false, error: '' }); return; }
    setState({ items: [], loading: true, error: '' });
    (async () => {
      const items: T[] = [];
      let total = 1, identity = '';
      while (items.length < total) {
        const page = await api<DirectoryPage<T>>(`${path}${path.includes('?') ? '&' : '?'}limit=200&offset=${items.length}`, { signal: controller.signal });
        const next = `${page.generation || ''}:${page.catalog?.generation || ''}`;
        if (identity && identity !== next) throw new Error('The workspace changed. Reload before continuing.');
        identity = next;
        if (!page.items.length && items.length < page.total) throw new Error('The directory changed. Refresh and retry.');
        items.push(...page.items); total = page.total;
      }
      if (!controller.signal.aborted) setState({ items, loading: false, error: '' });
    })().catch(error => { if (!controller.signal.aborted) setState({ items: [], loading: false, error: error.message }); });
    return () => controller.abort();
  }, [path, version]);
  return state;
}

export const machineLabel = (machine: AccountMachine) => `${machine.name}${machine.serialNumber ? ` - ${machine.serialNumber}` : ''}`;
const compatible = (product: Product, machine?: AccountMachine) => !machine || !product.modelIds?.length || product.modelIds.includes(machine.modelId);
const newId = () => crypto.randomUUID().replaceAll('-', '');

export function ModelsPage() {
  const models = useDirectory<Revision<MachineModel>>('/models');
  const [editing, setEditing] = useState<Revision<MachineModel> | 'new' | null>(null);
  return <><PageHeader eyebrow="SALES DIRECTORY" title="Models" description="Machine models used by your customers and spare parts."
    action={<button className="button primary" onClick={() => setEditing('new')}><Plus size={16} />New model</button>} />
    {models.error && <Alert>{models.error}</Alert>}{models.loading && <Loading />}
    {!models.loading && !models.error && <section className="panel details-panel">{!models.items.length ? <Empty title="No models yet" text="Add a machine model to connect customer machines and compatible products." />
      : <table aria-label="Models"><thead><tr><th>Model</th><th>Actions</th></tr></thead><tbody>{models.items.map(row => <tr key={row.entityId}><td>{row.data.name}</td><td><button className="text-button" onClick={() => setEditing(row)}><Pencil size={14} />Edit {row.data.name}</button></td></tr>)}</tbody></table>}</section>}
    {editing && <ModelEditor key={editing === 'new' ? 'new' : editing.id} row={editing === 'new' ? undefined : editing} close={() => setEditing(null)} />}
  </>;
}
function ModelEditor({ row, close }: { row?: Revision<MachineModel>; close: () => void }) {
  const save = useSave<MachineModel>('model', row?.entityId, row?.id);
  return <Modal title={row ? 'Edit model' : 'New model'} onClose={close}><TrackedForm onSubmit={async event => {
    event.preventDefault(); const form = event.currentTarget;
    const result = await save.save(row ? `/models/${row.entityId}` : '/models', { name: String(new FormData(form).get('name')) }, row ? 'PATCH' : 'POST');
    if (result) { markFormSaved(form); close(); }
  }}><Field label="Model name *"><input name="name" required maxLength={200} defaultValue={row?.data.name} /></Field>
    {save.error && <Alert>{save.error}</Alert>}<div className="form-footer"><SaveButton busy={save.busy}>Save model</SaveButton></div></TrackedForm></Modal>;
}

export function AccountMachines({ account }: { account: Revision<Account> }) {
  const models = useDirectory<Revision<MachineModel>>('/models');
  const [editing, setEditing] = useState<AccountMachine | 'new' | null>(null);
  return <section className="panel details-panel machine-panel"><div className="section-heading compact"><h2>Machines</h2><button className="text-button" onClick={() => setEditing('new')}><Plus size={15} />Add machine</button></div>
    {models.error && <Alert>{models.error}</Alert>}
    {!account.data.machines?.length ? <p className="muted">No machines added.</p> : <div className="table-scroll"><table aria-label="Account machines"><thead><tr><th>Machine</th><th>Model</th><th>Serial number</th><th>Year</th><th>Actions</th></tr></thead><tbody>
      {account.data.machines.map(machine => <tr key={machine.id}><td>{machine.name}</td><td>{models.items.find(m => m.entityId === machine.modelId)?.data.name || 'Loading model...'}</td><td>{machine.serialNumber || 'Not added'}</td><td>{machine.manufacturingYear || 'Not added'}</td><td><button className="text-button" onClick={() => setEditing(machine)}>Edit machine</button></td></tr>)}
    </tbody></table></div>}
    {editing && <MachineEditor key={`${account.id}-${editing === 'new' ? 'new' : editing.id}`} account={account} machine={editing === 'new' ? undefined : editing} models={models.items} close={() => setEditing(null)} />}
  </section>;
}
function MachineEditor({ account, machine, models, close }: { account: Revision<Account>; machine?: AccountMachine; models: Revision<MachineModel>[]; close: () => void }) {
  const [id] = useState(machine?.id || newId);
  const save = useSave<Account>('account', account.entityId, account.id);
  const me = useOutletContext<Me>();
  return <Modal title={machine ? 'Edit machine' : 'Add machine'} onClose={close}><TrackedForm onSubmit={async event => {
    event.preventDefault(); const form = event.currentTarget, values = new FormData(form);
    const result = await save.save(`/accounts/${account.entityId}/machines/${id}`, {
      id, name: String(values.get('name')), modelId: String(values.get('modelId')),
      serialNumber: String(values.get('serialNumber')), manufacturingYear: values.get('year') ? Number(values.get('year')) : null,
    }, 'PUT');
    if (result) { markFormSaved(form); close(); }
  }}><div className="form-grid">
    <Field label="Machine name *"><input name="name" required maxLength={200} defaultValue={machine?.name} /></Field>
    <Field label="Model *"><select name="modelId" required defaultValue={machine?.modelId || ''}><option value="">Select a model</option>{models.map(model => <option key={model.entityId} value={model.entityId}>{model.data.name}</option>)}</select></Field>
    <Field label="Serial number"><input name="serialNumber" maxLength={200} defaultValue={machine?.serialNumber} /></Field>
    <Field label="Manufacturing year"><input name="year" type="number" min={1000} max={Number(me.today.slice(0, 4))} defaultValue={machine?.manufacturingYear || ''} /></Field>
  </div>{!models.length && <p><Link to="/models">Add a model first</Link></p>}{save.error && <Alert>{save.error}</Alert>}<div className="form-footer"><SaveButton busy={save.busy}>Save machine</SaveButton></div></TrackedForm></Modal>;
}

export function PartLines({ accountId, parts, onChange, quantityRequired = true }: { accountId: string; parts: SalePart[]; onChange: (parts: SalePart[]) => void; quantityRequired?: boolean }) {
  const account = useResource<Detail<Account>>(accountId ? `/entities/account/${accountId}` : null);
  const products = useDirectory<Product>('/parts/catalog');
  const machines = account.data?.revision.data.machines || [];
  const update = (index: number, patch: Partial<SalePart>) => onChange(parts.map((part, i) => i === index ? { ...part, ...patch } : part));
  return <div className="parts-editor">{(products.error || account.error) && <Alert>{products.error || account.error}</Alert>}
    {parts.map((part, index) => {
      const selected = machines.find(machine => machine.id === part.machineId);
      return <div className="part-line" key={part.id}><Field label={`Machine ${index + 1}`}><select value={part.machineId || ''} disabled={!accountId} onChange={event => {
        const machine = machines.find(m => m.id === event.target.value);
        const current = products.items.find(product => product.id === part.productId);
        update(index, { machineId: event.target.value || null, ...(current && !compatible(current, machine) ? { productId: '' } : {}) });
      }}><option value="">Machine not specified</option>{machines.map(machine => <option key={machine.id} value={machine.id}>{machineLabel(machine)}</option>)}</select></Field>
        <Field label={`Product ${index + 1} *`}><select required value={part.productId} disabled={products.loading} onChange={event => update(index, { productId: event.target.value })}><option value="">Select a product</option>
          {products.items.filter(product => compatible(product, selected) || product.id === part.productId).map(product => <option value={product.id} key={product.id}>{product.partNumber ? `${product.partNumber} - ` : ''}{product.name}</option>)}
        </select></Field><Field label={`Quantity ${index + 1}${quantityRequired ? ' *' : ''}`}><input type="number" min={1} max={10 ** 12} step={1} required={quantityRequired} value={part.quantity ?? ''} onChange={event => update(index, { quantity: event.target.value === '' ? null : Number(event.target.value) })} /></Field>
        <button type="button" className="icon-button" aria-label={`Remove part ${index + 1}`} onClick={event => { if (event.currentTarget.form) event.currentTarget.form.dataset.dirty = 'true'; onChange(parts.filter((_, i) => i !== index)); }}><Trash2 size={17} /></button></div>;
    })}
    <button className="button" type="button" disabled={!accountId || products.loading || !!products.error || parts.length >= 100} onClick={event => {
      if (event.currentTarget.form) event.currentTarget.form.dataset.dirty = 'true';
      onChange([...parts, { id: newId(), productId: '', machineId: null }]);
    }}><Plus size={15} />Add part</button>
  </div>;
}

export function SalesPartsPanel({ row, busy = false }: { row: Revision<SalesRecord>; busy?: boolean }) {
  const [editing, setEditing] = useState(false);
  const products = useDirectory<Product>('/parts/catalog');
  const account = useResource<Detail<Account>>(`/entities/account/${row.data.accountId}`);
  return <section className="panel details-panel"><div className="section-heading compact"><h2>Parts</h2>{!row.data.discarded && <button className="text-button" disabled={busy} onClick={() => setEditing(true)}><Pencil size={15} />Edit parts</button>}</div>
    {products.error && <Alert>{products.error}</Alert>}
    {!row.data.parts?.length ? <p className="muted">No catalog parts added.</p> : <div className="table-scroll"><table aria-label="Sales parts"><thead><tr><th>Product</th><th>Machine</th><th>Quantity</th></tr></thead><tbody>{row.data.parts.map(part => <tr key={part.id}>
      <td>{products.items.find(product => product.id === part.productId)?.name || 'Loading product...'}</td><td>{account.data?.revision.data.machines?.find(m => m.id === part.machineId)?.name || 'Machine not specified'}</td><td>{part.quantity ?? 'Not recorded'}</td>
    </tr>)}</tbody></table></div>}
    {editing && <PartsEditor key={row.id} row={row} close={() => setEditing(false)} />}
  </section>;
}
function PartsEditor({ row, close }: { row: Revision<SalesRecord>; close: () => void }) {
  const [parts, setParts] = useState(row.data.parts || []);
  const save = useSave<SalesRecord>('record', row.entityId, row.id);
  const correction = row.data.partsPublished || !!row.data.stageDates.closed || row.data.inventoryTracked && !!row.data.stageDates.dispatched;
  return <Modal title="Edit parts" onClose={close}><TrackedForm onSubmit={async event => {
    event.preventDefault(); const form = event.currentTarget;
    const result = await save.save(`/records/${row.entityId}/parts`, { parts, reason: String(new FormData(form).get('reason') || '') }, 'PATCH');
    if (result) { markFormSaved(form); close(); }
  }}><PartLines accountId={row.data.accountId} parts={parts} onChange={setParts} quantityRequired={!!row.data.inventoryTracked} />
    {correction && <Field label="Correction reason *"><textarea name="reason" required maxLength={10000} /></Field>}
    {save.error && <Alert>{save.error}</Alert>}<div className="form-footer"><SaveButton busy={save.busy}>Save parts</SaveButton></div></TrackedForm></Modal>;
}

export function ManualDeliveryForm({ accountId, row, close }: { accountId?: string; row?: Revision<SentPart>; close: () => void }) {
  const accounts = useDirectory<Revision<Account>>('/accounts');
  const products = useDirectory<Product>('/parts/catalog');
  const me = useOutletContext<Me>();
  const [account, setAccount] = useState(row?.data.accountId || accountId || '');
  const [machine, setMachine] = useState(row?.data.machineId || '');
  const [product, setProduct] = useState(row?.data.productId || '');
  const [duplicate, setDuplicate] = useState(false);
  const save = useSave<SentPart>('sent_part', row?.entityId, row?.id);
  const machines = accounts.items.find(a => a.entityId === account)?.data.machines || [];
  const selected = machines.find(m => m.id === machine);
  return <Modal title={row ? 'Correct delivery' : 'Add past delivery'} onClose={close}><TrackedForm onSubmit={async event => {
    event.preventDefault(); const form = event.currentTarget, values = new FormData(form);
    const result = await save.save(row ? `/parts/history/${row.entityId}` : '/parts/history', {
      accountId: account, productId: product, machineId: machine || null, sentOn: String(values.get('sentOn')),
      reason: String(values.get('reason') || ''), acknowledgeDuplicate: duplicate,
      quantity: values.get('quantity') ? Number(values.get('quantity')) : null,
    }, row ? 'PATCH' : 'POST');
    if (result) { markFormSaved(form); close(); }
  }}><div className="form-grid">
    <Field label="Account *" wide><select required value={account} onChange={event => { setAccount(event.target.value); setMachine(''); setDuplicate(false); }}><option value="">Select a company</option>{accounts.items.map(a => <option key={a.entityId} value={a.entityId}>{a.data.name}</option>)}</select></Field>
    <Field label="Machine"><select value={machine} onChange={event => {
      const next = machines.find(m => m.id === event.target.value), current = products.items.find(p => p.id === product);
      setMachine(event.target.value); if (current && !compatible(current, next)) setProduct(''); setDuplicate(false);
    }}><option value="">Machine not specified</option>{machines.map(m => <option key={m.id} value={m.id}>{machineLabel(m)}</option>)}</select></Field>
    <Field label="Product *"><select required value={product} onChange={event => { setProduct(event.target.value); setDuplicate(false); }}><option value="">Select a product</option>{products.items.filter(p => compatible(p, selected) || p.id === row?.data.productId && machine === (row.data.machineId || '')).map(p => <option key={p.id} value={p.id}>{p.partNumber ? `${p.partNumber} - ` : ''}{p.name}</option>)}</select></Field>
    <Field label="Delivered date *"><input name="sentOn" type="date" required max={me.today} defaultValue={row?.data.sentOn || me.today} /></Field>
    <Field label="Quantity (if known)"><input name="quantity" type="number" min={1} max={10 ** 12} step={1} defaultValue={row?.data.quantity ?? ''} /></Field>
    {row && <Field label="Correction reason *" wide><textarea name="reason" required maxLength={10000} /></Field>}
  </div>{(accounts.error || products.error || save.error) && <Alert>{accounts.error || products.error || save.error}</Alert>}
    {(save.errorCode === 'duplicate_delivery' || duplicate) && <label className="duplicate-review"><input type="checkbox" checked={duplicate} onChange={event => setDuplicate(event.target.checked)} />This is a separate delivery, not a duplicate.</label>}
    <div className="form-footer"><SaveButton busy={save.busy} disabled={accounts.loading || products.loading || !!accounts.error || !!products.error}>{row ? 'Save correction' : 'Save delivery'}</SaveButton></div></TrackedForm></Modal>;
}

function DeliveryAudit({ row, close }: { row: Revision<SentPart>; close: () => void }) {
  const [offset, setOffset] = useState(0);
  const history = useResource<Page<SentPart>>(`/entities/sent_part/${row.entityId}/history?offset=${offset}&limit=10`);
  return <Modal title="Delivery history" onClose={close}>{history.error && <Alert>{history.error}</Alert>}{history.loading && <Loading />}
    {history.data?.items.map(version => <div className="followup-entry" key={version.id}><strong>{version.data.productName} - {formatDate(version.data.sentOn)}</strong>
      <p>{version.data.accountName} / {version.data.machineName || 'Machine not specified'}</p><p>{version.data.active ? 'Active' : 'Withdrawn'} / {version.action.replaceAll('_', ' ')}</p>
      <p>Quantity: {version.data.quantity ?? 'Not recorded'}</p>
      {version.reason && <p>{version.reason}</p>}<small>{new Date(version.createdAt).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' })} IST / {version.actor}</small></div>)}
    {history.data && <div className="pagination"><span>{history.data.total} saved versions</span><button disabled={!offset} onClick={() => setOffset(offset - 10)}>Previous</button><button disabled={offset + 10 >= history.data.total} onClick={() => setOffset(offset + 10)}>Next</button></div>}</Modal>;
}
function VoidForm({ row, close }: { row: Revision<SentPart>; close: () => void }) {
  const save = useSave<SentPart>('sent_part', row.entityId, row.id);
  return <Modal title="Void delivery" onClose={close}><TrackedForm onSubmit={async event => {
    event.preventDefault(); const form = event.currentTarget;
    const result = await save.save(`/parts/history/${row.entityId}/void`, { reason: String(new FormData(form).get('reason')) });
    if (result) { markFormSaved(form); close(); }
  }}><Field label="Void reason *"><textarea name="reason" required maxLength={200} /></Field>{save.error && <Alert>{save.error}</Alert>}<div className="form-footer"><SaveButton busy={save.busy}>Void delivery</SaveButton></div></TrackedForm></Modal>;
}

export function PartsHistoryPage() {
  const [params, setParams] = useSearchParams();
  const [account, setAccount] = useState(params.get('accountId') || '');
  const [machine, setMachine] = useState('');
  const [start, setStart] = useState(''), [end, setEnd] = useState('');
  const [offset, setOffset] = useState(0), [voided, setVoided] = useState(false);
  const [action, setAction] = useState<{ kind: 'new' | 'edit' | 'audit' | 'void'; row?: Revision<SentPart> } | null>(null);
  const accounts = useDirectory<Revision<Account>>('/accounts');
  const machines = accounts.items.find(a => a.entityId === account)?.data.machines || [];
  const query = new URLSearchParams({ offset: String(offset), limit: '50', includeVoided: String(voided), ...(account ? { accountId: account } : {}), ...(machine ? { machineId: machine } : {}), ...(start ? { start } : {}), ...(end ? { end } : {}) });
  const history = useResource<DeliveryPage>(`/parts/history?${query}`);
  const pdfQuery = new URLSearchParams(query); pdfQuery.delete('offset'); pdfQuery.delete('limit');
  const pdf = usePdfDownload(`/parts/history/pdf?${pdfQuery}`, 'sales-history.pdf');
  return <><PageHeader eyebrow="SALES WORKSPACE" title="Sales History" description="Delivered parts, including past deliveries added separately."
    action={<div className="actions"><button className="button" disabled={!history.data || history.loading || !!history.error || !!accounts.error || pdf.busy} onClick={pdf.download}><Download size={16} />{pdf.busy ? 'Creating PDF...' : 'Create PDF'}</button>
      <button className="button primary" disabled={pdf.busy} onClick={() => setAction({ kind: 'new' })}><Plus size={16} />Add past delivery</button></div>} />
    <section className="panel details-panel"><fieldset className="report-filters" disabled={pdf.busy}><div className="delivery-filters">
      <Field label="Account"><select value={account} onChange={event => { setAccount(event.target.value); setMachine(''); setOffset(0); setParams(event.target.value ? { accountId: event.target.value } : {}); }}><option value="">All accounts</option>{accounts.items.map(a => <option key={a.entityId} value={a.entityId}>{a.data.name}</option>)}</select></Field>
      <Field label="Machine"><select value={machine} disabled={!account} onChange={event => { setMachine(event.target.value); setOffset(0); }}><option value="">All machines</option><option value="unassigned">Machine not specified</option>{machines.map(m => <option key={m.id} value={m.id}>{machineLabel(m)}</option>)}</select></Field>
      <Field label="From"><input type="date" value={start} onChange={event => { setStart(event.target.value); setOffset(0); }} /></Field><Field label="To"><input type="date" value={end} onChange={event => { setEnd(event.target.value); setOffset(0); }} /></Field>
    </div><label className="duplicate-review"><input type="checkbox" checked={voided} onChange={event => { setVoided(event.target.checked); setOffset(0); }} />Include withdrawn entries</label></fieldset>
      {(accounts.error || history.error || pdf.error) && <Alert>{accounts.error || history.error || pdf.error}</Alert>}{history.loading && <Loading />}
      {history.data && (!history.data.items.length ? <Empty title="No deliveries found" text="Choose another date range or add a past delivery." /> : <div className="table-scroll"><table aria-label="Sales History"><thead><tr><th>Delivered</th><th>Account</th><th>Product</th><th>Machine</th><th>Quantity</th><th>Source</th><th>Actions</th></tr></thead><tbody>
        {history.data.items.map(row => <tr key={row.entityId} className={row.data.active ? '' : 'delivery-withdrawn'}><td>{formatDate(row.data.sentOn)}{!row.data.active && <span className="cell-sub">Withdrawn</span>}</td><td>{row.data.accountName}</td><td>{row.data.productName}<span className="cell-sub">{row.data.productPartNumber}</span></td><td>{row.data.machineName || 'Not specified'}<span className="cell-sub">{[row.data.machineModelName, row.data.serialNumber].filter(Boolean).join(' / ')}</span></td>
          <td>{row.data.quantity ?? 'Not recorded'}</td><td>{row.data.source === 'sale' ? row.sourceRecordAvailable
            ? <Link to={`/records/${row.data.sourceRecordId}`}>Sales record</Link>
            : <span title={`Record ${row.data.sourceRecordId}`}>Deleted sales record</span> : 'Manual'}</td>
          <td><div className="actions"><button className="text-button" onClick={() => setAction({ kind: 'audit', row })}><History size={14} />History</button>{row.data.source === 'manual' && row.data.active && <><button className="text-button" onClick={() => setAction({ kind: 'edit', row })}>Correct</button><button className="text-button danger" onClick={() => setAction({ kind: 'void', row })}>Void</button></>}</div></td></tr>)}
      </tbody></table></div>)}
      {history.data && <div className="pagination"><span>{history.data.total} deliveries</span><div><button disabled={!offset} onClick={() => setOffset(offset - 50)}>Previous</button><button disabled={offset + 50 >= history.data.total} onClick={() => setOffset(offset + 50)}>Next</button></div></div>}
    </section>
    {(action?.kind === 'new' || action?.kind === 'edit') && <ManualDeliveryForm accountId={account} row={action.row} close={() => setAction(null)} />}
    {action?.kind === 'audit' && action.row && <DeliveryAudit row={action.row} close={() => setAction(null)} />}
    {action?.kind === 'void' && action.row && <VoidForm row={action.row} close={() => setAction(null)} />}
  </>;
}

type CoveragePage = { items: (Product & { lastSentOn: string | null; machineSpecified: boolean })[]; total: number };
export function MachinePartsPage() {
  const accounts = useDirectory<Revision<Account>>('/accounts');
  const [account, setAccount] = useState(''), [machine, setMachine] = useState(''), [offset, setOffset] = useState(0);
  const pdf = usePdfDownload(`/parts/coverage/pdf?${new URLSearchParams({ accountId: account, machineId: machine })}`, 'potential-spares-machine-wise.pdf');
  const machines = accounts.items.find(a => a.entityId === account)?.data.machines || [];
  const data = useResource<CoveragePage>(account && machine ? `/parts/coverage?accountId=${account}&machineId=${machine}&offset=${offset}&limit=100` : null);
  return <><PageHeader eyebrow="SALES WORKSPACE" title="Potential Spares machine wise" description="Compatible products and their latest delivery to this account or machine."
    action={<button className="button" disabled={!account || !machine || !data.data || data.loading || !!data.error || !!accounts.error || pdf.busy} onClick={pdf.download}><Download size={16} />{pdf.busy ? 'Creating PDF...' : 'Create PDF'}</button>} />
    <section className="panel details-panel"><div className="form-grid"><Field label="Account"><select value={account} disabled={pdf.busy} onChange={event => { setAccount(event.target.value); setMachine(''); setOffset(0); }}><option value="">Select a company</option>{accounts.items.map(a => <option key={a.entityId} value={a.entityId}>{a.data.name}</option>)}</select></Field>
      <Field label="Machine"><select disabled={!account || pdf.busy} value={machine} onChange={event => { setMachine(event.target.value); setOffset(0); }}><option value="">Select a machine</option>{machines.map(m => <option key={m.id} value={m.id}>{machineLabel(m)}</option>)}</select></Field></div>
      {(accounts.error || data.error || pdf.error) && <Alert>{accounts.error || data.error || pdf.error}</Alert>}{data.loading && machine && <Loading />}
      {!machine && <p className="muted">Select an account and machine to see its compatible parts.</p>}
      {data.data && <><div className="table-scroll"><table aria-label="Potential Spares machine wise"><thead><tr><th>Part number</th><th>Product</th><th>Delivery status</th><th>Last sent</th></tr></thead><tbody>{data.data.items.map(product => <tr key={product.id} className={product.lastSentOn ? 'delivery-sent' : ''}><td>{product.partNumber || 'Not added'}</td><td><Link to={`/manufacturing/products/${product.id}`}>{product.name}</Link></td><td>{product.lastSentOn ? <span className="delivery-status"><Check size={15} />Sent<span className="cell-sub">{product.machineSpecified ? 'This machine' : 'Account delivery / machine not specified'}</span></span> : 'Not sent'}</td><td>{formatDate(product.lastSentOn)}</td></tr>)}</tbody></table></div>
        {!data.data.items.length && <p className="muted">No compatible products yet.</p>}<div className="pagination"><span>{data.data.total} compatible products</span><div><button disabled={!offset} onClick={() => setOffset(offset - 100)}>Previous</button><button disabled={offset + 100 >= data.data.total} onClick={() => setOffset(offset + 100)}>Next</button></div></div></>}
    </section>
  </>;
}
