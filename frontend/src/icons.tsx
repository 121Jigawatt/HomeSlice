type IconName =
  | "library"
  | "slice"
  | "jobs"
  | "settings"
  | "printing"
  | "paused"
  | "ready"
  | "done"
  | "error"
  | "offline"
  | "queued"
  | "cube"
  | "download"
  | "camera"
  | "more"
  | "close"
  | "trash"
  | "printer"
  | "menu"
  | "upload"
  | "orca"
  | "user"
  | "key"
  | "edit";

// 24x24 outline paths (stroke icons) kept inline so the PWA shell needs no icon font.
const PATHS: Record<IconName, string> = {
  library: "M4 5h6v14H4zM14 5h6v14h-6z",
  slice: "M3 17l9 4 9-4M3 12l9 4 9-4M12 3l9 4-9 4-9-4z",
  jobs: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
  settings: "M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z",
  printing: "M6 4l14 8-14 8z",
  paused: "M7 4h4v16H7zM13 4h4v16h-4z",
  ready: "M12 21a9 9 0 100-18 9 9 0 000 18z",
  done: "M20 6L9 17l-5-5",
  error: "M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z",
  offline: "M18 6L6 18M6 6l12 12",
  queued: "M12 7v5l3 3M12 21a9 9 0 100-18 9 9 0 000 18z",
  cube: "M21 16V8l-9-5-9 5v8l9 5 9-5zM3.3 7L12 12l8.7-5M12 22V12",
  download: "M12 3v12M7 10l5 5 5-5M5 21h14",
  camera: "M23 7l-7 5 7 5zM1 5h15v14H1z",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  close: "M18 6L6 18M6 6l12 12",
  trash: "M3 6h18M8 6V4h8v2M6 6l1 15h10l1-15M10 11v6M14 11v6",
  printer: "M4 3h16v18H4zM4 7h16M8 11h8v4l-4 2-4-2zM12 17v1",
  menu: "M3 6h18M3 12h18M3 18h18",
  upload: "M12 21V9M7 14l5-5 5 5M5 3h14",
  orca: "M3 5h18v12H3zM8 21h8M12 17v4",
  user: "M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2M12 11a4 4 0 100-8 4 4 0 000 8z",
  key: "M21 2l-2 2m-7.6 7.6a5.5 5.5 0 11-7.8 7.8 5.5 5.5 0 017.8-7.8zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4",
  edit: "M12 20h9M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z",
};

export function Icon({ name, size = 18, label }: { name: IconName; size?: number; label?: string }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  );
}

export type { IconName };
