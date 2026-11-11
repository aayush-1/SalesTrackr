import { useEffect, useId, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Download, Mail, Pencil, Plus, UserRound } from 'lucide-react';
import { Link, useLocation, useNavigate, useOutletContext, useParams } from 'react-router-dom';
import { api, usePdfDownload, useResource, useSave } from '../api';
import { Alert, Back, Completeness, Empty, Field, Loading, Modal, PageHeader, SaveButton, TrackedForm, formValues, markFormSaved } from '../components';
import type { Account, AccountMatches, Contact, Detail, Me, Page, Revision } from '../types';
import { RecordList } from './lists';
import { AccountMachines, ManualDeliveryForm } from './sales-parts';

const indiaStates = [
  'Andaman and Nicobar Islands', 'Andhra Pradesh', 'Arunachal Pradesh', 'Assam', 'Bihar', 'Chandigarh',
  'Chhattisgarh', 'Dadra and Nagar Haveli and Daman and Diu', 'Delhi', 'Goa', 'Gujarat', 'Haryana',
  'Himachal Pradesh', 'Jammu and Kashmir', 'Jharkhand', 'Karnataka', 'Kerala', 'Ladakh', 'Lakshadweep',
  'Madhya Pradesh', 'Maharashtra', 'Manipur', 'Meghalaya', 'Mizoram', 'Nagaland', 'Odisha', 'Puducherry',
  'Punjab', 'Rajasthan', 'Sikkim', 'Tamil Nadu', 'Telangana', 'Tripura', 'Uttar Pradesh', 'Uttarakhand',
  'West Bengal',
];

function cityValue(value: string, suggestions: string[]) {
  const clean = value.normalize('NFKC').trim().replace(/\s+/g, ' ');
  return suggestions.find(city => city.toLowerCase() === clean.toLowerCase()) || clean;
}

function CityInput({ id, 'aria-describedby': describedBy, value, onChange, cities, loading, failed }: {
  id?: string; 'aria-describedby'?: string; value: string; onChange: (value: string) => void;
  cities: string[]; loading: boolean; failed: boolean;
}) {
  const input = useRef<HTMLInputElement>(null);
  const listId = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(-1);
  const options = cities.filter(city => city.toLowerCase().includes(query));
  const choose = (city: string) => {
    // Button selections do not emit the input change event tracked by TrackedForm.
    if (city !== value && input.current?.form) input.current.form.dataset.dirty = 'true';
    onChange(city); setOpen(false); setActive(-1);
  };
  useEffect(() => { setActive(-1); }, [cities]);
  useEffect(() => {
    if (open && active >= 0) document.getElementById(`${listId}-${active}`)?.scrollIntoView({ block: 'nearest' });
  }, [open, active, listId]);
  return <div className="city-picker">
    <input ref={input} id={id} aria-describedby={describedBy} name="city" role="combobox" autoComplete="off"
      aria-autocomplete="list" aria-expanded={open} aria-controls={open ? listId : undefined}
      aria-activedescendant={open && options[active] ? `${listId}-${active}` : undefined}
      maxLength={200} value={value} placeholder="e.g. Mumbai"
      onFocus={() => { setQuery(''); setOpen(true); setActive(-1); }}
      onChange={e => { onChange(e.target.value); setQuery(cityValue(e.target.value, []).toLowerCase()); setOpen(true); setActive(-1); }}
      onBlur={e => { onChange(cityValue(e.target.value, cities)); setOpen(false); setActive(-1); }}
      onKeyDown={e => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault();
          const count = open ? options.length : cities.length;
          if (!open) setQuery('');
          setOpen(true);
          setActive(index => !count ? -1 : e.key === 'ArrowDown' ? (index + 1) % count : (index < 0 ? count - 1 : (index - 1 + count) % count));
        } else if (e.key === 'Enter' && open) {
          e.preventDefault();
          if (options[active]) choose(options[active]);
          else { onChange(cityValue(value, cities)); setOpen(false); }
        } else if (e.key === 'Escape' && open) {
          e.preventDefault(); e.stopPropagation(); setOpen(false); setActive(-1);
        }
      }} />
    <button type="button" className="city-picker-toggle" tabIndex={-1} aria-label="Toggle city suggestions"
      aria-expanded={open} onPointerDown={e => e.preventDefault()} onClick={() => {
        const next = !open;
        input.current?.focus(); setQuery(''); setActive(-1); setOpen(next);
      }}><ChevronDown size={16} aria-hidden="true" /></button>
    {open && <div className="city-picker-menu">
      <div id={listId} role="listbox" aria-label="Saved cities" className="city-picker-options">
        {options.map((city, index) => <button key={city} id={`${listId}-${index}`} type="button" role="option"
          tabIndex={-1} aria-selected={index === active} onPointerMove={() => setActive(index)}
          onPointerDown={e => e.preventDefault()} onClick={() => choose(city)}>{city}</button>)}
      </div>
      {!options.length && <p role="status">{loading ? 'Loading cities...' : failed ? 'Enter a city manually.'
        : query ? 'No saved cities match. You can enter a new city.' : 'No saved cities yet. Enter a city name.'}</p>}
    </div>}
  </div>;
}

function accountAddress(account: Account) {
  return [account.address, [account.city, account.state, account.country].filter(Boolean).join(', ')].filter(Boolean).join('\n');
}

export function Accounts() {
  const [search, setSearch] = useState('');
  const [city, setCity] = useState('');
  const [state, setState] = useState('');
  const [offset, setOffset] = useState(0);
  const filters = new URLSearchParams({ search, city, state });
  const { data, loading, error } = useResource<Page<Account>>(`/accounts?${filters}&offset=${offset}&limit=24`);
  const pdf = usePdfDownload(`/accounts/pdf?${filters}`, 'accounts.pdf');
  return <><PageHeader eyebrow="CUSTOMER RELATIONSHIPS" title="Your accounts." description="Create a customer once. Keep every contact and enquiry connected."
    action={<div className="actions"><button className="button" disabled={!data?.complete || loading || !!error || pdf.busy} onClick={pdf.download}><Download size={16} />{pdf.busy ? 'Creating PDF...' : 'Create PDF'}</button>
      <Link className="button primary" to="/accounts/new"><Plus size={17} />New account</Link></div>} />
    <section className="panel account-filters"><fieldset className="report-filters" disabled={pdf.busy}><div className="filters">
      <Field label="Search accounts"><input placeholder="Search company names..." value={search} onChange={e => { setSearch(e.target.value); setOffset(0); }} /></Field>
      <Field label="City"><input placeholder="Filter by city..." maxLength={200} value={city} onChange={e => { setCity(e.target.value); setOffset(0); }} /></Field>
      <Field label="State"><input placeholder="Filter by state..." maxLength={200} value={state} onChange={e => { setState(e.target.value); setOffset(0); }} /></Field>
    </div></fieldset><div className="table-caption"><small>Filter by city, state, or both. PDF includes all matching accounts and their contacts, across every page.</small>
      {(search || city || state) && <button className="text-button" disabled={pdf.busy} onClick={() => { setSearch(''); setCity(''); setState(''); setOffset(0); }}>Clear filters</button>}</div></section>
    {(error || pdf.error) && <Alert>{error || pdf.error}</Alert>}{loading && <Loading />}{data && <><Completeness meta={data} />
      {!!data.items.length && <section className="panel"><div className="table-scroll"><table aria-label="Accounts" className="account-list"><thead><tr>
        <th>Company</th><th>Location / address</th><th>GST / tax reference</th><th><span className="sr-only">Open account</span></th>
      </tr></thead><tbody>{data.items.map(row => <tr key={row.entityId}>
        <td><Link className="record-title" to={`/accounts/${row.entityId}`}>{row.data.name}</Link></td>
        <td className="account-location pre-wrap">{accountAddress(row.data) || 'Location not added'}</td><td>{row.data.taxReference || 'Not added'}</td>
        <td><Link className="icon-button" aria-label={`Open ${row.data.name}`} to={`/accounts/${row.entityId}`}><ChevronRight size={18} /></Link></td>
      </tr>)}</tbody></table></div></section>}
      {!data.items.length && <Empty title="No accounts found" text="Create a customer account to start tracking enquiries." />}
      <div className="pagination"><span>{data.total} accounts</span><div><button disabled={!offset} onClick={() => setOffset(offset - 24)}>Previous</button><button disabled={offset + 24 >= data.total} onClick={() => setOffset(offset + 24)}>Next</button></div></div>
    </>}</>;
}

export function AccountForm({ existing, onDone, onCreated }: { existing?: Revision<Account>; onDone?: () => void; onCreated?: (row: Revision<Account>) => void }) {
  const navigate = useNavigate();
  const { save, error, busy, errorCode, unconfirmed, setError } = useSave<Account>('account', existing?.entityId, existing?.id);
  const [name, setName] = useState(existing?.data.name || '');
  const [country, setCountry] = useState(existing ? existing.data.country || '' : 'India');
  const [state, setState] = useState(existing?.data.state || '');
  const [city, setCity] = useState(existing?.data.city || '');
  const [cityScope, setCityScope] = useState({ country, state });
  const cityOptions = useResource<{ cities: string[] }>(`/accounts/cities?${new URLSearchParams(cityScope)}`);
  const cityScopeChanged = cityScope.country !== country || cityScope.state !== state;
  const cities = cityScopeChanged ? [] : cityOptions.data?.cities || [];
  const india = country.trim().toLowerCase() === 'india';
  const [found, setFound] = useState<AccountMatches | null>(null);
  const [lookupError, setLookupError] = useState('');
  const [lookupVersion, setLookupVersion] = useState(0);
  const [reviewedName, setReviewedName] = useState('');
  const companyName = name.trim();
  const matches = found?.name === companyName ? found : null;
  const checking = !existing && !!companyName && !matches && !lookupError;
  const needsReview = !!matches?.total && !matches.exactCount && reviewedName !== companyName;
  useEffect(() => {
    const timer = window.setTimeout(() => setCityScope({ country, state }), 300);
    return () => window.clearTimeout(timer);
  }, [country, state]);
  useEffect(() => {
    setFound(null); setLookupError(''); setReviewedName('');
    if (existing || !companyName) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      api<AccountMatches>(`/accounts/matches?name=${encodeURIComponent(companyName)}`, { signal: controller.signal })
        .then(value => { if (!controller.signal.aborted) setFound(value); })
        .catch(e => { if (!controller.signal.aborted) setLookupError(e instanceof Error ? e.message : 'Could not check existing accounts.'); });
    }, 300);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [companyName, existing, lookupVersion]);
  useEffect(() => {
    if (errorCode === 'account_exists') setLookupVersion(version => version + 1);
  }, [errorCode]);
  const blocked = !existing && !unconfirmed && (!companyName || checking || !!lookupError || !!matches?.exactCount || needsReview);
  return <>{!existing && !onCreated && <><Back to="/accounts">All accounts</Back><PageHeader eyebrow="CUSTOMER RELATIONSHIPS" title="Create a new account." description="Add the company once, then connect its people and sales records." /></>}
    <TrackedForm className={existing || onCreated ? '' : 'panel form-panel'} onSubmit={async e => {
      const form = e.currentTarget;
      const get = formValues(e);
      if (blocked) {
        setError(matches?.exactCount ? 'This account already exists. Use an existing account below.' : 'Check and review existing account matches before creating a new account.');
        return;
      }
      const result = await save(existing ? `/accounts/${existing.entityId}` : '/accounts', {
        name: get('name'), address: get('address'), city: cityValue(get('city'), cities), state: get('state'), country: get('country'), taxReference: get('taxReference'), notes: get('notes'),
      }, existing ? 'PATCH' : 'POST');
      if (result) {
        markFormSaved(form);
        onDone?.();
        if (onCreated) onCreated(result.revision);
        else navigate(`/accounts/${result.revision.entityId}`, { state: { notice: result.warning } });
      }
    }}>
      <div className="form-grid">
        <Field label="Company name *" wide><input name="name" autoFocus required maxLength={200} value={name} onChange={e => { setName(e.target.value); if (!unconfirmed) setError(''); }} placeholder="e.g. Northstar Packaging" /></Field>
        {!existing && <div className="field wide account-duplicate-check">
          {checking && <small role="status">Checking for existing accounts...</small>}
          {lookupError && <Alert>{lookupError} <button type="button" className="text-button" onClick={() => setLookupVersion(version => version + 1)}>Retry account check</button></Alert>}
          {!!matches?.total && <section className="account-matches" role="status" aria-label="Existing company matches">
            <strong>{matches.exactCount ? 'Account already exists.' : 'Similar accounts already exist.'}</strong>
            <p>{matches.exactCount
              ? 'Capitalization, spaces and punctuation do not make a different company name. Use the existing account instead.'
              : 'Check these companies before creating a separate account.'}</p>
            <ul>{matches.items.map(match => <li key={match.revision.entityId}>
              <div><strong>{match.revision.data.name}</strong><small>{accountAddress(match.revision.data) || 'Location not added'}</small>
                {match.conflicted && <small>Conflicting versions need review.</small>}</div>
              {match.conflicted ? <Link className="button" to="/conflicts">Review versions</Link>
                : <button type="button" className="button" disabled={busy || unconfirmed} onClick={e => {
                  const form = e.currentTarget.form!;
                  const values = new FormData(form);
                  if ((String(values.get('country') || '').trim() !== 'India' ||
                      ['address', 'city', 'state', 'taxReference', 'notes'].some(field => String(values.get(field) || '').trim())) &&
                      !window.confirm('Use this existing account and discard the new account details?')) return;
                  markFormSaved(form);
                  if (onCreated) onCreated(match.revision);
                  else navigate(`/accounts/${match.revision.entityId}`);
                }}>Use this account</button>}
            </li>)}</ul>
            {matches.total > matches.items.length && <small>{matches.total - matches.items.length} more matching account(s) exist.</small>}
            {!matches.exactCount && <label className="duplicate-review"><input type="checkbox" checked={reviewedName === companyName}
              disabled={busy} onChange={e => setReviewedName(e.target.checked ? companyName : '')} />
              <span>I checked these matches; this is a different company.</span></label>}
          </section>}
        </div>}
        <Field label="Location / address" hint="Shown below the company name on its sales records." wide><textarea name="address" rows={3} maxLength={10000} defaultValue={existing?.data.address} placeholder="Street, area or office address" /></Field>
        <Field label="Country" hint="Defaults to India for new accounts; change or clear it if needed."><input name="country" maxLength={200} value={country} onChange={e => setCountry(e.target.value)} placeholder="e.g. India" /></Field>
        <Field label="State" hint={india ? 'Indian states and union territories; optional.' : 'State, province or region; optional.'}>
          {india ? <select name="state" value={state} onChange={e => setState(e.target.value)}>
            <option value="">Select a state / union territory</option>
            {state && !indiaStates.includes(state) && <option value={state}>{state} (current value)</option>}
            {indiaStates.map(value => <option key={value} value={value}>{value}</option>)}
          </select> : <input name="state" maxLength={200} value={state} onChange={e => setState(e.target.value)} placeholder="State, province or region" />}
        </Field>
        <Field label="City" hint={cityScopeChanged || cityOptions.loading ? 'Loading saved cities...' : cityOptions.error ? 'Suggestions unavailable. You can still enter a city.' : 'Choose a previously used city or enter a new one.'}>
          <CityInput value={city} onChange={setCity} cities={cities} loading={cityScopeChanged || cityOptions.loading} failed={!!cityOptions.error} />
        </Field>
        <Field label="GST / tax reference"><input name="taxReference" maxLength={100} defaultValue={existing?.data.taxReference} /></Field>
        <Field label="Account notes" wide><textarea name="notes" rows={3} maxLength={10000} defaultValue={existing?.data.notes} /></Field>
      </div>
      {error && <Alert>{error}</Alert>}<div className="form-footer"><span>Contact people can be added on the account page.</span><SaveButton busy={busy} disabled={blocked}>{existing ? 'Save account' : 'Create account'}</SaveButton></div>
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
  const [delivery, setDelivery] = useState(false);
  const me = useOutletContext<Me>();
  const [contact, setContact] = useState<Revision<Contact> | 'new' | null>(null);
  return <><Back to="/accounts">All accounts</Back>
    {account.error && <Alert>{account.error}</Alert>}{account.loading && <Loading />}
    {account.data && <>
      <PageHeader eyebrow="ACCOUNT" title={account.data.revision.data.name} description={accountAddress(account.data.revision.data) || 'Location not added'}
        action={<div className="actions"><button className="button" onClick={() => setEdit(true)}><Pencil size={15} />Edit account</button>{me.manufacturingEnabled && <button className="button" onClick={() => setDelivery(true)}><Plus size={15} />Add past delivery</button>}<Link className="button primary" to={`/records/new?accountId=${id}`}><Plus size={16} />New sales record</Link></div>} />
      {location.state?.notice && <Alert tone="warning">{location.state.notice}</Alert>}
      <div className="account-detail-grid"><section className="panel details-panel"><h2>Company details</h2><dl><dt>City</dt><dd>{account.data.revision.data.city || 'Not added'}</dd><dt>State</dt><dd>{account.data.revision.data.state || 'Not added'}</dd><dt>Country</dt><dd>{account.data.revision.data.country || 'Not added'}</dd><dt>GST / tax reference</dt><dd>{account.data.revision.data.taxReference || 'Not added'}</dd><dt>Notes</dt><dd className="pre-wrap">{account.data.revision.data.notes || 'No notes yet.'}</dd></dl></section>
        <section className="panel details-panel"><div className="section-heading compact"><h2>Contact people</h2><button className="text-button" onClick={() => setContact('new')}><Plus size={15} />Add contact</button></div>
          {contacts.error && <Alert>{contacts.error}</Alert>}{contacts.data?.items.map(row => <div className="contact-row" key={row.entityId}><UserRound size={20} /><div><strong>{row.data.name}</strong><small>{row.data.designation}</small><span><Mail size={12} /> {row.data.email || row.data.phone || 'No contact details'}</span>{row.data.email && row.data.phone && <span>{row.data.phone}</span>}</div><button className="icon-button" aria-label={`Edit ${row.data.name}`} onClick={() => setContact(row)}><Pencil size={16} /></button></div>)}
          {!contacts.loading && !contacts.error && !contacts.data?.items.length && <p className="muted">No contact people yet.</p>}
        </section></div>
      {me.manufacturingEnabled && <AccountMachines account={account.data.revision} />}
      <div className="section-title"><h2>Sales records for this account</h2></div><RecordList accountId={id} />
      {delivery && <ManualDeliveryForm accountId={id} close={() => setDelivery(false)} />}
      {edit && <Modal title="Edit account" onClose={() => setEdit(false)}><AccountForm key={account.data.revision.id} existing={account.data.revision} onDone={() => setEdit(false)} /></Modal>}
      {contact && <Modal title={contact === 'new' ? 'Add contact' : 'Edit contact'} onClose={() => setContact(null)}><ContactForm accountId={id!} existing={contact === 'new' ? undefined : contact} onDone={() => setContact(null)} /></Modal>}
    </>}
  </>;
}
