import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// BASE_PATH is set by the GitHub Pages build (/blindfold/); locally it's /.
export default defineConfig({
  base: process.env.BASE_PATH || "/",
  plugins: [react()],
  server: { port: 5173, fs: { allow: [".."] }, allowedHosts: [".app.github.dev"] },
});