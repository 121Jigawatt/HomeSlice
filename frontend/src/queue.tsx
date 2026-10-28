import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, QueueItem } from "./api";
import { useMe } from "./auth";
import { Icon } from "./icons";
import { BedFlag, IDLE_STATES, useLivePrinters } from "./printers";
import { useToast } from "./toast";
import { useConfirm } from "./ui";

const POLL_MS = 5000;

const ITEM_STATUS: Record<QueueItem["status"], string> = {
  waiting: "Waiting",
  sending: "Starting…",
  sent: "Started",
  failed: "Failed",
  cancelled: "Cancelled",
};

/** Per-printer print queue: what's next, why it's waiting, reorder and cancel. */
export function PrintQueuePanel() {
  const [items, setItems] = useState<QueueItem[] | null>(null);
  const { printers } = useLivePrinters();
  const confirm = useConfirm();
  const notify = useToast();
  const me = useMe();
  const canReorder = me.can("printers.queue");
  const canCancel = (it: QueueItem) => canReorder || (me.can("printers.print") && me.owns(it.owner));
  const load = useCallback(() => api<QueueItem[]>("/api/queue").then(setItems).catch(() => undefined), []);
  useEffect(() => {
    load();
    const t = window.setInterval(() => document.visibilityState === "visible" && load(), POLL_MS);
    return () => window.clearInterval(t);
  }, [load]);

  if (!items || items.length === 0) return null;

  const byPrinter = new Map<string, QueueItem[]>();
  for (const it of items) byPrinter.set(it.printer_id, [...(byPrinter.get(it.printer_id) ?? []), it]);

  async function move(printerId: string, waiting: QueueItem[], index: number, delta: number) {
    const ids = waiting.map((w) => w.id);
    const [moved] = ids.splice(index, 1);
    ids.splice(index + delta, 0, moved);
    try {
      await api("/api/queue/order", { method: "PUT", json: { printer_id: printerId, ids } });
    } catch (e) {
      notify((e as Error).message, { tone: "error" });
    }
    load();
  }

  async function cancel(it: QueueItem) {
    const ok = await confirm({
      title: `Remove ${it.job_title ?? it.file} from the ${it.printer_name} queue?`,
      body: <p>The sliced job stays in Jobs; it just won't print automatically.</p>,
      confirmLabel: "Remove from queue",
      danger: true,
    });
    if (!ok) return;
    try {
      await api(`/api/queue/${it.id}`, { method: "DELETE" });
    } catch (e) {
      notify((e as Error).message, { tone: "error" });
    }
    load();
  }

  return (
    <section className="card stack" id="queue" aria-labelledby="queue-title">
      <h2 id="queue-title">Print queue</h2>
      {[...byPrinter.entries()].map(([printerId, list]) => {
        const live = printers?.find((p) => p.id === printerId);
        const waiting = list.filter((i) => i.status === "waiting");
        const done = list.filter((i) => i.status !== "waiting");
        const why = !waiting.length
          ? null
          : !live
            ? "Checking printer…"
            : !live.connected
              ? "Waiting: printer is offline."
              : live.needs_attention
                ? "Waiting: confirm the bed is clear."
                : !IDLE_STATES.has(live.state)
                  ? `Waiting: printer is ${live.state}.`
                  : "Starting shortly…";
        return (
          <div key={printerId} className="stack">
            <div className="row between">
              <h3 className="queue-printer">{list[0].printer_name}</h3>
              {why && <span className="muted small">{why}</span>}
            </div>
            {live && waiting.length > 0 && <BedFlag p={live} />}
            {waiting.length > 0 && (
              <ol className="list queue-list">
                {waiting.map((it, i) => (
                  <li key={it.id} className="row queue-item">
                    <span className="queue-pos" aria-hidden="true">
                      {i + 1}
                    </span>
                    <Link className="grow plain title" to={`/jobs/${it.job_id}`}>
                      {it.job_title ?? it.file}
                    </Link>
                    <span className="muted small wide-only">{it.owner && `by ${it.owner}`}</span>
                    {canReorder && (
                      <>
                        <button
                          type="button"
                          className="icon-button secondary"
                          disabled={i === 0}
                          onClick={() => move(printerId, waiting, i, -1)}
                          aria-label={`Move ${it.job_title ?? it.file} up`}
                        >
                          ↑
                        </button>
                        <button
                          type="button"
                          className="icon-button secondary"
                          disabled={i === waiting.length - 1}
                          onClick={() => move(printerId, waiting, i, 1)}
                          aria-label={`Move ${it.job_title ?? it.file} down`}
                        >
                          ↓
                        </button>
                      </>
                    )}
                    {canCancel(it) && (
                      <button
                        type="button"
                        className="icon-button secondary"
                        onClick={() => cancel(it)}
                        aria-label={`Remove ${it.job_title ?? it.file} from queue`}
                      >
                        <Icon name="close" size={16} />
                      </button>
                    )}
                  </li>
                ))}
              </ol>
            )}
            {done.length > 0 && (
              <ul className="list small">
                {done.slice(0, 5).map((it) => (
                  <li key={it.id} className="row between">
                    <Link className="grow plain" to={`/jobs/${it.job_id}`}>
                      {it.job_title ?? it.file}
                    </Link>
                    <span className={`badge ${it.status === "failed" ? "failed" : it.status === "sent" ? "done" : ""}`} title={it.error ?? ""}>
                      <Icon name={it.status === "failed" ? "error" : it.status === "sent" ? "printing" : "offline"} size={14} />{" "}
                      {ITEM_STATUS[it.status]}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {done.find((i) => i.status === "failed" && i.error) && (
              <p className="error small">{done.find((i) => i.status === "failed")?.error}</p>
            )}
          </div>
        );
      })}
    </section>
  );
}
