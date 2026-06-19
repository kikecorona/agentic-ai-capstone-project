// vite.config.js
//
// Plain Vite + Vue 3 + Quasar (via @quasar/vite-plugin). This is the
// lighter setup compared to @quasar/app-vite — Quasar still works, but
// we skip its CLI's SASS-variable auto-injection that was triggering
// the source-SASS compile of `quasar.sass` (and the resulting
// `:has(:is(...))` / `math.div()` parser interactions).
//
// Theme overrides happen at runtime via `setCssVar()` in src/main.js;
// Quasar's CSS comes from the precompiled bundle (`quasar/dist/quasar.css`).
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { quasar, transformAssetUrls } from "@quasar/vite-plugin";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [
    vue({ template: { transformAssetUrls } }),
    // sassVariables: false → no auto-injected `@import 'quasar.variables.scss'`.
    // We override the Quasar palette at runtime via setCssVar() instead.
    quasar({ sassVariables: false }),
  ],
  server: {
    host: process.env.PORTAL_HOST || "127.0.0.1",
    port: Number(process.env.PORTAL_PORT || 9000),
    // strictPort:true → vite errors out instead of falling back to 9001,
    // 9002, … if 9000 is taken. Combined with the port-9000 killer in
    // start_all.sh, this guarantees the portal is always reachable at
    // the same URL the rest of the toolchain hardcodes.
    strictPort: true,
    open: false,
  },
  resolve: {
    // Match the import paths used across the existing components so
    // we don't have to touch every page/component file.
    alias: {
      "@":          fileURLToPath(new URL("./src", import.meta.url)),
      "pages":      fileURLToPath(new URL("./src/pages", import.meta.url)),
      "components": fileURLToPath(new URL("./src/components", import.meta.url)),
      "layouts":    fileURLToPath(new URL("./src/layouts", import.meta.url)),
      "stores":     fileURLToPath(new URL("./src/stores", import.meta.url)),
      "boot":       fileURLToPath(new URL("./src/boot", import.meta.url)),
    },
  },
});
