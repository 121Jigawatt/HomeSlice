import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { Icon, IconName } from "./icons";
import { useToast } from "./toast";

interface ConfirmOptions {
  title: string;
  body?: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
}
type Confirm = (opts: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<Confirm>(async () => false);

/** Native <dialog> confirmation: focus is trapped, Escape cancels, and the page behind is inert. */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [opts, setOpts] = useState<ConfirmOptions | null>(null);
  const resolver = useRef<((ok: boolean) => void) | null>(null);

  const confirm = useCallback<Confirm>((o) => {
    setOpts(o);
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  useEffect(() => {
    if (opts && ref.current && !ref.current.open) ref.current.showModal();
  }, [opts]);

  const close = (ok: boolean) => {
    resolver.current?.(ok);
    resolver.current = null;
    ref.current?.close();
    setOpts(null);
  };

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <dialog
        ref={ref}
        className="dialog"
        aria-labelledby="confirm-title"
        aria-describedby="confirm-body"
        onCancel={(e) => {
          e.preventDefault();
          close(false);
        }}
      >
        {opts && (
          <form method="dialog" className="stack" onSubmit={(e) => (e.preventDefault(), close(true))}>
            <h2 id="confirm-title">{opts.title}</h2>
            {opts.body && <div id="confirm-body">{opts.body}</div>}
            <div className="row end">
              <button type="button" className="secondary" onClick={() => close(false)} autoFocus>
                Cancel
              </button>
              <button type="submit" className={opts.danger ? "danger-solid" : undefined}>
                {opts.confirmLabel ?? "Confirm"}
              </button>
            </div>
          </form>
        )}
      </dialog>
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): Confirm {
  return useContext(ConfirmContext);
}

const UNDO_MS = 8000;

/**
 * Hides an item immediately and offers Undo; the real delete only runs once the undo window closes.
 * If the tab is closed first, nothing is deleted.
 */
export function useUndoableDelete() {
  const notify = useToast();
  return useCallback(
    (label: string, hide: () => void, restore: () => void, commit: () => Promise<unknown>) => {
      hide();
      let undone = false;
      const timer = window.setTimeout(() => {
        if (undone) return;
        commit().catch((e: Error) => {
          restore();
          notify(`Couldn't delete ${label}: ${e.message}`, { tone: "error" });
        });
      }, UNDO_MS);
      notify(`Deleted ${label}.`, {
        ms: UNDO_MS,
        action: {
          label: "Undo",
          onClick: () => {
            undone = true;
            window.clearTimeout(timer);
            restore();
          },
        },
      });
    },
    [notify],
  );
}

export interface MenuItem {
  label: string;
  icon?: IconName;
  href?: string;
  download?: boolean;
  onSelect?: () => void;
  danger?: boolean;
}

/** "…" overflow menu with arrow-key navigation; closes on Escape, selection or outside click. */
export function OverflowMenu({ label, items }: { label: string; items: MenuItem[] }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const first = root.current?.querySelector<HTMLElement>("[role=menuitem]");
    first?.focus();
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const onKey = (e: React.KeyboardEvent) => {
    const els = [...(root.current?.querySelectorAll<HTMLElement>("[role=menuitem]") ?? [])];
    const i = els.indexOf(document.activeElement as HTMLElement);
    if (e.key === "Escape") {
      setOpen(false);
      button.current?.focus();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      els[(i + 1) % els.length]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      els[(i - 1 + els.length) % els.length]?.focus();
    } else if (e.key === "Tab") setOpen(false);
  };

  return (
    <div className="menu" ref={root} onKeyDown={onKey}>
      <button
        ref={button}
        type="button"
        className="icon-button secondary"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        onClick={() => setOpen(!open)}
      >
        <Icon name="more" size={20} />
      </button>
      {open && (
        <div className="menu-list" role="menu" aria-label={label}>
          {items.map((it) =>
            it.href ? (
              <a
                key={it.label}
                role="menuitem"
                tabIndex={-1}
                href={it.href}
                download={it.download || undefined}
                className={it.danger ? "danger-text" : undefined}
                onClick={() => setOpen(false)}
              >
                {it.icon && <Icon name={it.icon} size={16} />} {it.label}
              </a>
            ) : (
              <button
                key={it.label}
                type="button"
                role="menuitem"
                tabIndex={-1}
                className={it.danger ? "danger-text" : undefined}
                onClick={() => {
                  setOpen(false);
                  it.onSelect?.();
                }}
              >
                {it.icon && <Icon name={it.icon} size={16} />} {it.label}
              </button>
            ),
          )}
        </div>
      )}
    </div>
  );
}
