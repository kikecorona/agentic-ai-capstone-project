// src/boot/apis.js
//
// Boot file — wires the API base URLs (orchestrator REST + GitHub raw)
// onto the Vue app's globalProperties so any component can do
// `this.$oc.get('/v1/metrics')` or `this.$gh.get('/repos/...')`.
//
// Pinia is also installed here so the settings store works in the
// composition API without per-component imports.
//
// Theme overrides happen here at runtime via Quasar's setCssVar() —
// avoids the SASS-source compilation path that triggers Quasar's
// internal `quasar.sass` to be processed by vite:css.
import axios from "axios";
import { createPinia } from "pinia";
import { setCssVar } from "quasar";

export default ({ app }) => {
  // 80s retro orange palette — applied to Quasar's CSS custom properties
  // before any component renders. Equivalent to overriding $primary etc.
  // at compile time, but works against the precompiled CSS bundle.
  setCssVar("primary", "#ff6b35");
  setCssVar("secondary", "#ffd600");
  setCssVar("accent", "#00acc1");
  setCssVar("dark", "#1a1a2e");
  setCssVar("dark-page", "#1a1a2e");
  setCssVar("positive", "#2e7d32");
  setCssVar("negative", "#ff5252");
  setCssVar("info", "#00acc1");
  setCssVar("warning", "#ffd600");

  // Orchestrator REST API. Default points at the locally-running
  // FastAPI service from start_all.sh; override via env or by editing
  // .env in the portal directory.
  const ocBase = import.meta.env.VITE_OC_BASE_URL || "http://127.0.0.1:8000";
  const oc = axios.create({ baseURL: ocBase, timeout: 30000 });

  // GitHub REST API for the BP/SD doc fetchers. Anonymous access works
  // for public repos; production deployments would proxy this through
  // the orchestrator with a server-side PAT.
  const gh = axios.create({
    baseURL: "https://api.github.com",
    timeout: 15000,
    headers: { Accept: "application/vnd.github+json" },
  });

  app.config.globalProperties.$oc = oc;
  app.config.globalProperties.$gh = gh;
  app.config.globalProperties.$ocBase = ocBase;

  // Surface the same instances on a provide() key so composition-API
  // setups can grab them via inject('oc') / inject('gh').
  app.provide("oc", oc);
  app.provide("gh", gh);
  app.provide("ocBase", ocBase);

  app.use(createPinia());
};
