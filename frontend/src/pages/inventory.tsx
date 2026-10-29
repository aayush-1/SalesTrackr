import { useContext, useState } from 'react';
import { Link } from 'react-router-dom';
import { RefreshCw } from 'lucide-react';
import { RefreshContext, useResource } from '../api';
import { Alert, Empty, Field, Loading, Modal, PageHeader, SaveButton, TrackedForm, markFormSaved } from '../components';
import { useManufacturing, useManufacturingResource, useManufacturingSave } from '../manufacturing-api';
import type { Product } from '../manufacturing-types';

interface StockRow extends Product {
  requiredQuantity: number; missingQuantity: number;
  openJobs: { id: string; number: string; quantity: number | null }[]; openJobCount: number;
  sales?: { recordId: string; quantity: number }[]; salesCount?: number;
}
interface StockPage { items: StockRow[]; total: number }
interface DemandPage { items: { recordId: string; companyName: string; quantity: number }[]; total: number }

function DemandDetails({ product, close }: { product: StockRow; close: () => void }) {
  const [offset, setOffset] = useState(0);
  const resource = useResource<DemandPage>(`/manufacturing/inventory/products/${product.id}/demand?offset=${offset}`);
  return <Modal title={`Sales requiring ${product.name}`} onClose={close}>
    {resource.error && <Alert>{resource.error}</Alert>}{resource.loading && <Loading />}
    {resource.data && <><div className="table-scroll"><table><thead><tr><th>Company</th><th>Required quantity</th></tr></thead><tbody>
      {resource.data.items.map(row => <tr key={row.recordId}><td><Link to={`/records/${row.recordId}`}>{row.companyName}</Link></td><td>{row.quantity}</td></tr>)}
    </tbody></table></div><div className="pagination"><span>{resource.data.total} sales records</span><button disabled={!offset} onClick={() => setOffset(offset - 50)}>Previous</button><button disabled={offset + 50 >= resource.data!.total} onClick={() => setOffset(offset + 50)}>Next</button></div></>}
  </Modal>;
}

function StockReport({ resource, shortages, offset, setOffset, refresh }: {
  resource: { data: StockPage | null; loading: boolean; error: string };
  shortages: boolean; offset: number; setOffset: (offset: number) => void; refresh: () => void;
}) {
  const [demand, setDemand] = useState<StockRow | null>(null);
  const title = shortages ? 'Order in hand shortage' : 'Low stock';
  return <><PageHeader eyebrow="PRODUCT INVENTORY" title={title}
    description={shortages ? 'Combined requirements from New leads, Follow-ups and Order in hand, compared with actual stock.' : 'Products with stock below their minimum quantity.'}
    action={<button className="button" onClick={refresh}><RefreshCw size={16} />Refresh</button>} />
    {resource.error && <Alert>{resource.error}</Alert>}{resource.loading && <Loading />}
    {resource.data && <section className="panel details-panel">{!resource.data.items.length
      ? <Empty title={shortages ? 'No order shortages' : 'No low-stock products'} text={shortages ? 'Current stock covers the recorded requirements.' : 'Configured products are at or above their minimum quantity.'} />
      : <div className="table-scroll"><table aria-label={title}><thead><tr><th>Product</th><th>{shortages ? 'Required quantity' : 'Minimum quantity'}</th><th>In stock</th><th>Short by</th>{shortages && <th>Sales records</th>}<th>Manufacturing</th></tr></thead><tbody>
        {resource.data.items.map(product => <tr key={product.id}><td><Link to={`/manufacturing/products/${product.id}`}>{product.name}</Link><span className="cell-sub">{product.partNumber}</span></td>
          <td>{shortages ? product.requiredQuantity : product.minimumStockQuantity}</td><td>{product.stockQuantity}</td><td><strong>{product.missingQuantity}</strong></td>
          {shortages && <td><button className="text-button" onClick={() => setDemand(product)}>{product.salesCount} sales records</button></td>}
          <td>{product.stepIds.length ? <Link className="button" to={`/manufacturing/records/new?productId=${product.id}&quantity=${product.missingQuantity}`}>Create manufacturing record</Link>
            : <Link className="button" to={`/manufacturing/products/${product.id}`}>Set up process</Link>}
            {product.openJobCount > 0 && <div className="cell-sub">{product.openJobCount} open jobs{product.openJobs.map(job => <Link className="cell-sub" key={job.id} to={`/manufacturing/records/${job.id}`}>{job.number} / {job.quantity ?? 'Quantity not recorded'}</Link>)}</div>}</td>
        </tr>)}
      </tbody></table></div>}
      <div className="pagination"><span>{resource.data.total} products</span><div><button disabled={!offset} onClick={() => setOffset(offset - 50)}>Previous</button><button disabled={offset + 50 >= resource.data.total} onClick={() => setOffset(offset + 50)}>Next</button></div></div>
    </section>}
    {demand && <DemandDetails product={demand} close={() => setDemand(null)} />}
  </>;
}

export function LowStockPage() {
  const [offset, setOffset] = useState(0);
  const { refresh } = useManufacturing();
  const resource = useManufacturingResource<StockPage>(`/inventory/low-stock?offset=${offset}`);
  return <StockReport resource={resource} shortages={false} offset={offset} setOffset={setOffset} refresh={refresh} />;
}
export function OrderShortagePage() {
  const [offset, setOffset] = useState(0);
  const { refresh } = useContext(RefreshContext);
  const resource = useResource<StockPage>(`/manufacturing/inventory/order-shortages?offset=${offset}`);
  return <StockReport resource={resource} shortages offset={offset} setOffset={setOffset} refresh={() => refresh()} />;
}

export function StockEditor({ product, close }: { product: Product; close: () => void }) {
  const save = useManufacturingSave();
  const locked = save.busy || save.unconfirmed;
  return <Modal title="Edit stock" onClose={() => { if (!locked) close(); }}><TrackedForm onSubmit={async event => {
    event.preventDefault(); const form = event.currentTarget, values = new FormData(form);
    const result = save.unconfirmed ? await save.retry() : await save.save(`/products/${product.id}/stock`, {
      stockQuantity: Number(values.get('stockQuantity')), minimumStockQuantity: Number(values.get('minimumStockQuantity')),
      reason: String(values.get('reason')),
    }, product.version, product.id);
    if (result) { markFormSaved(form); close(); }
  }}><div className="form-grid">
    <Field label="Stock quantity *"><input type="number" name="stockQuantity" required min={0} max={10 ** 12} step={1} defaultValue={product.stockQuantity ?? ''} disabled={locked} /></Field>
    <Field label="Minimum stock quantity *"><input type="number" name="minimumStockQuantity" required min={0} max={10 ** 12} step={1} defaultValue={product.minimumStockQuantity ?? ''} disabled={locked} /></Field>
    <Field label="Reason *" wide><textarea name="reason" required maxLength={10000} disabled={locked} /></Field>
  </div>{save.error && <Alert>{save.error}</Alert>}<div className="form-footer"><SaveButton busy={save.busy}>{save.unconfirmed ? 'Retry same save' : 'Save stock'}</SaveButton></div></TrackedForm></Modal>;
}

interface Movement {
  id: string; source: 'sale' | 'manufacturing' | 'manual' | 'opening'; sourceId: string;
  beforeQuantity: number | null; afterQuantity: number; delta: number | null; reason: string;
  actor: string; recordedAt: string; sourceAvailable?: boolean;
}
export function StockHistory({ productId }: { productId: string }) {
  const [offset, setOffset] = useState(0);
  const resource = useManufacturingResource<{ items: Movement[]; total: number }>(`/inventory/products/${productId}/movements?offset=${offset}`);
  return <section className="panel details-panel"><h2>Stock history</h2>{resource.error && <Alert>{resource.error}</Alert>}{resource.loading && <Loading />}
    {resource.data && <>{!resource.data.items.length ? <p className="muted">No stock changes recorded.</p> : <div className="table-scroll"><table aria-label="Stock history"><thead><tr><th>Recorded</th><th>Source</th><th>Before</th><th>Change</th><th>After</th><th>Reason / recorded by</th></tr></thead><tbody>
      {resource.data.items.map(row => <tr key={row.id}><td>{new Date(row.recordedAt).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' })} IST</td>
        <td>{row.source === 'sale' || row.source === 'manufacturing' ? row.sourceAvailable
          ? <Link to={row.source === 'sale' ? `/records/${row.sourceId}` : `/manufacturing/records/${row.sourceId}`}>{row.source === 'sale' ? 'Sales record' : 'Manufacturing record'}</Link>
          : `Deleted ${row.source === 'sale' ? 'sales' : 'manufacturing'} record` : row.source === 'opening' ? 'Initial stock' : 'Manual adjustment'}</td>
        <td>{row.beforeQuantity ?? 'Not recorded'}</td><td>{row.delta === null ? 'Set balance' : row.delta > 0 ? `+${row.delta}` : row.delta}</td><td>{row.afterQuantity}</td><td>{row.reason || 'Workflow change'}<span className="cell-sub">{row.actor}</span></td>
      </tr>)}</tbody></table></div>}
      <div className="pagination"><span>{resource.data.total} stock changes</span><button disabled={!offset} onClick={() => setOffset(offset - 20)}>Previous</button><button disabled={offset + 20 >= resource.data.total} onClick={() => setOffset(offset + 20)}>Next</button></div></>}
  </section>;
}
