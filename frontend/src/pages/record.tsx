import { useContext, useEffect, useState } from 'react';
import { ArchiveX, ArrowRight, Building2, CalendarDays, Check, CircleDollarSign, Clock3, History, Pencil, Plus } from 'lucide-react';
import { Link, useLocation, useNavigate, useOutletContext, useParams, useSearchParams } from 'react-router-dom';
import { allAccounts, RefreshContext, useResource, useSave } from '../api';
import { Alert, Back, Field, Loading, Modal, PageHeader, PaymentBadge, SaveButton, StageBadge, Timeline, TrackedForm, formValues, markFormSaved } from '../components';
import { currentStage, formatDate, labels, stages } from '../types';
import type { Account, Contact, Detail, Me, Page, Revision, SalesRecord } from '../types';
import { AccountForm } from './accounts';

export function NewRecordPage() {
  const [query] = useSearchParams();
  const [accountId, setAccountId] = useState(query.get('accountId') || '');
  const [contactId, setContactId] = useState('');
  const [accounts, setAccounts] = useState<Revision<Account>[]>([]);
  const [accountError, setAccountError] = useState('');
  const [newAccount, setNewAccount] = useState(false);
  const [notice, setNotice] = useState('');
  const { version } = useContext(RefreshContext);
  const me = useOutletContext<Me | null>();
  const navigate = useNavigate();
  const { save, busy, error } = useSave<SalesRecord>('record');
  const contacts = useResource<{ items: Revision<Contact>[] }>(accountId ? `/contacts?accountId=${accountId}` : null);
  useEffect(() => {
    const controller = new AbortController();
    setAccountError('');
    allAccounts<Account>(controller.signal).then(setAccounts).catch(e => {
      if (!controller.signal.aborted) setAccountError(e instanceof Error ? e.message : 'Could not load accounts.');
    });
    return () => controller.abort();
  }, [version]);
  return <><Back to="/records">All sales records</Back><PageHeader eyebrow="START A NEW OPPORTUNITY" title="Create a sales record." description="One enquiry, one record. Follow it all the way to payment." />
    {notice && <Alert tone="success">{notice}</Alert>}
    <TrackedForm className="panel form-panel" onSubmit={async e => {
      const form = e.currentTarget;
      const get = formValues(e);
      const result = await save('/records', {
        accountId, contactId: get('contactId') || null, productNotes: get('productNotes'),
        notes: get('notes'), acquiredOn: get('acquiredOn') || null, quotationReference: '',
      });
      if (result) { markFormSaved(form); navigate(`/records/${result.revision.entityId}`, { state: { notice: result.warning } }); }
    }}>
      <div className="form-section-title"><span>01</span><div><h2>Who is the enquiry for?</h2><p>Select an existing customer or create an account here.</p></div></div>
      <div className="form-grid">
        <Field label="Account *"><select required value={accountId} onChange={e => { setAccountId(e.target.value); setContactId(''); }}><option value="">Select a company</option>{accounts.map(a => <option key={a.entityId} value={a.entityId}>{a.data.name}{a.data.address ? ` - ${a.data.address}` : ''}</option>)}</select></Field>
        <div className="inline-new"><button type="button" className="button" onClick={() => setNewAccount(true)}><Plus size={16} />Create a new account</button></div>
        <Field label="Contact person (optional)"><select name="contactId" value={contactId} onChange={e => setContactId(e.target.value)} disabled={!accountId}><option value="">No contact selected</option>{contacts.data?.items.map(c => <option value={c.entityId} key={c.entityId}>{c.data.name}</option>)}</select></Field>
        <Field label="Lead acquired date" hint="Leave blank to use today. Enter an actual date for an older enquiry."><input name="acquiredOn" type="date" max={me?.today} /></Field>
      </div>
      {accountError && <Alert>{accountError}</Alert>}{contacts.error && <Alert>{contacts.error}</Alert>}
      <div className="form-section-title"><span>02</span><div><h2>What is the requirement?</h2><p>Keep all related parts together in plain-language notes.</p></div></div>
      <div className="form-grid">
        <Field label="Product / requirement notes" wide><textarea name="productNotes" rows={5} maxLength={10000} placeholder="Part names, part numbers, quantities, specifications, sample requirements..." /></Field>
        <Field label="General notes" wide><textarea name="notes" rows={3} maxLength={10000} placeholder="Other context or customer requests" /></Field>
      </div>
      <div className="info-note"><Check size={17} />New lead will be your first green stage. Quotation, order, and payment details come later.</div>
      {error && <Alert>{error}</Alert>}<div className="form-footer"><Link to="/records">Cancel</Link><SaveButton busy={busy}>Create sales record</SaveButton></div>
    </TrackedForm>
    {newAccount && <Modal title="Create an account" onClose={() => setNewAccount(false)}><AccountForm onCreated={row => {
      setAccounts(old => [...old.filter(a => a.entityId !== row.entityId), row]); setAccountId(row.entityId); setContactId('');
      setNewAccount(false); setNotice(`Account "${row.data.name}" is saved. Complete the enquiry below.`);
    }} /></Modal>}
  </>;
}

type Action = 'stage' | 'correction' | 'followup' | 'edit' | 'product' | 'discard';

function ActionForm({ row, action, done }: { row: Revision<SalesRecord>; action: Action; done: (warning: string | null) => void }) {
  const me = useOutletContext<Me | null>();
  const { save, busy, error, setError } = useSave<SalesRecord>('record', row.entityId, row.id);
  const data = row.data;
  const current = currentStage(data);
  const next = stages[stages.indexOf(current) + 1];
  const [target, setTarget] = useState<string>(action === 'correction' ? current : next);
  const [contactId, setContactId] = useState(data.contactId || '');
  const contacts = useResource<{ items: Revision<Contact>[] }>(action === 'edit' ? `/contacts?accountId=${data.accountId}` : null);
  return <TrackedForm onSubmit={async e => {
    const form = e.currentTarget;
    const get = formValues(e);
    let body: unknown;
    let suffix: string = action;
    try {
      if (action === 'stage' || action === 'correction') {
        suffix = 'stage';
        body = { stage: target, occurredOn: get('occurredOn') || null, reason: get('reason'),
          quotationReference: target === 'follow_up' ? get('quotationReference') : null };
      } else if (action === 'followup') {
        suffix = 'followups';
        body = { note: get('note'), occurredOn: get('occurredOn') || null, nextFollowUpOn: get('nextFollowUpOn') || null };
      } else if (action === 'product') {
        suffix = 'product-notes';
        body = { productNotes: get('productNotes') };
      } else if (action === 'discard') {
        body = { reason: get('reason') };
      } else {
        suffix = '';
        body = { accountId: data.accountId, contactId: get('contactId') || null,
          productNotes: get('productNotes'), notes: get('notes'), quotationReference: get('quotationReference') };
      }
      const result = await save(`/records/${row.entityId}${suffix ? `/${suffix}` : ''}`, body, action === 'edit' || action === 'product' ? 'PATCH' : 'POST');
      if (result) { markFormSaved(form); done(result.warning); }
    } catch (e) { setError(e instanceof Error ? e.message : 'Please check your entries.'); }
  }}>
    <div className="form-grid">
      {(action === 'stage' || action === 'correction') && <>
        <Field label={action === 'correction' ? 'Stage to correct / return to' : 'Next stage'} wide>
          <select value={target} onChange={e => setTarget(e.target.value)} disabled={action === 'stage'}>
            {(action === 'stage' ? [next] : stages.slice(0, stages.indexOf(current) + 1)).filter(Boolean).map(s => <option value={s} key={s}>{labels[s]}</option>)}
          </select>
        </Field>
        <Field label="Actual stage date" hint="If blank, today is recorded."><input name="occurredOn" type="date" max={me?.today} /></Field>
        {target === 'follow_up' && <Field label="Quotation number (optional)"><input name="quotationReference" maxLength={100} defaultValue={data.quotationReference} placeholder="e.g. Q-2026-001" /></Field>}
        {action === 'correction' && <><Field label="Correction reason *" wide><textarea name="reason" required maxLength={10000} rows={3} /></Field><div className="wide"><Alert tone="warning">Later stages will return to grey. Their old dates remain in history. This corrects a recording error; it is not a return or cancellation.</Alert></div></>}
      </>}
      {action === 'followup' && <>
        <Field label="Follow-up note *" wide><textarea name="note" required rows={4} maxLength={10000} placeholder="What was discussed? What happens next?" /></Field>
        <Field label="Actual follow-up date" hint="Leave blank to record today."><input name="occurredOn" type="date" max={me?.today} /></Field>
        <Field label="Next follow-up date" hint="Leave blank to clear the reminder."><input name="nextFollowUpOn" type="date" defaultValue={data.nextFollowUpOn || ''} /></Field>
      </>}
      {action === 'product' && <Field label="Product / requirement notes" wide><textarea name="productNotes" rows={7} maxLength={10000} defaultValue={data.productNotes} /></Field>}
      {action === 'discard' && <>
        <div className="wide"><Alert tone="warning">This record will become read-only and leave the stage/payment queues. It stays in All sales records and Discarded, with its original dates, payment state, and history. Nothing is deleted.</Alert></div>
        <Field label="Discard remark *" wide><textarea name="reason" required rows={4} maxLength={10000} placeholder="Why is this sales record being discarded?" /></Field>
      </>}
      {action === 'edit' && <>
        <Field label="Contact"><select name="contactId" value={contactId} onChange={e => setContactId(e.target.value)}><option value="">No contact</option>{contacts.data?.items.map(c => <option value={c.entityId} key={c.entityId}>{c.data.name}</option>)}</select></Field>
        <Field label="Quotation number (optional)"><input name="quotationReference" maxLength={100} defaultValue={data.quotationReference} /></Field>
        <Field label="Product notes" wide><textarea name="productNotes" rows={5} maxLength={10000} defaultValue={data.productNotes} /></Field>
        <Field label="General notes" wide><textarea name="notes" rows={3} maxLength={10000} defaultValue={data.notes} /></Field>
      </>}
    </div>
    {contacts.error && <Alert>{contacts.error}</Alert>}{error && <Alert>{error}</Alert>}
    <div className="form-footer">{action === 'discard'
      ? <button type="submit" className="button danger" disabled={busy}><ArchiveX size={16} />{busy ? 'Discarding...' : 'Discard record'}</button>
      : <SaveButton busy={busy || (action === 'edit' && (contacts.loading || !!contacts.error))}>{action === 'stage' ? 'Mark stage reached' : 'Save changes'}</SaveButton>}</div>
  </TrackedForm>;
}

function PaymentPanel({ row, note, onNoteChange, done }: {
  row: Revision<SalesRecord>; note: string; onNoteChange: (note: string) => void; done: (warning: string | null) => void;
}) {
  const record = row.data;
  const { save, busy, error } = useSave<SalesRecord>('record', row.entityId, row.id);
  const closed = currentStage(record) === 'closed';
  const persist = async (received: boolean | undefined, form: HTMLFormElement | null) => {
    const result = await save(`/records/${row.entityId}/payment`, { received, note });
    if (result) { if (form) markFormSaved(form); done(result.warning); }
  };
  if (record.discarded) return <section className="panel payment-panel">
    <div className="section-heading compact"><h2><CircleDollarSign size={20} /> Payment</h2><PaymentBadge data={record} /></div>
    <p className="pre-wrap">{record.paymentNote || 'No payment details.'}</p>
    {record.paymentReceived && <p className="help-text">Received on {formatDate(record.paymentReceivedOn)}.</p>}
    <p className="help-text">Payment state retained at discard. This record is read-only.</p>
  </section>;
  return <section className="panel payment-panel">
    <div className="section-heading compact"><h2><CircleDollarSign size={20} /> Payment</h2><PaymentBadge data={record} /></div>
    <TrackedForm data-dirty={note !== record.paymentNote ? 'true' : 'false'} onSubmit={async event => { event.preventDefault(); await persist(undefined, event.currentTarget); }}>
      <Field label="Payment details"><textarea rows={3} maxLength={10000} value={note} onChange={e => onNoteChange(e.target.value)} placeholder="Payment reference or notes (optional)" /></Field>
      {error && <Alert>{error}</Alert>}
      <div className="form-footer">
        {note !== record.paymentNote && <SaveButton busy={busy}>Save details</SaveButton>}
        <button type="button" className="button primary" disabled={busy || !record.stageDates.order_received || (record.paymentReceived && currentStage(record) !== 'delivered')}
          onClick={e => void persist(true, e.currentTarget.form)}><Check size={16} />{busy ? 'Saving...' : 'Received'}</button>
      </div>
      {record.paymentReceived && <p className="help-text">Received on {formatDate(record.paymentReceivedOn)}.</p>}
      {!record.paymentReceived && <p className="help-text">If already delivered, Received also closes the sale.</p>}
      {record.paymentReceived && !closed && <button type="button" className="text-button" disabled={busy} onClick={e => void persist(false, e.currentTarget.form)}>Undo received</button>}
    </TrackedForm>
  </section>;
}

function RecordHistory({ id }: { id: string }) {
  const [offset, setOffset] = useState(0);
  const { data, error, loading } = useResource<Page<SalesRecord>>(`/entities/record/${id}/history?offset=${offset}&limit=10`);
  return <section className="panel details-panel"><div className="section-heading compact"><h2><History size={19} /> Record history</h2><small>Original versions are retained</small></div>
    {error && <Alert>{error}</Alert>}{loading && <Loading />}
    {data?.items.map(row => <details className="history-item" key={row.id}><summary><div className="history-dot" /><div><strong>{row.action.replaceAll('_', ' ')}</strong><small>Recorded {new Date(row.createdAt).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' })} IST · {row.actor}</small>{row.reason && <p>{row.reason}</p>}</div><span>View version</span></summary>
      <div className="history-snapshot"><div><StageBadge record={row.data} /></div>
        <dl>{stages.filter(s => row.data.stageDates[s]).map(s => <div key={s}><dt>{labels[s]}</dt><dd>{formatDate(row.data.stageDates[s])}</dd></div>)}
          {row.data.stageDates.quotation_pending && <div><dt>Quotation pending (earlier workflow)</dt><dd>{formatDate(row.data.stageDates.quotation_pending)}</dd></div>}
          {row.data.stageDates.quotation_sent && <div><dt>Quotation sent (earlier workflow)</dt><dd>{formatDate(row.data.stageDates.quotation_sent)}</dd></div>}
          <div><dt>Quotation number</dt><dd>{row.data.quotationReference || 'Not added'}</dd></div></dl>
        <p className="pre-wrap">{row.data.productNotes}</p><p className="pre-wrap">{row.data.notes}</p>
        {row.data.paymentNote && <p className="pre-wrap">{row.data.paymentNote}</p>}
        {row.data.discarded && <><p>Discarded on {formatDate(row.data.discardedOn)}</p><p className="pre-wrap">{row.data.discardNote}</p></>}
      </div>
    </details>)}
    {data && data.total > 10 && <div className="pagination"><span>{offset + 1}–{Math.min(offset + 10, data.total)} of {data.total}</span><div><button disabled={!offset} onClick={() => setOffset(offset - 10)}>Previous</button><button disabled={offset + 10 >= data.total} onClick={() => setOffset(offset + 10)}>Next</button></div></div>}
  </section>;
}

export function RecordDetail() {
  const { id } = useParams();
  const location = useLocation();
  const { data, error, loading } = useResource<Detail<SalesRecord>>(`/entities/record/${id}`);
  const account = useResource<Detail<Account>>(data ? `/entities/account/${data.revision.data.accountId}` : null);
  const contact = useResource<Detail<Contact>>(data?.revision.data.contactId ? `/entities/contact/${data.revision.data.contactId}` : null);
  const [action, setAction] = useState<Action | null>(null);
  const [notice, setNotice] = useState<string | null>(location.state?.notice || null);
  const [paymentDraft, setPaymentDraft] = useState<{ entityId: string; note: string } | null>(null);
  const titles = { stage: 'Move to the next stage', correction: 'Correct stage / date', followup: 'Log a follow-up', edit: 'Edit sales record', product: 'Edit product / requirement notes', discard: 'Discard sales record' };
  return <><Back to="/records">All sales records</Back>
    {error && <Alert>{error} <Link to="/conflicts">Review conflicts</Link></Alert>}{loading && <Loading />}
    {data && (() => {
      const row = data.revision;
      const record = row.data;
      const current = currentStage(record);
      const next = stages[stages.indexOf(current) + 1];
      return <>
        <PageHeader eyebrow={`SALES RECORD · ${row.entityId.slice(0, 8).toUpperCase()}`} title={account.data?.revision.data.name || (account.error ? 'Company unavailable' : 'Loading company...')}
          description={account.data ? account.data.revision.data.address || 'Location not added' : undefined}
          action={!record.discarded && <div className="actions">
            <button className="button" onClick={() => setAction('edit')}><Pencil size={15} />Edit details</button>
            <button className="button danger" onClick={() => {
              if (paymentDraft?.entityId === row.entityId && paymentDraft.note !== record.paymentNote &&
                  !window.confirm('Payment details have unsaved changes. Continue discarding without saving them?')) return;
              setAction('discard');
            }}><ArchiveX size={15} />Discard record</button>
          </div>} />
        {notice && <Alert tone="warning">{notice}</Alert>}
        {record.discarded && <section className="panel details-panel discard-panel" aria-label="Discard details">
          <h2><ArchiveX size={19} />Discarded on {formatDate(record.discardedOn)}</h2>
          <p className="pre-wrap">{record.discardNote}</p><p className="help-text">This record is read-only. Its last sales stage, payment state, and history are preserved.</p>
        </section>}
        <section className="panel timeline-panel"><div className="section-heading"><div><h2>Your sales journey</h2><p>Green stages are reached. Every step keeps its date.</p></div><StageBadge record={record} /></div>
          <Timeline record={record} />{!record.discarded && <div className="timeline-actions"><button className="text-button muted" onClick={() => setAction('correction')}><History size={15} />Correct a stage or date</button>
            {next ? <button className="button primary" onClick={() => setAction('stage')} disabled={next === 'closed' && !record.paymentReceived}>{labels[next]}<ArrowRight size={17} /></button> : <span className="completed-label"><Check size={17} />Delivered, paid, and closed</span>}</div>}
          {!record.discarded && next === 'closed' && !record.paymentReceived && <p className="closing-note">Use Received in the payment box to close this delivered sale.</p>}
        </section>
        <div className="record-columns">
          <div><section className="panel details-panel"><h2>Enquiry details</h2><dl className="details-grid">
            <div><dt><Building2 size={14} />Account</dt><dd><Link to={`/accounts/${record.accountId}`}>{account.data?.revision.data.name || 'View account'}</Link></dd></div>
            <div><dt><CalendarDays size={14} />Lead acquired</dt><dd>{formatDate(record.stageDates.new_lead)}</dd></div>
            <div><dt>Contact person</dt><dd>{contact.data?.revision.data.name || 'Not selected'}</dd></div><div><dt>Quotation number</dt><dd>{record.quotationReference || 'Not added'}</dd></div>
          </dl>{account.error && <Alert>{account.error}</Alert>}{contact.error && <Alert>{contact.error}</Alert>}
            <div className="note-block"><div className="note-heading"><h3>Product / requirement notes</h3>
              {!record.discarded && <button className="text-button" aria-label="Edit product / requirement notes" onClick={() => setAction('product')}><Pencil size={14} />Edit</button>}
            </div><p className="pre-wrap">{record.productNotes || 'No product notes yet.'}</p></div>
            <div className="note-block"><h3>General notes</h3><p className="pre-wrap">{record.notes || 'No general notes yet.'}</p></div>
          </section>
          <section className="panel details-panel"><div className="section-heading compact"><h2><Clock3 size={19} /> Follow-ups</h2>{!record.discarded && <button className="text-button" onClick={() => setAction('followup')}><Plus size={15} />Log follow-up</button>}</div>
            <div className="followup-next"><CalendarDays size={18} /><div><small>NEXT FOLLOW-UP</small><strong>{formatDate(record.nextFollowUpOn)}</strong></div></div>
            {!record.followUps.length && <p className="muted">{record.discarded ? 'No follow-up notes were recorded.' : 'Record a conversation or set the next reminder. The sales stage stays unchanged.'}</p>}
            {[...record.followUps].reverse().map(f => <div className="followup-entry" key={f.id}><strong>{formatDate(f.occurredOn)}</strong><p className="pre-wrap">{f.note}</p><small>Entered {formatDate(f.recordedAt)}</small></div>)}
          </section></div>
          <aside><PaymentPanel key={row.id} row={row} note={paymentDraft?.entityId === row.entityId ? paymentDraft.note : record.paymentNote}
            onNoteChange={note => setPaymentDraft({ entityId: row.entityId, note })}
            done={warning => { setPaymentDraft(null); setNotice(warning); }} /><div className="audit-note"><History size={17} /><span>Last updated {formatDate(row.createdAt)}<br />Creation and later changes are retained in history.</span></div></aside>
        </div>
        <RecordHistory id={id!} />
        {action && !record.discarded && <Modal title={titles[action]} onClose={() => setAction(null)}><ActionForm key={`${action}-${row.id}`} row={row} action={action} done={warning => { if (action === 'discard') setPaymentDraft(null); setAction(null); setNotice(warning); }} /></Modal>}
      </>;
    })()}
  </>;
}
