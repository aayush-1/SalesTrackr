import { ExternalLink, FolderOpen, RefreshCw } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Alert } from '../components';
import { manufacturingApi, ManufacturingError, useManufacturing } from '../manufacturing-api';
import type { ManufacturingMeta } from '../manufacturing-types';

type Folder = ManufacturingMeta & { configured: boolean; url: string | null };

export function ProductFiles({ productId }: { productId: string }) {
  const { meta, workspaceChanged, noteWorkspaceChange } = useManufacturing();
  const [folder, setFolder] = useState<Folder | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (workspaceChanged) return;
    const controller = new AbortController();
    setFolder(null); setError('');
    manufacturingApi<Folder>(`/products/${productId}/drive-folder`, { method: 'POST', signal: controller.signal }, meta)
      .then(result => {
        if (controller.signal.aborted) return;
        if (result.generation !== meta.generation || result.workspaceId !== meta.workspaceId) {
          throw new ManufacturingError('ProductTrackr changed. Reload before opening files.', 409, 'workspace_changed');
        }
        setFolder(result);
      })
      .catch(reason => {
        if (controller.signal.aborted) return;
        if (reason.code === 'workspace_changed') noteWorkspaceChange();
        setError(reason.message);
      });
    return () => controller.abort();
  }, [productId, meta, workspaceChanged, noteWorkspaceChange, attempt]);
  return <section className="panel details-panel" aria-label="Product files">
    <div className="bt-panel-heading"><h2><FolderOpen size={19} /> Drawings &amp; files</h2>
      {folder?.url && !workspaceChanged && <a className="button" href={folder.url} target="_blank" rel="noreferrer"><ExternalLink size={16} />Open Drive folder</a>}
      {error && <button className="button" disabled={workspaceChanged} onClick={() => setAttempt(v => v + 1)}><RefreshCw size={16} />Retry folder connection</button>}
    </div>
    {error ? <Alert>{error}</Alert> : !folder ? <p>Connecting this product's Drive folder...</p>
      : folder.configured ? <p>Add drawings, specifications and other files directly in this product's folder under ProductTrackr / Products. Its name updates with the product name and part number; its folder ID, link and files stay the same.</p>
        : <p>Connect the private ProductTrackr Drive folder and the owner's Drive credentials to enable a folder for this product.</p>}
    <p className="muted">Files stay in Google Drive. Workspace ZIP backups contain records, not drawings or uploaded files; restoring or cleaning up jobs does not delete these files. Existing Drive permissions apply.</p>
  </section>;
}
