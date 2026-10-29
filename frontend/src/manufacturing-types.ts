export type Directory = 'products' | 'operations' | 'suppliers' | 'contacts';
export interface ManufacturingMeta {
  workspaceId: string; generation: string; mode: 'demo' | 'production'; storageBackend: 'firestore';
}
export interface ManufacturingEntity {
  id: string; version: number; name: string; createdAt: string; updatedAt: string; updatedBy: string;
}
export interface Product extends ManufacturingEntity {
  partNumber: string; notes: string; stepIds: string[]; modelIds?: string[];
  stockQuantity?: number | null; minimumStockQuantity?: number | null;
}
export interface Operation extends ManufacturingEntity { description: string }
export interface Supplier extends ManufacturingEntity {
  type: 'company' | 'individual'; relationship: 'external' | 'internal';
  address: string; email: string; phone: string; notes: string;
}
export interface CompanyContact extends ManufacturingEntity {
  supplierId: string; designation: string; email: string; phone: string;
}
export interface ProductOperation {
  id: string; version: number; productId: string; operationId: string; instructions: string;
  primaryAssignmentId: string | null;
}
export interface SupplierAssignment {
  id: string; version: number; productId: string; stepId: string; supplierId: string;
  contactId: string | null; notes: string;
}
export type Master = Product | Operation | Supplier | CompanyContact;
export interface ManufacturingPage extends ManufacturingMeta { items: Master[]; total: number }
export interface ManufacturingDetail extends ManufacturingMeta {
  entity: Master; products: Product[]; operations: Operation[]; suppliers: Supplier[];
  contacts: CompanyContact[]; steps: ProductOperation[]; assignments: SupplierAssignment[];
}

export type ManufacturingRecordStatus = 'ready' | 'in_progress' | 'completed' | 'cancelled';
export type ManufacturingRecordView = ManufacturingRecordStatus | 'active' | 'all';
export type ManufacturingRecordAction = 'assign' | 'complete' | 'reassign' | 'undo' | 'cancel' | 'quantity';
export interface WorkAssignment {
  supplierId: string; supplierName: string; type: Supplier['type']; relationship: Supplier['relationship'];
  email: string; phone: string; contact: Pick<CompanyContact, 'id' | 'name' | 'designation' | 'email' | 'phone'> | null;
}
export interface CopiedOperation {
  sourceStepId: string; operationId: string; name: string; instructions: string;
}
export interface ManufacturingWorkStep extends CopiedOperation {
  id: string; status: ManufacturingRecordStatus | 'pending'; assignment: WorkAssignment | null;
  startedOn: string | null; assignedOn: string | null; completedOn: string | null;
  assignmentNote: string; completionNote: string;
}
export interface ManufacturingRecordSummary {
  id: string; version: number; number: string; productId: string; productName: string; productPartNumber: string;
  notes: string; status: ManufacturingRecordStatus; active: boolean; completedCount: number; stepCount: number;
  currentOperationId: string | null; currentOperationName: string | null;
  currentSupplierId: string | null; currentSupplierName: string | null; currentContactName: string | null;
  startedOn: string | null; completedOn: string | null; createdAt: string; updatedAt: string;
  quantity?: number | null; inventoryTracked?: boolean;
}
export interface ManufacturingRecord extends ManufacturingRecordSummary {
  operations: ManufacturingWorkStep[]; currentIndex: number | null; updatedBy: string;
  cancelledAt: string | null; cancelReason: string;
}
export interface EligibleManufacturingSupplier {
  supplier: Supplier; suggestedContactId: string | null; primary: boolean;
}
export interface ManufacturingRecordDetail extends ManufacturingMeta {
  record: ManufacturingRecord; eligibleSuppliers: EligibleManufacturingSupplier[];
  eligibilityError: string; today: string;
}
export interface ManufacturingRecordPreview extends ManufacturingMeta {
  process: { productId: string; productName: string; productPartNumber: string; operations: CopiedOperation[] };
  sourceToken: string; suggestions: Record<string, string[]>; today: string;
}
export interface ManufacturingRecordPage extends ManufacturingMeta { items: ManufacturingRecordSummary[]; total: number }
export interface ManufacturingRecordCounts extends ManufacturingMeta {
  counts: Record<ManufacturingRecordView, number>;
}
export interface ManufacturingRecordEvent {
  id: string; sequence: number; recordId: string; action: ManufacturingRecordAction | 'create';
  actor: string; recordedAt: string; occurredOn?: string; operationName?: string;
  note?: string; reason?: string; assignment?: WorkAssignment; previousAssignment?: WorkAssignment;
  previousCompletedOn?: string;
  quantity?: number; previousQuantity?: number | null;
}
export interface ManufacturingRecordHistory extends ManufacturingMeta { items: ManufacturingRecordEvent[]; total: number }

export interface ManufacturingCleanupRequest {
  writeId: string; confirmation: string; records: { id: string; baseVersion: number }[];
}
export interface ManufacturingCleanupPage extends ManufacturingRecordPage {
  canDelete: boolean; cleanupInProgress: boolean; lastError: string | null;
  pending: { request: ManufacturingCleanupRequest; generation: string } | null;
}
export interface ManufacturingCleanupResult extends Pick<ManufacturingMeta, 'workspaceId' | 'generation'> {
  deletedIds: string[]; deletedCount: number; activityEventsRemoved: number;
  writeReceiptsRemoved: number; retryMarkersKept: number; recoveryBackup: { id: string };
}
