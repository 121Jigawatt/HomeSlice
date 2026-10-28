import { useEffect, useRef } from "react";
import { api, Job, JobPage } from "./api";
import { useToast } from "./toast";

/** App-wide: announces when an in-progress slice finishes or fails, whatever page you're on. */
export function JobWatcher() {
  const notify = useToast();
  const active = useRef<Set<string> | null>(null);

  useEffect(() => {
    let timer: number | undefined;
    let stopped = false;
    const tick = async () => {
      if (document.visibilityState === "visible") {
        try {
          const page = await api<JobPage>("/api/jobs?status=queued,running&limit=50");
          const now = new Set(page.items.map((j) => j.id));
          if (active.current) {
            for (const id of active.current) {
              if (now.has(id)) continue;
              const job = await api<Job>(`/api/jobs/${id}`).catch(() => null);
              if (!job || stopped) continue;
              const name = job.title ?? "Slice";
              if (job.status === "done")
                notify(`${name} sliced${job.stats?.print_time ? ` \u00b7 ${job.stats.print_time}` : ""}.`, {
                  tone: "success",
                  action: { label: "Open", to: `/jobs/${id}` },
                });
              else if (job.status === "failed")
                notify(`${name} failed: ${job.error_info?.title ?? "slicing error"}.`, {
                  tone: "error",
                  action: { label: "Details", to: `/jobs/${id}` },
                  ms: 10000,
                });
            }
          }
          active.current = now;
        } catch {
          /* transient; try again next tick */
        }
      }
      if (!stopped) timer = window.setTimeout(tick, active.current?.size ? 3000 : 10000);
    };
    tick();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [notify]);

  return null;
}
