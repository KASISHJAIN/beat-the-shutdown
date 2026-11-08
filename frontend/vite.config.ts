import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API runs on :8000 (uvicorn backend.api:app); the dev server proxies /api to it.
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: { react: ["react", "react-dom"], map: ["leaflet", "react-leaflet"], charts: ["recharts"] },
      },
    },
  },
  server: {
    host: true,
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
});
