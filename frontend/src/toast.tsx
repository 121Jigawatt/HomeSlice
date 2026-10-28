import { createContext, ReactNode, useCallback, useContext, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Icon } from "./icons";

type Tone = "success" | "error" | "info";
interface ToastAction {
  label: string;
  to?: string;
  onClick?: () => void;
}
interface Toast {
  id: number;
  tone: Tone;
  text: string;
  action?: ToastAction;
}
type Notify = (text: string, opts?: { tone?: Tone; action?: ToastAction; ms?: number }) => void;

const ToastContext = createContext<Notify>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);

  const notify = useCallback<Notify>(
    (text, opts = {}) => {
      const id = nextId.current++;
      setToasts((t) => [...t.slice(-3), { id, text, tone: opts.tone ?? "info", action: opts.action }]);
      window.setTimeout(() => dismiss(id), opts.ms ?? 6000);
    },
    [dismiss],
  );

  return (
    <ToastContext.Provider value={notify}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.tone}`}>
            <Icon name={t.tone === "error" ? "error" : t.tone === "success" ? "done" : "ready"} size={16} />
            <span className="grow">{t.text}</span>
            {t.action &&
              (t.action.to ? (
                <Link className="toast-action" to={t.action.to} onClick={() => dismiss(t.id)}>
                  {t.action.label}
                </Link>
              ) : (
                <button
                  type="button"
                  className="toast-action"
                  onClick={() => {
                    t.action?.onClick?.();
                    dismiss(t.id);
                  }}
                >
                  {t.action.label}
                </button>
              ))}
            <button type="button" className="toast-close" onClick={() => dismiss(t.id)} aria-label="Dismiss notification">
              <Icon name="close" size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): Notify {
  return useContext(ToastContext);
}
