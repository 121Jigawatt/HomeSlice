/**
 * Registers the offline-shell service worker. Browsers refuse service workers over a certificate they
 * don't trust, which is also the most reliable way for the page to learn the device hasn't trusted
 * HomeSlice's certificate yet.
 */
export type TrustState = "trusted" | "untrusted" | "unknown";

let resolveTrust: (s: TrustState) => void;
export const certTrust: Promise<TrustState> = new Promise((r) => (resolveTrust = r));

export function registerServiceWorker(): void {
  if (!import.meta.env.PROD || !("serviceWorker" in navigator)) {
    resolveTrust("unknown");
    return;
  }
  if (!window.isSecureContext) {
    // Plain HTTP (e.g. port 8080 on the LAN): service workers are unavailable regardless of certificates.
    resolveTrust("unknown");
    return;
  }
  navigator.serviceWorker
    .register("/sw.js", { scope: "/" })
    .then(() => resolveTrust("trusted"))
    .catch((e: Error) => {
      const certError = e.name === "SecurityError" || /ssl|certificate/i.test(e.message);
      resolveTrust(certError && location.protocol === "https:" ? "untrusted" : "unknown");
    });
}
