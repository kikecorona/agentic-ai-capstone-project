<template>
  <!--
    §9.8.4 Dashboard — surfaces every §9.7 online metric, split into
    two clearly labelled sections:

      • Service Metrics — infrastructure-level signals (call counts,
        latency, error rate, status histograms) derived from the OTel
        span store via /v1/metrics.

      • Agent Metrics — output-quality signals from §9.7 (RAG status,
        grader-fail / re-grade pass rates, escalation, ToT branch
        success, coverage, freshness, open-placeholder rate, SME
        resolution time, cross-reference health). Items derivable from
        the existing span stream populate live; items that need an
        additional data source render as labelled "pending" cards so
        the §9.7 surface is visible end-to-end.

    Polls /v1/metrics on a 5s interval; charts re-render in place.
  -->
  <q-page class="dashboard-page q-pa-md">
    <q-toolbar class="bg-grey-9 text-white q-mb-md rounded-borders">
      <q-icon name="insights" class="q-mr-sm" />
      <span class="retro-display">Dashboard</span>
      <q-space />
      <q-chip dense square :color="statusColor" text-color="dark">
        {{ status }}
      </q-chip>
      <q-chip dense square color="grey-7" text-color="white">
        spans: {{ metrics.total_spans || 0 }}
      </q-chip>
      <q-btn flat dense round icon="refresh" :loading="loading" @click="reload" />
    </q-toolbar>

    <q-banner v-if="error" class="bg-red-9 text-white q-mb-md">
      <template v-slot:avatar><q-icon name="error" /></template>
      {{ error }}
    </q-banner>

    <!-- ═══════════════════════════════════════════════════════════
         SECTION A — SERVICE METRICS
         Infrastructure health from OTel spans (counts, latency, errors).
         ═══════════════════════════════════════════════════════════ -->
    <dashboard-section-header
      icon="dns"
      label="SERVICE METRICS"
    />

    <div class="row q-col-gutter-md q-mb-md">
      <div class="col-6 col-sm-3">
        <kpi-card
          label="total spans"
          :value="metrics.total_spans || 0"
          accent="accent"
        />
      </div>
      <div class="col-6 col-sm-3">
        <kpi-card
          label="error rate"
          :value="formatPct(errorRate)"
          :sub="`${errorCount} / ${metrics.total_spans || 0}`"
          :accent="errorRate > 0.05 ? 'negative' : 'accent'"
        />
      </div>
      <div class="col-6 col-sm-3">
        <kpi-card
          label="overall p95"
          :value="`${overallP95} ms`"
          :sub="slowestKey || '—'"
          accent="warning"
        />
      </div>
      <div class="col-6 col-sm-3">
        <kpi-card
          label="methods seen"
          :value="countRows.length"
          :sub="`${serviceCount} services`"
          accent="accent"
        />
      </div>
    </div>

    <div class="row q-col-gutter-md q-mb-md">
      <div class="col-12 col-md-6">
        <metrics-panel
          title="span counts (per service.method)"
          :rows="countRows"
          empty-text="No spans yet — start a refresh and reload."
          kind="bar"
        />
      </div>
      <div class="col-12 col-md-6">
        <metrics-panel
          title="latency p50 / p95 (ms)"
          :rows="latencyRows"
          empty-text="No latency data yet."
          kind="latency"
        />
      </div>
      <div class="col-12">
        <metrics-panel
          title="status histogram (all methods)"
          :rows="statusRows"
          empty-text="No status histogram yet."
          kind="status"
        />
      </div>
    </div>

    <!-- ═══════════════════════════════════════════════════════════
         SECTION B — AGENT METRICS
         §9.7 online signals: how good is the agent's output?
         ═══════════════════════════════════════════════════════════ -->
    <dashboard-section-header
      icon="psychology"
      label="AGENT METRICS"
    />

    <div class="row q-col-gutter-md q-mb-md">
      <div class="col-6 col-sm-3">
        <kpi-card
          label="re-grade pass rate"
          :value="formatPct(answerRegradePassRate)"
          :sub="`${ragOkCount} / ${ragRetrieveTotal} retrievals`"
          :accent="answerRegradePassRate < 0.7 ? 'warning' : 'accent'"
        />
      </div>
      <div class="col-6 col-sm-3">
        <kpi-card
          label="grader-fail rate"
          :value="formatPct(graderFailRate)"
          :sub="`${graderFailCount} low-conf / exhausted`"
          :accent="graderFailRate > 0.2 ? 'warning' : 'accent'"
        />
      </div>
      <div class="col-6 col-sm-3">
        <kpi-card
          label="escalation rate"
          :value="formatPct(escalationRate)"
          :sub="`${escalationCount} of ${dispatchTotal} dispatches`"
          :accent="escalationRate > 0.15 ? 'warning' : 'accent'"
        />
      </div>
      <div class="col-6 col-sm-3">
        <kpi-card
          label="ToT success rate"
          :value="totSuccessRate === null ? '—' : formatPct(totSuccessRate)"
          :sub="totSuccessSub"
          accent="accent"
        />
      </div>
    </div>

    <!-- Enrichment-specific KPIs (§9.3 enrich-existing flow). -->
    <div class="row q-col-gutter-md q-mb-md">
      <div class="col-6 col-sm-3">
        <kpi-card
          label="pages enriched"
          :value="pagesEnrichedCount"
          :sub="`${pagesEnrichedRate ? formatPct(pagesEnrichedRate) : '—'} of ${enrichTotal} touched`"
          accent="accent"
        />
      </div>
      <div class="col-6 col-sm-3">
        <kpi-card
          label="pages unchanged"
          :value="pagesUnchangedCount"
          :sub="enrichTotal ? `${formatPct(pagesUnchangedCount / enrichTotal)} skip-rate` : '—'"
          accent="accent"
        />
      </div>
      <div class="col-6 col-sm-3">
        <kpi-card
          label="escalated-only pages"
          :value="pagesEscalatedOnlyCount"
          :sub="`${pagesEscalatedOnlyCount ? 'all gaps to SME' : 'none'}`"
          :accent="pagesEscalatedOnlyCount > 0 ? 'warning' : 'accent'"
        />
      </div>
      <div class="col-6 col-sm-3">
        <kpi-card
          label="new pages stubbed"
          :value="pagesStubbedCount"
          :sub="pagesStubbedCount ? 'awaiting next refresh' : '—'"
          accent="accent"
        />
      </div>
    </div>

    <div class="row q-col-gutter-md q-mb-md">
      <div class="col-12 col-md-6">
        <metrics-panel
          title="RAG retrieve status"
          :rows="ragStatusRows"
          empty-text="No RAG retrieve spans yet."
          kind="status"
        />
      </div>
      <div class="col-12 col-md-6">
        <metrics-panel
          title="dispatch + enrich outcomes (escalation signal)"
          :rows="dispatchStatusRows"
          empty-text="No dispatch / enrich spans yet."
          kind="status"
        />
      </div>
    </div>

    <!-- §9.7 metrics that need data sources outside /v1/metrics —
         render as pending placeholders so the operator can see the
         intended shape. The `source` line on each card describes
         what's needed to wire it. -->
    <div class="row q-col-gutter-md q-mb-md">
      <div class="col-12 col-sm-6 col-md-4">
        <dashboard-pending-card
          label="coverage"
          sub="% products with BP page · % services with SD page"
          source="needs BP/SD doc_index query"
        />
      </div>
      <div class="col-12 col-sm-6 col-md-4">
        <dashboard-pending-card
          label="freshness"
          sub="median age since last refresh · % pages with diverged hash"
          source="needs BP/SD doc_index query"
        />
      </div>
      <div class="col-12 col-sm-6 col-md-4">
        <dashboard-pending-card
          label="open-placeholder rate"
          sub="% pages with at least one unresolved SME-PLACEHOLDER"
          source="needs BP/SD doc_index query"
        />
      </div>
      <div class="col-12 col-sm-6 col-md-4">
        <dashboard-pending-card
          label="SME resolution time"
          sub="median / p95 (posted_at → answered)"
          source="needs OC pending_sme_questions schema bump"
        />
      </div>
      <div class="col-12 col-sm-6 col-md-4">
        <dashboard-pending-card
          label="cross-reference health"
          sub="% relative MD links that resolve"
          source="needs scheduled link-validator pass"
        />
      </div>
      <div class="col-12 col-sm-6 col-md-4">
        <dashboard-pending-card
          label="index-quality hit rate"
          sub="chunks surviving retrieval but failing the grader"
          source="needs span-attribute aggregation"
        />
      </div>
    </div>
  </q-page>
</template>

<script setup>
import { computed, inject, onBeforeUnmount, onMounted, ref } from "vue";
import MetricsPanel from "components/MetricsPanel.vue";
import KpiCard from "components/KpiCard.vue";
import DashboardSectionHeader from "components/DashboardSectionHeader.vue";
import DashboardPendingCard from "components/DashboardPendingCard.vue";

const oc = inject("oc");

const metrics = ref({});
const loading = ref(false);
const error = ref("");
const lastFetched = ref(0);

const POLL_MS = 5000;
let timer = null;

const status = computed(() => {
  if (loading.value) return "fetching…";
  if (error.value) return "error";
  if (!lastFetched.value) return "idle";
  const ago = Math.round((Date.now() - lastFetched.value) / 1000);
  return `live · ${ago}s ago`;
});

const statusColor = computed(() => {
  if (error.value) return "red-5";
  if (loading.value) return "amber";
  return "accent";
});

// ─── Per-method breakdowns (existing) ──────────────────────────────
const countRows = computed(() =>
  Object.entries(metrics.value.counts || {})
    .map(([k, v]) => ({ key: k, value: v }))
    .sort((a, b) => b.value - a.value),
);

const serviceCount = computed(() => {
  const set = new Set();
  for (const k of Object.keys(metrics.value.counts || {})) {
    set.add(k.split(".")[0]);
  }
  return set.size;
});

const latencyRows = computed(() =>
  Object.entries(metrics.value.latency_ms || {})
    .map(([k, v]) => ({
      key: k,
      p50: Math.round(v.p50 || 0),
      p95: Math.round(v.p95 || 0),
      max: Math.round(v.max || 0),
    }))
    .sort((a, b) => b.p95 - a.p95),
);

const statusRows = computed(() =>
  Object.entries(metrics.value.status_counts || {}).map(([k, v]) => ({
    key: k,
    statuses: v,
  })),
);

// ─── §9.7 derived metrics — composed from status_counts/latency ────

// Service-level error: any status that isn't a known soft outcome.
const SOFT_STATUSES = new Set([
  "ok",
  "unset",
  "low_confidence",
  "exhausted",
  "no_match",
  "fallback",
  "escalation",
]);

const errorCount = computed(() => {
  let n = 0;
  for (const histo of Object.values(metrics.value.status_counts || {})) {
    for (const [s, c] of Object.entries(histo)) {
      if (!SOFT_STATUSES.has(s)) n += c;
    }
  }
  return n;
});

const errorRate = computed(() => {
  const total = metrics.value.total_spans || 0;
  if (!total) return 0;
  return errorCount.value / total;
});

const slowestKey = computed(() => latencyRows.value[0]?.key || "");
const overallP95 = computed(() => latencyRows.value[0]?.p95 || 0);

// RAG retrieve status mix — §9.7 RAG status distribution.
const ragStatusRows = computed(() => {
  const out = [];
  for (const [k, v] of Object.entries(metrics.value.status_counts || {})) {
    if (k.endsWith(".retrieve") || k.includes("rag")) {
      out.push({ key: k, statuses: v });
    }
  }
  return out;
});

const ragRetrieveTotal = computed(() => {
  let n = 0;
  for (const r of ragStatusRows.value) {
    for (const c of Object.values(r.statuses || {})) n += c;
  }
  return n;
});

const ragOkCount = computed(() => {
  let n = 0;
  for (const r of ragStatusRows.value) n += r.statuses?.ok || 0;
  return n;
});

const graderFailCount = computed(() => {
  let n = 0;
  for (const r of ragStatusRows.value) {
    n += (r.statuses?.low_confidence || 0) + (r.statuses?.exhausted || 0);
  }
  return n;
});

const graderFailRate = computed(() => {
  const total = ragRetrieveTotal.value;
  if (!total) return 0;
  return graderFailCount.value / total;
});

// §9.7 answer re-grade pass rate — RAG status `ok` requires both
// faithfulness AND answerability to pass, so the ok ratio over total
// retrievals IS the combined pass rate. (Per-axis breakdown would
// need extra span attributes; not yet exposed by OTel store.)
const answerRegradePassRate = computed(() => {
  const total = ragRetrieveTotal.value;
  if (!total) return 0;
  return ragOkCount.value / total;
});

// Dispatch outcomes — §9.7 escalation rate.
// Filter widened to include `enrich_page` so the dashboard surfaces
// per-page enrichment outcomes alongside the orchestrator/specialist
// dispatch spans.
const dispatchStatusRows = computed(() => {
  const out = [];
  for (const [k, v] of Object.entries(metrics.value.status_counts || {})) {
    if (
      k.includes("dispatch") ||
      k.includes("refresh") ||
      k.includes("ingest_sme") ||
      k.includes("enrich_page")
    ) {
      out.push({ key: k, statuses: v });
    }
  }
  return out;
});

const dispatchTotal = computed(() => {
  let n = 0;
  for (const r of dispatchStatusRows.value) {
    for (const c of Object.values(r.statuses || {})) n += c;
  }
  return n;
});

const escalationCount = computed(() => {
  let n = 0;
  for (const r of dispatchStatusRows.value) {
    n += r.statuses?.escalation || 0;
    n += r.statuses?.escalations_emitted || 0;
  }
  return n;
});

const escalationRate = computed(() => {
  const total = dispatchTotal.value;
  if (!total) return 0;
  return escalationCount.value / total;
});

// §9.7 ToT branch success rate — fraction of ToT loops exiting before
// the depth cap. Spans for ToT loops haven't been wired with explicit
// method names yet, so we look for any method containing "tot" and
// fall back to null when nothing matches (KPI then renders "—").
const totRows = computed(() => {
  const out = [];
  for (const [k, v] of Object.entries(metrics.value.status_counts || {})) {
    if (/(^|\.)tot(\.|$)/i.test(k)) {
      out.push({ key: k, statuses: v });
    }
  }
  return out;
});

const totSuccessRate = computed(() => {
  if (!totRows.value.length) return null;
  let ok = 0, total = 0;
  for (const r of totRows.value) {
    for (const [s, c] of Object.entries(r.statuses || {})) {
      total += c;
      if (s === "ok") ok += c;
    }
  }
  if (!total) return null;
  return ok / total;
});

const totSuccessSub = computed(() => {
  if (totSuccessRate.value === null) return "no ToT spans yet";
  let total = 0;
  for (const r of totRows.value) {
    for (const c of Object.values(r.statuses || {})) total += c;
  }
  return `${total} ToT span(s)`;
});

// ─── §9.3 enrich-pipeline KPIs ─────────────────────────────────────
// Aggregate over every `enrich_page` span emitted by BP/SD specialists.
// Status set the specialist can emit:
//   enriched         — at least one gap was filled with substantive content
//   escalated_only   — every gap got pushed to an SME-PLACEHOLDER
//   unchanged        — page hash + side-info hash both unchanged → skipped
//   stub_created     — new-page discovery wrote a fresh stub
//   page_deleted     — page no longer on disk; cleared from index
const enrichRows = computed(() => {
  const out = [];
  for (const [k, v] of Object.entries(metrics.value.status_counts || {})) {
    if (k.endsWith(".enrich_page")) out.push({ key: k, statuses: v });
  }
  return out;
});

const enrichTotal = computed(() => {
  let n = 0;
  for (const r of enrichRows.value) {
    for (const c of Object.values(r.statuses || {})) n += c;
  }
  return n;
});

function _enrichStatusCount(name) {
  let n = 0;
  for (const r of enrichRows.value) n += r.statuses?.[name] || 0;
  return n;
}

const pagesEnrichedCount = computed(() => _enrichStatusCount("enriched"));
const pagesUnchangedCount = computed(() => _enrichStatusCount("unchanged"));
const pagesEscalatedOnlyCount = computed(() => _enrichStatusCount("escalated_only"));
const pagesStubbedCount = computed(() => _enrichStatusCount("stub_created"));

const pagesEnrichedRate = computed(() => {
  const total = enrichTotal.value;
  if (!total) return 0;
  return pagesEnrichedCount.value / total;
});

// ─── Helpers ────────────────────────────────────────────────────────
function formatPct(x) {
  if (x === null || x === undefined) return "—";
  return `${(x * 100).toFixed(1)}%`;
}

async function reload() {
  loading.value = true;
  error.value = "";
  try {
    const res = await oc.get("/v1/metrics");
    metrics.value = res.data || {};
    lastFetched.value = Date.now();
  } catch (e) {
    error.value = e?.response?.status
      ? `HTTP ${e.response.status} from /v1/metrics`
      : e.message;
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  reload();
  timer = window.setInterval(reload, POLL_MS);
});
onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer);
});
</script>

<style lang="scss" scoped>
.retro-display {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 1.15rem;
  letter-spacing: 0.05em;
}
</style>
