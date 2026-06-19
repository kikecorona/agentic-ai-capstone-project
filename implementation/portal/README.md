# Documentation Portal

Single-page Quasar v2 + Vue 3 web app — the user-facing surface for the
Capstone POC ([§9.8](../../PROJECT_LOW_LEVEL_DESIGN.md#98-documentation-portal)).
Three tabs and one collapsible drawer:

| Surface              | What it shows                                                                |
|----------------------|------------------------------------------------------------------------------|
| **Documentation**    | Live tree + Markdown render of `documentation/` from `kikecorona/pear-store` (covers `bp/`, `sd/`, and any other sibling) |
| **SME Answers**      | Pending escalation queue + reply form (drives the orchestrator's `ingest_sme_reply`) |
| **Dashboard**        | KPI tiles + per-(service, method) counts, p50/p95 latency, status histograms, RAG status mix, dispatch outcomes (§9.6 derived) |
| **Agent X-Ray** *(right-side drawer, toggled from header)* | Console-style live tail of the merged service-log + LLM-call SSE feed (`GET /v1/streams/events`) |

A floating chat bubble is mounted in the layout, available across every tab —
posts to `POST /v1/queries` and renders the agent's Markdown answer with cited
sources. A branch selector in the header (`main` / `starting-point` / custom
ref) applies to the Documentation tab; the operator-facing surfaces read live
runtime state and ignore branch.

## Setup

Requires Node.js 18+ and npm 9+.

```bash
cd implementation/portal
npm install
npm install -g @quasar/cli   # one-time, gives you `quasar` on PATH
```

`start_all.sh --install-dependencies` from the parent `implementation/`
directory runs both of these for you on a clean checkout.

## Run

The portal needs the orchestrator REST API up — `start_all.sh` brings
the five Python services up on port 8000 by default; the portal's dev
server binds 9000.

```bash
# In implementation/, boot the backend services if you haven't:
cd ..
./start_all.sh

# Then in another terminal:
cd portal
npm run dev          # → http://127.0.0.1:9000
```

`start_all.sh` will also boot the portal automatically when it detects
`npx`/`quasar` and the `node_modules` dir.

## Configuration

Override defaults via environment variables before `npm run dev`:

| Env var               | Default                    | Purpose                                    |
|-----------------------|----------------------------|--------------------------------------------|
| `PORTAL_PORT`         | `9000`                     | Quasar dev server bind port                |
| `VITE_OC_BASE_URL`    | `http://127.0.0.1:8000`    | Where to find the orchestrator REST API    |

The GitHub owner / repo / default branch are baked into the
`DocsViewer` component for the POC; future-deployment changes go in
`src/components/DocsViewer.vue`.

## Layout

```
implementation/portal/
├── README.md
├── package.json
├── quasar.config.js
├── index.html
├── public/
│   └── logo/                 # mono-white SVG used by header + footer
└── src/
    ├── App.vue               # router shell
    ├── boot/apis.js          # axios instances + Pinia install
    ├── css/
    │   ├── app.scss          # body / page resets
    │   ├── quasar.variables.scss   # Quasar SCSS variable overrides
    │   └── retro.scss        # 80s retro orange skin
    ├── layouts/
    │   └── MainLayout.vue    # header + 5 tabs + footer
    ├── pages/
    │   ├── BPPage.vue
    │   ├── SDPage.vue
    │   ├── SMEAnswersPage.vue
    │   ├── XRayPage.vue
    │   └── TelemetryPage.vue
    ├── components/
    │   ├── BranchSelector.vue
    │   ├── DocsViewer.vue
    │   ├── LogConsole.vue
    │   └── MetricsPanel.vue
    ├── stores/settings.js    # cross-tab UI state (active branch)
    └── router/{index,routes}.js
```

## Backend dependencies

The X-Ray drawer and Dashboard tab talk to two orchestrator endpoints
added in [§9.4.2](../../PROJECT_LOW_LEVEL_DESIGN.md#942-apis-rest):

- `GET /v1/streams/events`  — merged SSE tail of `service_logs` + `llm_calls` (audit DB);
  each event tagged with `kind: "service" | "llm"`
- `GET /v1/metrics`         — synchronous OTel `get_metrics` passthrough

CORS is enabled on the orchestrator so the portal can reach those
endpoints from a different origin during dev.
