import { AlertCircle, ArrowLeft, Check, ChevronRight, LoaderCircle, X } from 'lucide-react';
import { cloneElement, isValidElement, useEffect, useId, useRef } from 'react';
import type { FormEvent, FormHTMLAttributes, ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { currentStage, currentStageDates, formatDate, labels, stages } from './types';
import type { Meta, Revision, SalesRecord } from './types';

export function Alert({ children, tone = 'error' }: { children: ReactNode; tone?: string }) {
  return <div className={`alert ${tone}`} role={tone === 'error' ? 'alert' : 'status'}><AlertCircle size={18} /><span>{children}</span></div>;
}
export function Loading() {
  return <div className="loading"><LoaderCircle className="spin" size={22} /> Loading your workspace...</div>;
}
export function PageHeader({ eyebrow, title, description, action }: {
  eyebrow: string; title: string; description?: string; action?: ReactNode;
}) {
  return <header className="page-header"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1>
    {description && <p>{description}</p>}</div>{action}</header>;
}
export function Field({ label, children, hint, wide = false }: {
  label: string; children: ReactNode; hint?: string; wide?: boolean;
}) {
  const id = useId();
  return <div className={`field ${wide ? 'wide' : ''}`}><label htmlFor={id}>{label}</label>
    {isValidElement<{ id?: string; 'aria-describedby'?: string }>(children)
      ? cloneElement(children, { id, 'aria-describedby': hint ? `${id}-hint` : undefined }) : children}
    {hint && <small id={`${id}-hint`}>{hint}</small>}</div>;
}
export function SaveButton({ busy, children = 'Save changes' }: { busy: boolean; children?: ReactNode }) {
  return <button className="button primary" type="submit" disabled={busy}>
    {busy ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}{busy ? 'Saving...' : children}
  </button>;
}
export function TrackedForm(props: FormHTMLAttributes<HTMLFormElement>) {
  const ref = useRef<HTMLFormElement>(null);
  useEffect(() => {
    const unload = (event: BeforeUnloadEvent) => {
      if (ref.current?.dataset.dirty === 'true') { event.preventDefault(); event.returnValue = ''; }
    };
    const navigate = (event: MouseEvent) => {
      const anchor = event.target instanceof Element ? event.target.closest('a[href]') : null;
      if (anchor && ref.current?.dataset.dirty === 'true' && !event.ctrlKey && !event.metaKey &&
          !window.confirm('Leave this form and discard unsaved changes?')) {
        event.preventDefault(); event.stopImmediatePropagation();
      }
    };
    window.addEventListener('beforeunload', unload);
    document.addEventListener('click', navigate, true);
    return () => { window.removeEventListener('beforeunload', unload); document.removeEventListener('click', navigate, true); };
  }, []);
  return <form {...props} ref={ref} onChange={event => {
    if (ref.current) ref.current.dataset.dirty = 'true';
    props.onChange?.(event);
  }} />;
}
export function markFormSaved(form: HTMLFormElement) { form.dataset.dirty = 'false'; }
export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLElement>(null);
  const close = () => {
    if (!ref.current?.querySelector('form[data-dirty="true"]') || window.confirm('Discard unsaved changes?')) onClose();
  };
  useEffect(() => {
    const previous = document.activeElement;
    ref.current?.querySelector<HTMLElement>('input,select,textarea,button')?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const focusable = [...(ref.current?.querySelectorAll<HTMLElement>('input,select,textarea,button,a[href]') || [])]
        .filter(el => !el.hasAttribute('disabled'));
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { last?.focus(); event.preventDefault(); }
      else if (!event.shiftKey && document.activeElement === last) { first?.focus(); event.preventDefault(); }
    };
    document.addEventListener('keydown', keydown);
    return () => { document.removeEventListener('keydown', keydown); if (previous instanceof HTMLElement) previous.focus(); };
  }, []);
  return <div className="modal-backdrop"><section ref={ref} className="modal" role="dialog" aria-modal="true" aria-label={title}>
    <div className="modal-header"><h2>{title}</h2><button type="button" className="icon-button" aria-label="Close dialog" onClick={close}><X /></button></div>
    {children}</section></div>;
}
export function Back({ to, children }: { to: string; children: ReactNode }) {
  return <Link className="back" to={to}><ArrowLeft size={15} />{children}</Link>;
}
export function Empty({ title, text, action }: { title: string; text: string; action?: ReactNode }) {
  return <div className="empty"><div className="empty-symbol"><Check /></div><h3>{title}</h3><p>{text}</p>{action}</div>;
}
export function Completeness({ meta }: { meta: Meta }) {
  return !meta.complete ? <Alert tone="warning">Results are incomplete. {meta.conflictCount} conflicting record(s) are excluded from totals.
    {' '}<Link to="/conflicts">Review saved versions</Link></Alert> : null;
}
export function StageBadge({ record }: { record: SalesRecord }) {
  if (record.discarded) return <span className="badge discarded"><span className="badge-dot" />Discarded</span>;
  const stage = currentStage(record);
  return <span className={`badge stage-${stage}`}><span className="badge-dot" />{labels[stage]}</span>;
}
export function Timeline({ record }: { record: SalesRecord }) {
  return <ol className="stages" aria-label="Sales stages">{stages.map((stage, index) => {
    const reached = currentStageDates(record)[stage];
    const legacyGap = stage === 'follow_up' && !reached && stages.indexOf(currentStage(record)) > index;
    return <li key={stage} className={`${reached ? 'reached' : ''} ${currentStage(record) === stage ? 'current' : ''}`}>
      <div className="stage-circle">{reached ? <Check size={17} /> : index + 1}</div>
      <strong>{labels[stage]}</strong><small>{reached ? formatDate(reached) : legacyGap ? 'Not recorded (earlier workflow)' : 'Not reached'}</small>
      {currentStage(record) === stage && <span className="current-label">{record.discarded ? 'LAST STAGE' : 'CURRENT'}</span>}
    </li>;
  })}</ol>;
}
export function PaymentBadge({ data }: { data: SalesRecord }) {
  if (!data.stageDates.order_received) return <span className="muted">Not applicable</span>;
  return <span className={`badge ${data.paymentReceived ? 'paid' : 'unpaid'}`}>{data.paymentReceived ? 'Received' : 'Pending'}</span>;
}
export function RecordTable({ rows, showDiscardDetails = false }: { rows: Revision<SalesRecord>[]; showDiscardDetails?: boolean }) {
  return <div className="table-scroll"><table><thead><tr>
    <th>Company / location</th><th>Product / requirement</th><th aria-sort="descending" title="Newest lead-acquired date first">Lead acquired <span aria-hidden="true">↓</span></th><th>Current sales stage</th><th>Stage date</th><th>Next follow-up</th>
    {showDiscardDetails && <><th>Discarded on</th><th>Discard remark</th></>}<th><span className="sr-only">Open</span></th>
  </tr></thead><tbody>{rows.map(row => <tr key={row.entityId}>
    <td><Link className="record-title" to={`/records/${row.entityId}`}>{row.accountName}</Link><span className="cell-sub">{row.accountLocation || 'Location not added'}</span></td>
    <td><span className="record-summary" title={row.data.productNotes}>{row.data.productNotes || 'No product notes'}</span></td>
    <td><time dateTime={row.data.stageDates.new_lead}>{formatDate(row.data.stageDates.new_lead)}</time></td><td><StageBadge record={row.data} />
      {row.data.discarded && <span className="cell-sub">Last stage: {labels[currentStage(row.data)]}</span>}</td>
    <td>{formatDate(currentStageDates(row.data)[currentStage(row.data)])}</td>
    <td>{row.data.nextFollowUpOn ? formatDate(row.data.nextFollowUpOn) : '—'}</td>
    {showDiscardDetails && <><td>{formatDate(row.data.discardedOn)}</td><td><span className="record-summary" title={row.data.discardNote}>{row.data.discardNote}</span></td></>}
    <td><Link className="icon-button" aria-label={`Open ${row.accountName}, record ${row.entityId.slice(0, 8)}`} to={`/records/${row.entityId}`}><ChevronRight size={18} /></Link></td>
  </tr>)}</tbody></table></div>;
}
export function formValues(event: FormEvent<HTMLFormElement>) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  return (name: string) => String(form.get(name) ?? '').trim();
}
