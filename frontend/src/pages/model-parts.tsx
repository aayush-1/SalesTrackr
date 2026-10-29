import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Download, RefreshCw } from 'lucide-react';
import { usePdfDownload, useResource } from '../api';
import { Alert, Empty, Field, Loading, PageHeader } from '../components';
import { useManufacturing } from '../manufacturing-api';
import type { Product } from '../manufacturing-types';
import type { MachineModel, Revision } from '../types';
import { useDirectory } from './sales-parts';

export function ModelPartsPage() {
  const { refresh } = useManufacturing();
  const models = useDirectory<Revision<MachineModel>>('/models');
  const [model, setModel] = useState(''), [offset, setOffset] = useState(0);
  const data = useResource<{ items: Product[]; total: number }>(model
    ? `/parts/catalog?modelId=${encodeURIComponent(model)}&offset=${offset}&limit=100` : null);
  const pdf = usePdfDownload(`/parts/catalog/pdf?${new URLSearchParams({ modelId: model })}`, 'master-products-list.pdf');
  return <><PageHeader eyebrow="PRODUCTTRACKR / CATALOG" title="Master Product's List" description="Products for a model, including Common parts."
    action={<div className="actions"><button className="button" disabled={!model || !data.data || data.loading || !!data.error || !!models.error || pdf.busy} onClick={pdf.download}><Download size={16} />{pdf.busy ? 'Creating PDF...' : 'Create PDF'}</button>
      <button className="button" disabled={pdf.busy} onClick={refresh}><RefreshCw size={16} />Refresh</button></div>} />
    <section className="panel details-panel">
      <Field label="Model"><select value={model} disabled={models.loading || pdf.busy} onChange={event => { setModel(event.target.value); setOffset(0); }}>
        <option value="">Select a model</option>{models.items.map(row => <option key={row.entityId} value={row.entityId}>{row.data.name}</option>)}
      </select></Field>
      {(models.error || data.error || pdf.error) && <Alert>{models.error || data.error || pdf.error}</Alert>}
      {(models.loading || data.loading && model) && <Loading />}
      {!model && !models.loading && <p className="muted">Select a model to see its parts.</p>}
      {data.data && <>{data.data.items.length ? <div className="table-scroll"><table aria-label="Master Product's List">
        <thead><tr><th>Product</th><th>Part number</th><th>Notes</th></tr></thead><tbody>
          {data.data.items.map(product => <tr key={product.id}>
            <td><Link to={`/manufacturing/products/${product.id}`}>{product.name}</Link>{!product.modelIds?.length && <span className="cell-sub">Common</span>}</td>
            <td>{product.partNumber || 'Not added'}</td><td className="model-parts-notes">{product.notes || 'Not added'}</td>
          </tr>)}
        </tbody></table></div> : <Empty title="No compatible products" text="Add products for this model or mark them Common." />}
        <div className="pagination"><span>{data.data.total} products</span><div>
          <button disabled={!offset} onClick={() => setOffset(offset - 100)}>Previous</button>
          <button disabled={offset + 100 >= data.data.total} onClick={() => setOffset(offset + 100)}>Next</button>
        </div></div></>}
    </section>
  </>;
}
