import { useEffect, useState } from "react";
import { certTrust } from "./pwa";
import { Icon } from "./icons";

const DISMISS_KEY = "cert-banner.dismissed";

/** Explains how to make this device trust HomeSlice's private certificate authority. */
export function CertBanner() {
  const [show, setShow] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (localStorage.getItem(DISMISS_KEY) === "1") return;
    certTrust.then((s) => setShow(s === "untrusted"));
  }, []);

  if (!show) return null;
  return (
    <section className="cert-banner card stack" aria-labelledby="cert-title">
      <div className="row between">
        <h2 id="cert-title" className="row">
          <Icon name="error" size={18} /> This device doesn't trust HomeSlice yet
        </h2>
        <button
          type="button"
          className="icon-button secondary"
          aria-label="Dismiss certificate notice"
          onClick={() => {
            localStorage.setItem(DISMISS_KEY, "1");
            setShow(false);
          }}
        >
          <Icon name="close" size={16} />
        </button>
      </div>
      <p className="small">
        HomeSlice uses its own certificate, so your browser shows a warning. Everything still works, but HomeSlice can't be
        installed as an app or open offline until you trust it. Do this once per device.
      </p>
      <div className="row">
        <a className="button small" href="/homeslice-ca.crt" download>
          <Icon name="download" size={16} /> Download certificate
        </a>
        <button type="button" className="small secondary" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Hide steps" : "How to install it"}
        </button>
      </div>
      {open && (
        <div className="stack small cert-steps">
          <div>
            <h3>iPhone / iPad</h3>
            <ol>
              <li>Open this page in Safari and tap <b>Download certificate</b>, then <b>Allow</b>.</li>
              <li>Settings → <b>Profile Downloaded</b> → Install (enter your passcode).</li>
              <li>Settings → General → About → <b>Certificate Trust Settings</b> → turn on <b>HomeSlice local CA</b>.</li>
              <li>Close and reopen Safari, then Share → <b>Add to Home Screen</b>.</li>
            </ol>
          </div>
          <div>
            <h3>Android</h3>
            <ol>
              <li>Tap <b>Download certificate</b>.</li>
              <li>Settings → Security (or "Security &amp; privacy") → More → Encryption &amp; credentials → <b>Install a certificate</b> → <b>CA certificate</b>.</li>
              <li>Pick <code>homeslice-ca.crt</code> and confirm.</li>
              <li>Reopen Chrome, then menu → <b>Install app</b>.</li>
            </ol>
          </div>
          <div>
            <h3>Windows / macOS</h3>
            <ol>
              <li>Windows: open the file → Install Certificate → Current User → <b>Trusted Root Certification Authorities</b>.</li>
              <li>macOS: open it in Keychain Access, double-click <b>HomeSlice local CA</b> → Trust → <b>Always Trust</b>.</li>
              <li>Firefox keeps its own list: Settings → Privacy &amp; Security → View Certificates → Authorities → Import.</li>
            </ol>
          </div>
          <p className="muted">
            The certificate only lets this device recognise your HomeSlice server. If you reach HomeSlice by a different
            address than the ones set in <code>HOMESLICE_TLS_HOSTS</code>, add it there and restart the app.
          </p>
        </div>
      )}
    </section>
  );
}
