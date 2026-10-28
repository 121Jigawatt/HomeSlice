import { Link } from "react-router-dom";
import { useMe } from "../auth";
import { PrinterStrip, useLivePrinters } from "../printers";
import { PrintQueuePanel } from "../queue";

export default function PrintersPage() {
  const { printers, error } = useLivePrinters();
  const me = useMe();
  return (
    <section className="stack">
      <h1>Printers</h1>
      {printers === null && !error && <p className="muted">Checking printers…</p>}
      {error && <p className="error">Couldn't load printer status: {error}</p>}
      {printers?.length === 0 && (
        <div className="card empty">
          <p>No printers yet. Add your Klipper printers to see their status and webcams here.</p>
          {me.can("printers.manage") && (
            <Link className="button" to="/settings">
              Add a printer
            </Link>
          )}
        </div>
      )}
      <PrinterStrip expanded />
      <PrintQueuePanel />
    </section>
  );
}
