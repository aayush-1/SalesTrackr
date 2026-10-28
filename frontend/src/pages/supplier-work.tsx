import { ArrowLeft, ArrowRight, ChevronLeft, ChevronRight, RefreshCw, Search } from 'lucide-react';
import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Alert, Empty, Loading, PageHeader } from '../components';
import { useManufacturing, useManufacturingResource } from '../manufacturing-api';
import type { ManufacturingMeta, ManufacturingRecordPage } from '../manufacturing-types';

type SupplierWork = ManufacturingMeta & {
  items: { id: string; name: string; jobCount: number; productCount: number }[];
  total: number; supplierCount: number; jobCount: number;
};
type SupplierJobs = ManufacturingRecordPage & { supplierName: string | null };

export function SupplierWorkSheet({ overview = false }: { overview?: boolean }) {
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const { refresh } = useManufacturing();
  const limit = overview ? 5 : 50;
  const resource = useManufacturingResource<SupplierWork>(`/records/supplier-work?search=${encodeURIComponent(search)}&offset=${offset}&limit=${limit}`);
  return <section className="panel bt-directory" aria-label="Supplier work in progress">
    <div className="bt-panel-heading"><div><h2>Supplier work in progress</h2><p className="muted">One entry per supplier. Open a supplier to see its current manufacturing work.</p></div>
      {overview ? <Link className="text-button" to="/manufacturing/supplier-work">View all<ArrowRight size={14} /></Link>
        : <button className="icon-button" aria-label="Refresh supplier work" onClick={refresh}><RefreshCw size={17} /></button>}
    </div>
    {!overview && <div className="bt-toolbar"><label className="search"><Search size={17} /><input aria-label="Search supplier work" placeholder="Supplier, product, part number, operation or job..." maxLength={200} value={search}
      onChange={event => { setSearch(event.target.value); setOffset(0); }} /></label></div>}
    {resource.error && <Alert>{resource.error}</Alert>}
    {resource.loading ? <Loading /> : resource.data && (resource.data.items.length ? <div className="table-scroll"><table><thead><tr>
      <th>Supplier</th><th>Jobs in progress</th><th>Products</th>
    </tr></thead><tbody>{resource.data.items.map(row => <tr key={row.id}>
      <td><Link className="record-title" to={`/manufacturing/supplier-work?supplier=${encodeURIComponent(row.id)}`}>{row.name}</Link></td>
      <td>{row.jobCount}</td><td>{row.productCount}</td>
    </tr>)}</tbody></table></div> : <Empty title={search ? 'No supplier work matches' : 'No supplier work in progress'} text="Only assigned, in-progress operations appear here. Awaiting assignment, completed and cancelled jobs are excluded." />)}
    {resource.data && <div className="bt-pagination"><span>{resource.data.supplierCount} suppliers · {resource.data.jobCount} jobs in progress</span>{!overview && <div>
      <button className="icon-button" aria-label="Previous supplier work page" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - limit))}><ChevronLeft size={17} /></button>
      <button className="icon-button" aria-label="Next supplier work page" disabled={offset + limit >= resource.data.total} onClick={() => setOffset(offset + limit)}><ChevronRight size={17} /></button>
    </div>}</div>}
  </section>;
}

function SupplierManufacturingWork({ supplierId }: { supplierId: string }) {
  const [offset, setOffset] = useState(0);
  const { refresh } = useManufacturing();
  const limit = 50;
  const resource = useManufacturingResource<SupplierJobs>(`/records/supplier-work?partnerId=${encodeURIComponent(supplierId)}&offset=${offset}&limit=${limit}`);
  return <>
    <Link className="text-button" to="/manufacturing/supplier-work"><ArrowLeft size={15} />All suppliers in progress</Link>
    <section className="panel bt-directory" aria-label="Supplier manufacturing work">
      <div className="bt-panel-heading"><div><h2>{resource.data?.supplierName || 'Current manufacturing work'}</h2>
        <p className="muted">Current in-progress operations assigned to this supplier.</p></div>
        <button className="icon-button" aria-label="Refresh supplier work" onClick={refresh}><RefreshCw size={17} /></button>
      </div>
      {resource.error && <Alert>{resource.error}</Alert>}
      {resource.loading ? <Loading /> : resource.data && (resource.data.items.length ? <div className="table-scroll"><table><thead><tr>
        <th>Product / part number</th><th>Current operation</th><th>Contact</th><th>Manufacturing record</th>
      </tr></thead><tbody>{resource.data.items.map(row => <tr key={row.id}>
        <td><Link to={`/manufacturing/products/${row.productId}`}>{row.productName}</Link><span className="cell-sub">{row.productReference || 'No part number'}</span></td>
        <td>{row.currentOperationName}</td><td>{row.currentContactName || 'No contact assigned'}</td>
        <td><Link to={`/manufacturing/records/${row.id}`}>{row.number}</Link></td>
      </tr>)}</tbody></table></div> : <Empty title="No current manufacturing work" text="This supplier has no assigned, in-progress jobs. Awaiting assignment, completed and cancelled jobs are excluded." />)}
      {resource.data && <div className="bt-pagination"><span>{resource.data.total} jobs in progress</span><div>
        <button className="icon-button" aria-label="Previous supplier jobs page" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - limit))}><ChevronLeft size={17} /></button>
        <button className="icon-button" aria-label="Next supplier jobs page" disabled={offset + limit >= resource.data.total} onClick={() => setOffset(offset + limit)}><ChevronRight size={17} /></button>
      </div></div>}
      <div className="bt-toolbar"><Link className="text-button" to={`/manufacturing/partners/${encodeURIComponent(supplierId)}`}>Open supplier profile<ArrowRight size={14} /></Link></div>
    </section>
  </>;
}

export function SupplierWorkPage() {
  const [params] = useSearchParams();
  const supplierId = params.get('supplier');
  return <>
    <PageHeader eyebrow="PRODUCTTRACKR / SUPPLIERS" title="Supplier work" description="One entry per supplier with work in progress. Open a supplier to see its current manufacturing jobs." />
    {supplierId ? <SupplierManufacturingWork key={supplierId} supplierId={supplierId} /> : <SupplierWorkSheet />}
  </>;
}
