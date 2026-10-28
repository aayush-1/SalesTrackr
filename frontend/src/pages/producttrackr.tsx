import { ArrowDown, ArrowUp, ArrowUpRight, Building2, ChevronLeft, ChevronRight, Edit3, Layers, Plus, RefreshCw, Search, Star, Trash2, Users } from 'lucide-react';
import { useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { Alert, Back, Empty, Field, Loading, markFormSaved, Modal, PageHeader, SaveButton, TrackedForm } from '../components';
import { useManufacturing, useManufacturingDirectory, useManufacturingResource, useManufacturingSave } from '../manufacturing-api';
import type {
  Directory, ManufacturingDetail, ManufacturingPage, Master, Partner, PartnerAssignment, Product, ProductOperation,
} from '../manufacturing-types';
import { ConnectedManufacturingRecords } from './manufacturing-records';
import { ProductFiles } from './product-files';

export const directories = {
  products: { title: 'Products', singular: 'product', description: 'The components you build, and how they come together.', Icon: Layers },
  operations: { title: 'Operations', singular: 'operation', description: 'Reusable manufacturing activities. Suppliers are assigned separately for each product.', Icon: ArrowUpRight },
  partners: { title: 'Suppliers', singular: 'supplier', description: 'Companies and independent people who actually carry out your work.', Icon: Building2 },
  contacts: { title: 'Contacts', singular: 'contact', description: 'Your people at company suppliers, connected to the work they support.', Icon: Users },
} as const;
const fields: Record<Directory, [string, string, string?][]> = {
  products: [['name', 'Product name'], ['reference', 'Part number'], ['notes', 'Notes', 'textarea']],
  operations: [['name', 'Operation name'], ['description', 'Description', 'textarea']],
  partners: [['name', 'Supplier name'], ['type', 'Supplier type', 'type'], ['relationship', 'Relationship', 'relationship'],
    ['email', 'Email', 'email'], ['phone', 'Phone'], ['address', 'Address', 'textarea'], ['notes', 'Notes', 'textarea']],
  contacts: [['name', 'Contact name'], ['partnerId', 'Company supplier', 'partner'], ['designation', 'Role / designation'],
    ['email', 'Email', 'email'], ['phone', 'Phone']],
};
const link = (kind: Directory, id: string) => `/manufacturing/${kind}/${id}`;
function value(entity: Master, key: string) { return String((entity as unknown as Record<string, unknown>)[key] ?? ''); }

function MasterEditor({ kind, entity, partnerId, onClose, onSaved, refreshOnSave = true }: {
  kind: Directory; entity?: Master; partnerId?: string; onClose: () => void;
  onSaved?: (id: string, data: Record<string, string>) => void; refreshOnSave?: boolean;
}) {
  const [data, setData] = useState<Record<string, string>>(() => Object.fromEntries(fields[kind].map(([key]) => [
    key, entity ? value(entity, key) : key === 'type' ? 'company' : key === 'relationship' ? 'external' : key === 'partnerId' ? partnerId || '' : '',
  ])));
  const partners = useManufacturingDirectory(kind === 'contacts' ? 'partners' : null);
  const companies = partners.items.filter(p => (p as Partner).type === 'company');
  const save = useManufacturingSave({ refreshOnSave });
  const form = useRef<HTMLFormElement | null>(null);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); form.current = event.currentTarget; form.current.dataset.dirty = 'true';
    const result = await save.save(entity ? `/${kind}/${entity.id}` : `/${kind}`, data, entity?.version ?? null, entity?.id, entity ? 'PUT' : 'POST');
    if (result) { markFormSaved(form.current); onClose(); onSaved?.(result.id, data); }
  };
  const update = (key: string, next: string) => setData(old => ({ ...old, [key]: next, ...(key === 'type' && next === 'individual' ? { relationship: 'external' } : {}) }));
  return <Modal title={`${entity ? 'Edit' : 'New'} ${directories[kind].singular}`} onClose={() => { if (!save.busy && !save.unconfirmed) onClose(); }}>
    <TrackedForm onSubmit={submit}>
      {save.error && <Alert>{save.error}</Alert>}
      {partners.error && <Alert>{partners.error}</Alert>}
      <div className="form-grid">{fields[kind].map(([key, label, type]) => <Field key={key} label={label} wide={type === 'textarea'}
        hint={entity && (type === 'partner' || type === 'type') ? 'This relationship stays fixed to protect existing contacts and assignments.' : undefined}>
        {type === 'textarea' ? <textarea name={key} value={data[key]} maxLength={10000} rows={3} disabled={save.busy || save.unconfirmed} onChange={e => update(key, e.target.value)} />
          : type === 'partner' ? <select name={key} required value={data[key]} disabled={!!entity || partners.loading || save.busy || save.unconfirmed} onChange={e => update(key, e.target.value)}>
            <option value="">Select a company</option>{companies.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          : type === 'type' ? <select name={key} value={data[key]} disabled={!!entity || save.busy || save.unconfirmed} onChange={e => update(key, e.target.value)}>
            <option value="company">Company</option><option value="individual">Independent individual</option>
          </select>
          : type === 'relationship' ? <select name={key} value={data[key]} disabled={data.type === 'individual' || save.busy || save.unconfirmed} onChange={e => update(key, e.target.value)}>
            <option value="external">External supplier</option><option value="internal">Internal / in-house company</option>
          </select>
          : <input name={key} type={type === 'email' ? 'email' : 'text'} required={key === 'name'} value={data[key]} maxLength={key === 'name' ? 200 : 10000}
            disabled={save.busy || save.unconfirmed} onChange={e => update(key, e.target.value)} />}
      </Field>)}</div>
      {kind === 'contacts' && !partners.loading && !companies.length && <Alert tone="warning">Create a company in <Link to="/manufacturing/partners">Suppliers</Link> before adding its contacts. Independent people belong directly in Suppliers.</Alert>}
      <div className="form-actions"><button type="button" className="button" disabled={save.busy || save.unconfirmed} onClick={event => {
        if (event.currentTarget.form?.dataset.dirty !== 'true' || window.confirm('Discard unsaved changes?')) onClose();
      }}>Cancel</button><SaveButton busy={save.busy} disabled={kind === 'contacts' && (!data.partnerId || partners.loading || !!partners.error)}>
        {save.unconfirmed ? 'Retry same save' : entity ? 'Save changes' : `Create ${directories[kind].singular}`}
      </SaveButton></div>
    </TrackedForm>
  </Modal>;
}

export function BuildDirectory({ kind }: { kind: Directory }) {
  const [search, setSearch] = useState('');
  const [company, setCompany] = useState('');
  const [offset, setOffset] = useState(0);
  const [creating, setCreating] = useState(false);
  const navigate = useNavigate();
  const { refresh } = useManufacturing();
  const data = useManufacturingResource<ManufacturingPage>(`/${kind}?limit=50&offset=${offset}&search=${encodeURIComponent(search)}${company ? `&partnerId=${company}` : ''}`);
  const partners = useManufacturingDirectory(kind === 'contacts' ? 'partners' : null);
  const names = new Map(partners.items.map(p => [p.id, p.name]));
  const config = directories[kind];
  return <>
    <PageHeader eyebrow="PRODUCTTRACKR / DIRECTORY" title={config.title} description={config.description}
      action={<button className="button primary" onClick={() => setCreating(true)}><Plus size={16} />New {config.singular}</button>} />
    <section className="panel bt-directory">
      <div className="bt-toolbar"><label className="search"><Search size={17} /><input aria-label={`Search ${config.title.toLowerCase()}`} placeholder={kind === 'products' ? 'Search by name or part number...' : 'Search by starting name...'} value={search} onChange={e => { setSearch(e.target.value); setOffset(0); }} /></label>
        {kind === 'contacts' && <select aria-label="Filter contacts by company" value={company} onChange={e => { setCompany(e.target.value); setOffset(0); }}>
          <option value="">All company suppliers</option>{partners.items.filter(p => (p as Partner).type === 'company').map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>}
        <button className="icon-button" aria-label="Refresh directory" onClick={refresh}><RefreshCw size={17} /></button>
      </div>
      {(data.error || partners.error) && <Alert>{data.error || partners.error}</Alert>}
      {data.loading ? <Loading /> : data.data && (!data.data.items.length ? <Empty title={`No ${config.title.toLowerCase()} ${search || company ? 'match' : 'yet'}`}
        text={search || company ? kind === 'products' ? 'Try another name or part number.' : 'Try another name or company filter.' : `Add your first ${config.singular} to start connecting your manufacturing work.`} /> : <>
        <div className="table-scroll"><table><thead><tr><th>Name</th><th>{kind === 'products' ? 'Part number' : kind === 'partners' ? 'Type / relationship' : kind === 'contacts' ? 'Company supplier' : 'Description'}</th><th>{kind === 'products' ? 'Operations' : kind === 'contacts' || kind === 'partners' ? 'Contact details' : 'Updated'}</th><th><span className="sr-only">Open</span></th></tr></thead>
          <tbody>{data.data.items.map(entity => <tr key={entity.id}><td><Link className="record-title" to={link(kind, entity.id)}>{entity.name}</Link></td>
            <td>{kind === 'products' ? value(entity, 'reference') || 'Not added'
              : kind === 'partners' ? `${value(entity, 'type') === 'company' ? 'Company' : 'Individual'} · ${value(entity, 'relationship') === 'internal' ? 'In-house' : 'External'}`
              : kind === 'contacts' ? <Link to={link('partners', value(entity, 'partnerId'))}>{names.get(value(entity, 'partnerId')) || 'Company supplier'}</Link>
              : <span className="bt-text">{value(entity, 'description') || 'Not added'}</span>}</td>
            <td>{kind === 'products' ? (entity as Product).stepIds.length
              : kind === 'contacts' || kind === 'partners' ? <>{value(entity, 'email') || 'No email'}<span className="cell-sub">{value(entity, 'phone') || 'No phone'}</span></>
              : new Date(entity.updatedAt).toLocaleDateString()}</td>
            <td><Link className="icon-button" aria-label={`Open ${entity.name}`} to={link(kind, entity.id)}><ChevronRight size={18} /></Link></td>
          </tr>)}</tbody></table></div>
      </>)}
      {data.data && <div className="bt-pagination"><span>{data.data.total} {config.title.toLowerCase()}</span><div>
        <button className="icon-button" aria-label="Previous page" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 50))}><ChevronLeft size={17} /></button>
        <button className="icon-button" aria-label="Next page" disabled={offset + 50 >= data.data.total} onClick={() => setOffset(offset + 50)}><ChevronRight size={17} /></button>
      </div></div>}
    </section>
    {creating && <MasterEditor kind={kind} partnerId={company} onClose={() => setCreating(false)} onSaved={id => navigate(link(kind, id))} />}
  </>;
}

function References({ data, kind }: { data: ManufacturingDetail; kind: Directory }) {
  const supplierPage = kind === 'partners';
  const products = new Map(data.products.map(p => [p.id, p]));
  const operations = new Map(data.operations.map(o => [o.id, o]));
  const steps = new Map(data.steps.map(s => [s.id, s]));
  const partners = new Map(data.partners.map(p => [p.id, p]));
  const contacts = new Map(data.contacts.map(c => [c.id, c]));
  return <section className="panel bt-directory"><div className="bt-panel-heading"><h2>Connected product operations</h2><span>{data.assignments.length} process links</span></div>
    {!data.assignments.length ? <Empty title="No assignments yet" text="Assign this supplier or contact from a product's operation. Connections will appear here." />
      : <div className="table-scroll"><table><thead><tr><th>{supplierPage ? 'Product' : 'Product / operation'}</th><th>{supplierPage ? 'Operation' : 'Supplier'}</th><th>Contact</th><th>Assignment</th></tr></thead><tbody>
        {data.assignments.map(a => {
          const step = steps.get(a.stepId)!; const product = products.get(step.productId)!;
          return <tr key={a.id}><td><Link className="record-title" to={`${link('products', product.id)}#step-${step.id}`}>{product.name}</Link>
            {!supplierPage && <span className="cell-sub">Step {product.stepIds.indexOf(step.id) + 1} · {operations.get(step.operationId)?.name}</span>}</td>
            <td>{supplierPage ? <><Link to={link('operations', step.operationId)}>{operations.get(step.operationId)?.name}</Link>
              <span className="cell-sub">Step {product.stepIds.indexOf(step.id) + 1}</span></>
              : <Link to={link('partners', a.partnerId)}>{partners.get(a.partnerId)?.name}</Link>}</td>
            <td>{a.contactId ? <Link to={link('contacts', a.contactId)}>{contacts.get(a.contactId)?.name}</Link> : 'Not selected'}</td>
            <td>{step.primaryAssignmentId === a.id ? <span className="bt-primary"><Star size={13} />Primary</span> : 'Additional linked supplier'}</td>
          </tr>;
        })}</tbody></table></div>}
  </section>;
}

export function BuildDetail({ kind }: { kind: Directory }) {
  const { id } = useParams();
  const resource = useManufacturingResource<ManufacturingDetail>(`/${kind}/${id}`);
  const [editing, setEditing] = useState(false);
  const [addingContact, setAddingContact] = useState(false);
  const { refresh } = useManufacturing();
  if (resource.loading) return <Loading />;
  if (!resource.data) return <><Back to={`/manufacturing/${kind}`}>{directories[kind].title}</Back><Alert>{resource.error}</Alert><button className="button" onClick={refresh}>Try loading again</button></>;
  const data = resource.data, entity = data.entity;
  return <>
    <Back to={`/manufacturing/${kind}`}>{directories[kind].title}</Back>
    <PageHeader eyebrow={`PRODUCTTRACKR / ${directories[kind].singular.toUpperCase()}`} title={entity.name}
      description={kind === 'products' ? value(entity, 'reference') || 'Product manufacturing plan' : kind === 'partners' ? `${value(entity, 'type') === 'company' ? 'Company' : 'Independent individual'} · ${value(entity, 'relationship') === 'internal' ? 'In-house' : 'External supplier'}` : kind === 'contacts' ? value(entity, 'designation') || 'Company contact' : 'Reusable manufacturing operation'}
      action={<div className="bt-record-actions">{kind === 'products' && <Link className="button primary" to={`/manufacturing/records/new?productId=${entity.id}`}><Plus size={15} />New manufacturing record</Link>}
        <button className="button" onClick={() => setEditing(true)}><Edit3 size={15} />Edit {directories[kind].singular}</button></div>} />
    <section className="panel details-panel bt-master-details"><dl className="unit-settings-summary">
      {fields[kind].filter(([key]) => !['name', 'type', 'relationship'].includes(key)).map(([key, label]) => <div key={key}><dt>{label}</dt>
        <dd>{key === 'partnerId' ? <Link to={link('partners', value(entity, key))}>{data.partners.find(p => p.id === value(entity, key))?.name || 'Company supplier'}</Link> : value(entity, key) || 'Not added'}</dd></div>)}
    </dl></section>
    {kind === 'products' ? <><ProductFiles key={entity.id} productId={entity.id} /><ProductSteps data={data} /><ConnectedManufacturingRecords productId={entity.id} /></>
      : kind === 'operations' ? <section className="panel bt-directory"><div className="bt-panel-heading"><h2>Products using this operation</h2><span>{data.steps.length} steps</span></div>
        {!data.steps.length ? <Empty title="Not used yet" text="Add this operation to a product to connect its own suppliers and contacts." />
          : <div className="table-scroll"><table><thead><tr><th>Product</th><th>Step</th><th>Product-specific instructions</th></tr></thead><tbody>{data.steps.map(s => {
            const product = data.products.find(p => p.id === s.productId)!;
            return <tr key={s.id}><td><Link className="record-title" to={`${link('products', product.id)}#step-${s.id}`}>{product.name}</Link></td><td>{product.stepIds.indexOf(s.id) + 1}</td><td className="bt-text">{s.instructions || 'No instructions added'}</td></tr>;
          })}</tbody></table></div>}
      </section> : <>
        {kind === 'partners' && (entity as Partner).type === 'company' && <section className="panel bt-directory"><div className="bt-panel-heading"><h2>Company contacts</h2>
          <button className="button" onClick={() => setAddingContact(true)}><Plus size={15} />Add contact</button></div>
          {!data.contacts.length ? <Empty title="No company contacts yet" text="Add people at this company, then choose the right contact for each assignment." />
            : <div className="table-scroll"><table><thead><tr><th>Name</th><th>Role / designation</th><th>Contact details</th></tr></thead><tbody>{data.contacts.map(c => <tr key={c.id}>
              <td><Link className="record-title" to={link('contacts', c.id)}>{c.name}</Link></td><td>{c.designation || 'Not added'}</td><td>{c.email || 'No email'}<span className="cell-sub">{c.phone || 'No phone'}</span></td>
            </tr>)}</tbody></table></div>}
        </section>}
        {kind === 'partners' && <ConnectedManufacturingRecords partnerId={entity.id} />}
        <References data={data} kind={kind} />
      </>}
    {editing && <MasterEditor kind={kind} entity={entity} onClose={() => setEditing(false)} />}
    {addingContact && <MasterEditor kind="contacts" partnerId={entity.id} onClose={() => setAddingContact(false)} />}
  </>;
}

function StepEditor({ product, step, onClose }: { product: Product; step?: ProductOperation; onClose: () => void }) {
  const operations = useManufacturingDirectory('operations');
  const [operationId, setOperation] = useState(step?.operationId || '');
  const [instructions, setInstructions] = useState(step?.instructions || '');
  const [creatingOperation, setCreatingOperation] = useState(false);
  const [createdOperations, setCreatedOperations] = useState<{ id: string; name: string }[]>([]);
  const [dirty, setDirty] = useState(false);
  const save = useManufacturingSave();
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = event.currentTarget; form.dataset.dirty = 'true';
    const result = await save.save(step ? `/steps/${step.id}` : `/products/${product.id}/steps`, { operationId, instructions }, step?.version ?? product.version, step?.id, step ? 'PUT' : 'POST');
    if (result) { markFormSaved(form); onClose(); }
  };
  if (creatingOperation) return <MasterEditor kind="operations" refreshOnSave={false}
    onClose={() => setCreatingOperation(false)} onSaved={(id, data) => {
      setCreatedOperations(old => [...old, { id, name: data.name.trim() }]);
      setOperation(id); setDirty(true);
    }} />;
  const choices = [...operations.items, ...createdOperations.filter(o => !operations.items.some(existing => existing.id === o.id))];
  return <Modal title={step ? 'Edit product operation' : 'Add product operation'} onClose={() => { if (!save.busy && !save.unconfirmed) onClose(); }}>
    <TrackedForm onSubmit={submit} data-dirty={dirty ? 'true' : 'false'} onChange={() => setDirty(true)}>{(save.error || operations.error) && <Alert>{save.error || operations.error}</Alert>}
      <div className="form-grid"><Field label="Operation" wide><select required value={operationId} disabled={!!step || operations.loading || save.busy || save.unconfirmed} onChange={e => {
        if (e.target.value === '__create__') setCreatingOperation(true);
        else setOperation(e.target.value);
      }}>
        <option value="">Select an operation</option><option value="__create__">+ Create new operation...</option>
        {choices.map(o => <option key={o.id} value={o.id}>{o.name}</option>)}
      </select></Field><Field label="Product-specific instructions" wide><textarea rows={4} maxLength={10000} value={instructions} disabled={save.busy || save.unconfirmed} onChange={e => setInstructions(e.target.value)} /></Field></div>
      <p className="muted">Choose an existing operation or create one here. New operations are reusable in the <Link to="/manufacturing/operations">Operations directory</Link>. This step's suppliers stay specific to this product.</p>
      <div className="form-actions"><SaveButton busy={save.busy} disabled={!operationId || operations.loading || !!operations.error}>{save.unconfirmed ? 'Retry same save' : step ? 'Save changes' : 'Add operation'}</SaveButton></div>
    </TrackedForm>
  </Modal>;
}

function AssignmentEditor({ step, assignment, onClose }: { step: ProductOperation; assignment?: PartnerAssignment; onClose: () => void }) {
  const partners = useManufacturingDirectory('partners');
  const [partnerId, setPartner] = useState(assignment?.partnerId || '');
  const [contactId, setContact] = useState(assignment?.contactId || '');
  const [notes, setNotes] = useState(assignment?.notes || '');
  const partner = partners.items.find(p => p.id === partnerId) as Partner | undefined;
  const contacts = useManufacturingDirectory(partner?.type === 'company' ? 'contacts' : null, `&partnerId=${partnerId}`);
  const save = useManufacturingSave();
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = event.currentTarget; form.dataset.dirty = 'true';
    const result = await save.save(`/steps/${step.id}/assignments${assignment ? `/${assignment.id}` : ''}`, { partnerId, contactId: contactId || null, notes }, step.version, assignment?.id, assignment ? 'PUT' : 'POST');
    if (result) { markFormSaved(form); onClose(); }
  };
  return <Modal title={assignment ? 'Edit supplier assignment' : 'Assign manufacturing supplier'} onClose={() => { if (!save.busy && !save.unconfirmed) onClose(); }}>
    <TrackedForm onSubmit={submit}>{(save.error || partners.error || contacts.error) && <Alert>{save.error || partners.error || contacts.error}</Alert>}
      <div className="form-grid"><Field label="Manufacturing supplier" wide><select required disabled={!!assignment || partners.loading || save.busy || save.unconfirmed} value={partnerId} onChange={e => { setPartner(e.target.value); setContact(''); }}>
        <option value="">Select an actual supplier</option>{partners.items.map(p => <option key={p.id} value={p.id}>{p.name}{(p as Partner).type === 'individual' ? ' (individual)' : (p as Partner).relationship === 'internal' ? ' (in-house)' : ''}</option>)}
      </select></Field>
      {partner?.type === 'company' && <Field label="Company contact (optional)" wide><select value={contactId} disabled={contacts.loading || save.busy || save.unconfirmed} onChange={e => setContact(e.target.value)}>
        <option value="">No contact selected</option>{contacts.items.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
      </select></Field>}
      {partner?.type === 'individual' && <p className="muted">Independent individual · {[partner.email, partner.phone].filter(Boolean).join(' · ') || 'Contact details can be added in Suppliers.'}</p>}
      <Field label="Assignment notes" wide><textarea rows={3} maxLength={10000} value={notes} disabled={save.busy || save.unconfirmed} onChange={e => setNotes(e.target.value)} /></Field></div>
      <p className="muted">This is an actual assignment, not a possible supplier. The first assigned supplier becomes primary automatically.</p>
      <div className="form-actions"><SaveButton busy={save.busy} disabled={!partnerId || partners.loading || contacts.loading || !!partners.error || !!contacts.error}>{save.unconfirmed ? 'Retry same save' : assignment ? 'Save changes' : 'Assign supplier'}</SaveButton></div>
    </TrackedForm>
  </Modal>;
}

function RemoveAssignment({ step, assignment, assignments, partners, onClose }: {
  step: ProductOperation; assignment: PartnerAssignment; assignments: PartnerAssignment[]; partners: Partner[]; onClose: () => void;
}) {
  const remaining = assignments.filter(a => a.id !== assignment.id);
  const needsReplacement = step.primaryAssignmentId === assignment.id && !!remaining.length;
  const [replacementId, setReplacement] = useState('');
  const save = useManufacturingSave();
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = event.currentTarget; form.dataset.dirty = 'true';
    if (await save.save(`/steps/${step.id}/assignments/${assignment.id}/remove`, { replacementId: needsReplacement ? replacementId : null }, step.version, assignment.id)) { markFormSaved(form); onClose(); }
  };
  return <Modal title="Remove supplier assignment" onClose={() => { if (!save.busy && !save.unconfirmed) onClose(); }}>
    <TrackedForm onSubmit={submit}>{save.error && <Alert>{save.error}</Alert>}
      <p>Remove <strong>{partners.find(p => p.id === assignment.partnerId)?.name}</strong> from this operation? The supplier and its contacts stay in their directories.</p>
      {needsReplacement ? <Field label="Replacement primary supplier"><select required value={replacementId} disabled={save.busy || save.unconfirmed} onChange={e => setReplacement(e.target.value)}>
        <option value="">Choose a remaining assigned supplier</option>{remaining.map(a => <option value={a.id} key={a.id}>{partners.find(p => p.id === a.partnerId)?.name}</option>)}
      </select></Field> : !remaining.length && <p className="muted">This operation will have no assigned supplier.</p>}
      <div className="form-actions"><SaveButton busy={save.busy} disabled={needsReplacement && !replacementId}>{save.unconfirmed ? 'Retry same removal' : 'Remove assignment'}</SaveButton></div>
    </TrackedForm>
  </Modal>;
}

function ProductSteps({ data }: { data: ManufacturingDetail }) {
  const product = data.entity as Product;
  const [editor, setEditor] = useState<{ type: 'step' | 'assign' | 'remove'; step?: ProductOperation; assignment?: PartnerAssignment } | null>(null);
  const action = useManufacturingSave();
  const disabled = action.busy || action.unconfirmed;
  const reorder = (index: number, direction: number) => {
    const order = [...product.stepIds]; [order[index], order[index + direction]] = [order[index + direction], order[index]];
    void action.save(`/products/${product.id}/reorder`, { stepIds: order }, product.version, product.id);
  };
  return <section className="bt-product-steps">
    <div className="bt-section-heading"><div><h2>Manufacturing operations</h2><p>This reusable process defines the route and linked supplier choices. Actual assignments and progress belong to manufacturing records.</p></div>
      <button className="button primary" disabled={disabled} onClick={() => setEditor({ type: 'step' })}><Plus size={16} />Add operation</button></div>
    {action.error && <Alert>{action.error}{action.unconfirmed && <> <button className="text-button" disabled={action.busy} onClick={() => void action.retry()}>Retry same change</button></>}</Alert>}
    {!data.steps.length ? <section className="panel"><Empty title="Build this product's process" text="Add the operations in manufacturing order, then connect the companies and people carrying them out." /></section>
      : <div className="bt-steps">{data.steps.map((step, index) => {
        const operation = data.operations.find(o => o.id === step.operationId)!;
        const assignments = data.assignments.filter(a => a.stepId === step.id);
        return <article className="panel bt-step" key={step.id} id={`step-${step.id}`}>
          <div className="bt-step-heading"><span className="bt-step-number">{String(index + 1).padStart(2, '0')}</span><div><Link className="bt-operation-link" to={link('operations', operation.id)}>{operation.name}</Link>
            <p>{step.instructions || 'No product-specific instructions added.'}</p></div><div className="bt-step-controls">
            <button className="icon-button" disabled={disabled || !index} aria-label={`Move ${operation.name} up`} onClick={() => reorder(index, -1)}><ArrowUp size={16} /></button>
            <button className="icon-button" disabled={disabled || index === data.steps.length - 1} aria-label={`Move ${operation.name} down`} onClick={() => reorder(index, 1)}><ArrowDown size={16} /></button>
            <button className="icon-button" disabled={disabled} aria-label={`Edit ${operation.name} instructions`} onClick={() => setEditor({ type: 'step', step })}><Edit3 size={16} /></button>
          </div></div>
          {!assignments.length ? <div className="bt-unassigned">No supplier linked. Add the companies or people eligible to perform this operation.</div>
            : <div className="table-scroll"><table><thead><tr><th>Linked supplier</th><th>Company contact</th><th>Process link</th><th>Actions</th></tr></thead><tbody>{assignments.map(assignment => {
              const partner = data.partners.find(p => p.id === assignment.partnerId)!;
              const contact = data.contacts.find(c => c.id === assignment.contactId);
              const primary = step.primaryAssignmentId === assignment.id;
              return <tr key={assignment.id}><td><Link className="record-title" to={link('partners', partner.id)}>{partner.name}</Link>
                <span className="cell-sub">{partner.type === 'individual' ? 'Independent individual' : partner.relationship === 'internal' ? 'In-house company' : 'Company'}{assignment.notes && ` · ${assignment.notes}`}</span></td>
                <td>{contact ? <><Link to={link('contacts', contact.id)}>{contact.name}</Link><span className="cell-sub">{[contact.designation, contact.email, contact.phone].filter(Boolean).join(' · ')}</span></> : partner.type === 'individual' ? <span>{[partner.email, partner.phone].filter(Boolean).join(' · ') || 'On supplier record'}</span> : 'Not selected'}</td>
                <td>{primary ? <span className="bt-primary"><Star size={13} />Primary</span> : <button className="text-button" disabled={disabled} onClick={() => void action.save(`/steps/${step.id}/primary`, { assignmentId: assignment.id }, step.version, step.id)}>Make primary</button>}</td>
                <td><div className="bt-row-actions"><button className="icon-button" disabled={disabled} aria-label={`Edit ${partner.name} assignment`} onClick={() => setEditor({ type: 'assign', step, assignment })}><Edit3 size={15} /></button>
                  <button className="icon-button" disabled={disabled} aria-label={`Remove ${partner.name} assignment`} onClick={() => setEditor({ type: 'remove', step, assignment })}><Trash2 size={15} /></button></div></td>
              </tr>;
            })}</tbody></table></div>}
          <div className="bt-step-footer"><span>{assignments.length} linked supplier{assignments.length === 1 ? '' : 's'}</span><button className="button" disabled={disabled} onClick={() => setEditor({ type: 'assign', step })}><Plus size={15} />Assign supplier</button></div>
        </article>;
      })}</div>}
    {editor?.type === 'step' && <StepEditor product={product} step={editor.step} onClose={() => setEditor(null)} />}
    {editor?.type === 'assign' && <AssignmentEditor step={editor.step!} assignment={editor.assignment} onClose={() => setEditor(null)} />}
    {editor?.type === 'remove' && <RemoveAssignment step={editor.step!} assignment={editor.assignment!} assignments={data.assignments.filter(a => a.stepId === editor.step!.id)} partners={data.partners} onClose={() => setEditor(null)} />}
  </section>;
}
