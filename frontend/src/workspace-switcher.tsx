import { ArrowUpRight, Check, ChevronDown, Factory } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

const workspaces = [
  { id: 'sales', name: 'SalesTrackr', description: 'Sales, customers and collections', to: '/', Icon: ArrowUpRight },
  { id: 'manufacturing', name: 'BuildTrackr', description: 'Products, operations and partners', to: '/manufacturing', Icon: Factory },
] as const;

export function WorkspaceSwitcher({ current, onSwitch }: {
  current: 'sales' | 'manufacturing'; onSwitch?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  const selected = workspaces.find(workspace => workspace.id === current)!;
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setOpen(false); trigger.current?.focus(); }
    };
    const layout = window.matchMedia('(max-width: 760px)');
    const layoutChanged = () => setOpen(false);
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    layout.addEventListener('change', layoutChanged);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape);
      layout.removeEventListener('change', layoutChanged);
    };
  }, [open]);
  return <div className="workspace-switcher" ref={root}>
    <button ref={trigger} className="workspace-switcher-trigger" aria-label="Switch workspace"
      aria-expanded={open} aria-controls={id} onClick={() => setOpen(value => !value)}>
      <span className={`workspace-selected-icon ${current}`}><selected.Icon size={21} /></span>
      <span><small>WORKSPACE</small><strong>{selected.name}</strong></span>
      <ChevronDown className="workspace-switcher-chevron" size={14} />
    </button>
    {open && <div className="workspace-choices" id={id} role="group" aria-label="Available workspaces">
      <div className="workspace-choice-label">SWITCH WORKSPACE</div>
      {workspaces.map(({ id: key, name, description, to, Icon }) => <Link key={key} to={to}
        className={key === current ? 'selected' : ''} aria-current={key === current ? 'true' : undefined}
        onClick={() => { setOpen(false); onSwitch?.(); }}>
        <span className={`workspace-choice-icon ${key}`}><Icon size={19} /></span>
        <span><strong>{name}</strong><small>{description}</small></span>
        {key === current && <Check size={16} aria-label="Current workspace" />}
      </Link>)}
    </div>}
  </div>;
}
