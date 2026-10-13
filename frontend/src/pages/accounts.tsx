import { useState } from 'react';
import { ChevronRight, Mail, Pencil, Plus, UserRound } from 'lucide-react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { useResource, useSave } from '../api';
import { Alert, Back, Completeness, Empty, Field, Loading, Modal, PageHeader, SaveButton, TrackedForm, formValues, markFormSaved } from '../components';
import type { Account, Contact, Detail, Page, Revision } from '../types';
import { RecordList } from './lists';

export function Accounts() {
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const { data, loading, error } = useResource<Page<Account>>(`/accounts?search=${encodeURIComponent(search)}&offset=${offset}&limit=24`);
  return <><PageHeader eyebrow="CUSTOMER RELATIONSHIPS" title="Your accounts." description="Create a customer once. Keep every contact and enquiry connected."
    action={<Link className="button primary" to="/accounts/new"><Plus size={17} />New account</Link>} />
    <div className="account-search"><input aria-label="Search accounts" placeholder="Search company names..." value={search} onChange={e => { setSearch(e.target.value); setOffset(0); }} /></div>
    {error && <Alert>{error}</Alert>}{loading && <Loading />}{data && <><Completeness meta={data} />
      {!!data.items.length && <section className="panel"><div className="table-scroll"><table aria-label="Accounts" className="account-list"><thead><tr>
        <th>Company</th><th>Location / address</th><th>GST / tax reference</th><th><span className="sr-only">Open account</span></th>
      </tr></thead><tbody>{data.items.map(row => <tr key={row.entityId}>
        <td><Link className="record-title" to={`/accounts/${row.entityId}`}>{row.data.name}</Link></td>
        <td className="account-location">{row.data.address || 'Location not added'}</td><td>{row.data.taxReference || 'Not added'}</td>
        <td><Link className="icon-button" aria-label={`Open ${row.data.name}`} to={`/accounts/${row.entityId}`}><ChevronRight size={18} /></Link></td>
      </tr>)}</tbody></table></div></section>}
      {!data.items.length && <Empty title="No accounts found" text="Create a customer account to start tracking enquiries." />}
      <div className="pagination"><span>{data.total} accounts</span><div><button disabled={!offset} onClick={() => setOffset(offset - 24)}>Previous</button><button disabled={offset + 24 >= data.total} onClick={() => setOffset(offset + 24)}>Next</button></div></div>
    </>}</>;
}

export function AccountForm({ existing, onDone, onCreated }: { existing?: Revision<Account>; onDone?: () => void; onCreated?: (row: Revision<Account>) => void }) {
  const navigate = useNavigate();
  const { save, error, busy } = useSave<Account>('account', existing?.entityId, existing?.id);
  return <>{!existing && !onCreated && <><Back to="/accounts">All accounts</Back><PageHeader eyebrow="CUSTOMER RELATIONSHIPS" title="Create a new account." description="Add the company once, then connect its people and sales records." /></>}
    <TrackedForm className={existing || onCreated ? '' : 'panel form-panel'} onSubmit={async e => {
      const form = e.currentTarget;
      const get = formValues(e);
      const result = await save(existing ? `/accounts/${existing.entityId}` : '/accounts', {
        name: get('name'), address: get('address'), taxReference: get('taxReference'), notes: get('notes'),
      }, existing ? 'PATCH' : 'POST');
      if (result) {
        markFormSaved(form);
        onDone?.();
        if (onCreated) onCreated(result.revision);
        else navigate(`/accounts/${result.revision.entityId}`, { state: { notice: result.warning } });
      }
    }}>
      <div className="form-grid">
        <Field label="Company name *" wide><input name="name" autoFocus required maxLength={200} defaultValue={existing?.data.name} placeholder="e.g. Northstar Packaging" /></Field>
        <Field label="Location / address" hint="Shown below the company name on its sales records." wide><textarea name="address" rows={3} maxLength={10000} defaultValue={existing?.data.address} placeholder="City, area or office address" /></Field>
        <Field label="GST / tax reference"><input name="taxReference" maxLength={100} defaultValue={existing?.data.taxReference} /></Field>
        <Field label="Account notes" wide><textarea name="notes" rows={3} maxLength={10000} defaultValue={existing?.data.notes} /></Field>
      </div>
      {error && <Alert>{error}</Alert>}<div className="form-footer"><span>Contact people can be added on the account page.</span><SaveButton busy={busy}>{existing ? 'Save account' : 'Create account'}</SaveButton></div>
    </TrackedForm></>;
}

function ContactForm({ accountId, existing, onDone }: { accountId: string; existing?: Revision<Contact>; onDone: () => void }) {
  const { save, busy, error } = useSave<Contact>('contact', existing?.entityId, existing?.id);
  return <TrackedForm onSubmit={async e => {
    const form = e.currentTarget;
    const get = formValues(e);
    const result = await save(existing ? `/contacts/${existing.entityId}` : '/contacts', {
      accountId, name: get('name'), email: get('email'), phone: get('phone'), designation: get('designation'),
    }, existing ? 'PATCH' : 'POST');
    if (result) { markFormSaved(form); onDone(); }
  }}><div className="form-grid">
    <Field label="Contact name *"><input name="name" required maxLength={200} defaultValue={existing?.data.name} /></Field>
    <Field label="Designation"><input name="designation" maxLength={200} defaultValue={existing?.data.designation} /></Field>
    <Field label="Email"><input name="email" type="email" maxLength={254} defaultValue={existing?.data.email} /></Field>
    <Field label="Phone"><input name="phone" type="tel" maxLength={80} defaultValue={existing?.data.phone} /></Field>
  </div>{error && <Alert>{error}</Alert>}<div className="form-footer"><SaveButton busy={busy}>Save contact</SaveButton></div></TrackedForm>;
}

export function AccountDetail() {
  const { id } = useParams();
  const location = useLocation();
  const account = useResource<Detail<Account>>(`/entities/account/${id}`);
  const contacts = useResource<{ items: Revision<Contact>[] }>(`/contacts?accountId=${id}`);
  const [edit, setEdit] = useState(false);
  const [contact, setContact] = useState<Revision<Contact> | 'new' | null>(null);
  return <><Back to="/accounts">All accounts</Back>
    {account.error && <Alert>{account.error}</Alert>}{account.loading && <Loading />}
    {account.data && <>
      <PageHeader eyebrow="ACCOUNT" title={account.data.revision.data.name} description={account.data.revision.data.address || 'Location not added'}
        action={<div className="actions"><button className="button" onClick={() => setEdit(true)}><Pencil size={15} />Edit account</button><Link className="button primary" to={`/records/new?accountId=${id}`}><Plus size={16} />New sales record</Link></div>} />
      {location.state?.notice && <Alert tone="warning">{location.state.notice}</Alert>}
      <div className="account-detail-grid"><section className="panel details-panel"><h2>Company details</h2><dl><dt>GST / tax reference</dt><dd>{account.data.revision.data.taxReference || 'Not added'}</dd><dt>Notes</dt><dd className="pre-wrap">{account.data.revision.data.notes || 'No notes yet.'}</dd></dl></section>
        <section className="panel details-panel"><div className="section-heading compact"><h2>Contact people</h2><button className="text-button" onClick={() => setContact('new')}><Plus size={15} />Add contact</button></div>
          {contacts.error && <Alert>{contacts.error}</Alert>}{contacts.data?.items.map(row => <div className="contact-row" key={row.entityId}><UserRound size={20} /><div><strong>{row.data.name}</strong><small>{row.data.designation}</small><span><Mail size={12} /> {row.data.email || row.data.phone || 'No contact details'}</span>{row.data.email && row.data.phone && <span>{row.data.phone}</span>}</div><button className="icon-button" aria-label={`Edit ${row.data.name}`} onClick={() => setContact(row)}><Pencil size={16} /></button></div>)}
          {!contacts.loading && !contacts.error && !contacts.data?.items.length && <p className="muted">No contact people yet.</p>}
        </section></div>
      <div className="section-title"><h2>Sales records for this account</h2></div><RecordList accountId={id} />
      {edit && <Modal title="Edit account" onClose={() => setEdit(false)}><AccountForm key={account.data.revision.id} existing={account.data.revision} onDone={() => setEdit(false)} /></Modal>}
      {contact && <Modal title={contact === 'new' ? 'Add contact' : 'Edit contact'} onClose={() => setContact(null)}><ContactForm accountId={id!} existing={contact === 'new' ? undefined : contact} onDone={() => setContact(null)} /></Modal>}
    </>}
  </>;
}
