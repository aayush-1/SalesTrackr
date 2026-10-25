export const stages = [
  'new_lead', 'follow_up', 'order_received',
  'dispatched', 'delivered', 'closed',
] as const;
export type Stage = typeof stages[number];
export const labels: Record<Stage, string> = {
  new_lead: 'New lead', follow_up: 'Follow-up',
  order_received: 'Order in hand', dispatched: 'Dispatched', delivered: 'Delivered', closed: 'Closed',
};
export type Kind = 'account' | 'contact' | 'record';
export interface Account { name: string; address: string; taxReference: string; notes: string }
export interface AccountMatches extends Meta {
  name: string; total: number; exactCount: number;
  items: { revision: Revision<Account>; match: 'exact' | 'similar'; conflicted: boolean }[];
}
export interface Contact { accountId: string; name: string; email: string; phone: string; designation: string }
export interface SalesRecord {
  accountId: string;
  contactId: string | null;
  productNotes: string;
  notes: string;
  quotationReference: string;
  workflowVersion?: 1 | 2 | 3;
  stageDates: Partial<Record<Stage | 'quotation_pending' | 'quotation_sent', string>>;
  discarded?: boolean;
  discardNote?: string;
  discardedOn?: string | null;
  paymentReceived: boolean;
  paymentReceivedOn: string | null;
  paymentNote: string;
  feedbackReceived?: boolean;
  feedbackReceivedOn?: string | null;
  nextFollowUpOn: string | null;
  followUps: { id: string; note: string; occurredOn: string; recordedAt: string }[];
}
export interface Revision<T> {
  id: string; entityId: string; entityType: Kind; parents: string[];
  createdAt: string; actor: string; action: string; reason: string; data: T;
  accountName?: string;
  accountLocation?: string;
}
export interface Meta { syncedAt: string | null; conflictCount: number; complete: boolean }
export interface Page<T> extends Meta { items: Revision<T>[]; total: number; dateBasis?: string }
export interface Detail<T> extends Meta { revision: Revision<T> }
export interface RecordRelations {
  account: Revision<Account> | null;
  contact: Revision<Contact> | null;
  errors: { account?: string; contact?: string };
}
export interface RecordWorkspace extends Detail<SalesRecord> { related: RecordRelations }
export interface Save<T> extends Detail<T> {
  warning: string | null;
  revisionIsCurrent?: boolean;
  related?: RecordRelations;
}
export interface Me { email: string; mode: 'demo' | 'production'; today: string }
export interface Dashboard extends Meta { counts: Record<string, number> }
export const views: Record<string, { title: string; description: string; stages: readonly Stage[] }> = {
  all: { title: 'All sales records', description: 'Every enquiry, including discarded records, ordered by lead acquired date.', stages },
  leads: { title: 'New leads', description: 'Only records currently at New lead.', stages: ['new_lead'] },
  followups: { title: 'Follow-ups', description: 'Only records currently at Follow-up, with or without a quotation number.', stages: ['follow_up'] },
  orders: { title: 'Order in hand', description: 'Confirmed orders waiting to be dispatched.', stages: ['order_received'] },
  dispatched: { title: 'Dispatched', description: 'Only records currently dispatched and awaiting delivery.', stages: ['dispatched'] },
  delivered: { title: 'Delivered', description: 'Only records currently delivered, not yet closed.', stages: ['delivered'] },
  closed: { title: 'Closed', description: 'Delivered, payment received, and complete.', stages: ['closed'] },
  pending: { title: 'Payment pending', description: 'All outstanding confirmed orders, including older and delivered orders.', stages: stages.slice(2, 5) },
  paid: { title: 'Payment received', description: 'Orders with payment receipt confirmed.', stages: stages.slice(2) },
  'feedback-pending': { title: 'Feedback pending', description: 'Closed sales records awaiting feedback. Confirm feedback receipt on the record to remove it from this list.', stages: ['closed'] },
  discarded: { title: 'Discarded', description: 'Records set aside with a remark. Original stages and history are retained.', stages },
};
export function currentStageDates(data: SalesRecord): Partial<Record<Stage, string>> {
  const dates = data.stageDates;
  if ((data.workflowVersion ?? 1) < 3 && !dates.follow_up && dates.quotation_sent) {
    return { ...dates, follow_up: dates.quotation_sent };
  }
  return dates;
}
export function currentStage(data: SalesRecord): Stage {
  const dates = currentStageDates(data);
  return [...stages].reverse().find(s => dates[s]) || 'new_lead';
}
export const formatDate = (value?: string | null) => value
  ? new Intl.DateTimeFormat('en-IN', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'Asia/Kolkata' })
    .format(new Date(value.length === 10 ? `${value}T12:00:00+05:30` : value))
  : 'Not set';
