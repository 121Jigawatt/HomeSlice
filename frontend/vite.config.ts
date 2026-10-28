import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // three.js (~530 kB) is only loaded lazily by the 3D/G-code viewers.
  build: { sourcemap: false, chunkSizeWarningLimit: 600 },
  server: {
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
