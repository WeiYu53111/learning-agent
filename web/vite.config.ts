import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { cpSync, mkdirSync, readFileSync } from "node:fs";
import { resolve, basename } from "node:path";

export default defineConfig({
  plugins: [
    react(),
    {
      name: "local-pdf-resources",
      configureServer(server) {
        server.middlewares.use("/assets/pdf", (req, res, next) => {
          const parts = (req.url || "")
            .split("?")[0]
            .split("/")
            .filter(Boolean);
          if (
            parts.length !== 2 ||
            !["cmaps", "standard_fonts", "wasm"].includes(parts[0]) ||
            basename(parts[1]) !== parts[1]
          )
            return next();
          try {
            const bytes = readFileSync(
              resolve("node_modules/pdfjs-dist", ...parts),
            );
            res.setHeader(
              "Content-Type",
              parts[1].endsWith(".wasm")
                ? "application/wasm"
                : "application/octet-stream",
            );
            res.end(bytes);
          } catch {
            next();
          }
        });
      },
      closeBundle() {
        const target = resolve("../src/teaching_agent/web_static/assets/pdf");
        mkdirSync(target, { recursive: true });
        for (const folder of ["cmaps", "standard_fonts", "wasm"])
          cpSync(
            resolve("node_modules/pdfjs-dist", folder),
            resolve(target, folder),
            { recursive: true },
          );
      },
    },
  ],
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
  build: { outDir: "../src/teaching_agent/web_static", emptyOutDir: true },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test-setup.ts",
    include: ["src/**/*.test.tsx"],
  },
});
