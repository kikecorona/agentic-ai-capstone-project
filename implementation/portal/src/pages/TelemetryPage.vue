<template>
  <!--
    §9.8.4 Dashboard — surfaces every §9.7 online metric, split into
    two clearly labelled sections:

      • Service Metrics — infrastructure-level signals (call counts,
        latency, error rate) derived from the OTel span store via
        /v1/metrics.

      • Agent Metrics — output-quality signals from §9.7 (status
        histograms across every method, grader-fail / re-grade pass
        rates, escalation, ToT branch success, enrichment outcomes,
        coverage, freshness, open-placeholder rate, SME resolution
        time, cross-reference health). Items derivable from the
        existing span stream populate live; items that need an
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
      <div class="col-12">
        <metrics-panel
          title="status histogram (all methods)"
          :rows="statusRows"
          empty-text="No status histogram yet."
          kind="status"
        />
      </div>
    </div>

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

    <!-- LLM latency — sourced from the audit DB's llm_calls table.
         Headline KPIs cover overall calls / error rate / p50 / p95;
         the panel below breaks p50/p95 down per `module` so the
         operator can see which step (rag.auto_rag.grader,
         bp.compose_answer, …) is the slow one. -->
    <div class="row q-col-gutter-md q-mb-md">
      <div class="col-6 col-sm-3">
        <kpi-card
          label="llm calls"
          :value="llmCallCount"
          :sub="`${llmModuleCount} modules`"
          accent="accent"
        />
      </div>
      <div class="col-6 col-sm-3">
        <kpi-card
          label="llm error rate"
          :value="formatPct(llmErrorRate)"
          :sub="`${llmErrorCount} of ${llmCallCount} calls`"
          :accent="llmErrorRate > 0.05 ? 'negative' : 'accent'"
        />
      </div>
      <div class="col-6 col-sm-3">
        <kpi-card
          label="llm p50"
          :value="`${llmP50} ms`"
          sub="median call duration"
          accent="accent"
        />
      </div>
      <div class="col-6 col-sm-3">
        <kpi-card
          label="llm p95"
          :value="`${llmP95} ms`"
          :sub="llmSlowestModule || '—'"
          :accent="llmP95 > 5000 ? 'warning' : 'accent'"
        />
      </div>
    </div>

    <div class="row q-col-gutter-md q-mb-md">
      <div class="col-12">
        <metrics-panel
          title="llm latency p50 / p95 (ms, per module)"
          :rows="llmLatencyRows"
          empty-text="No LLM calls recorded yet — start a refresh and reload."
          kind="latency"
        />
      </div>
    </div>

    <!-- §9.7 doc-index + SME-loop KPIs sourced from /v1/metrics's
         `agent` section (BP/SD doc-indexes + orchestrator's
         pending_sme_questions table). The two cards still rendered
         as `dashboard-pending-card` are the ones that need data
         sources outside the existing on-disk state. -->
    <div class="row q-col-gutter-md q-mb-md">
      <div class="col-12 col-sm-6 col-md-4">
        <kpi-card
          label="coverage"
          :value="coverageOverallLabel"
          :sub="coverageSub"
          accent="accent"
        />
      </div>
      <div class="col-12 col-sm-6 col-md-4">
        <kpi-card
          label="freshness"
          :value="freshnessLabel"
          :sub="freshnessSub"
          accent="accent"
        />
      </div>
      <div class="col-12 col-sm-6 col-md-4">
        <kpi-card
          label="open-placeholder rate"
          :value="openPlaceholderLabel"
          :sub="openPlaceholderSub"
          :accent="openPlaceholderRatio > 0.25 ? 'warning' : 'accent'"
        />
      </div>
      <div class="col-12 col-sm-6 col-md-4">
        <kpi-card
          label="SME resolution time"
          :value="smeResolutionLabel"
          :sub="smeResolutionSub"
          accent="accent"
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

// ─── §9.6 LLM latency — sourced from llm_calls audit table ─────────
// `/v1/metrics` returns `llm: { by_module: {mod: {count, errors,
// latency_ms: {p50, p95, mean, max}}}, overall: {…} }`. Empty when
// no calls have been recorded yet (post `reset_state.sh --logs`).
const llmByModule = computed(() => metrics.value.llm?.by_module || {});
const llmOverall = computed(() => metrics.value.llm?.overall || {});

const llmCallCount = computed(() => llmOverall.value.count || 0);
const llmErrorCount = computed(() => llmOverall.value.errors || 0);
const llmModuleCount = computed(() => Object.keys(llmByModule.value).length);

const llmErrorRate = computed(() => {
  const n = llmCallCount.value;
  if (!n) return 0;
  return llmErrorCount.value / n;
});

const llmP50 = computed(() =>
  Math.round(llmOverall.value.latency_ms?.p50 || 0),
);

const llmP95 = computed(() =>
  Math.round(llmOverall.value.latency_ms?.p95 || 0),
);

// Per-module latency rows for the panel. Sorted by p95 desc so the
// slowest module surfaces first; a `mean` field also rides along for
// the tooltip on the bar.
const llmLatencyRows = computed(() =>
  Object.entries(llmByModule.value)
    .map(([k, v]) => ({
      key: k,
      p50: Math.round(v.latency_ms?.p50 || 0),
      p95: Math.round(v.latency_ms?.p95 || 0),
      max: Math.round(v.latency_ms?.max || 0),
    }))
    .sort((a, b) => b.p95 - a.p95),
);

const llmSlowestModule = computed(() => llmLatencyRows.value[0]?.key || "");

// ─── §9.7 doc-index + SME aggregates from /v1/metrics.agent ────────
// The orchestrator's ``_agent_quality_rollup`` reads BP/SD doc-indexes
// + ``pending_sme_questions`` and ships back a tidy summary; the
// dashboard formats it for display.
const agent = computed(() => metrics.value.agent || {});

// Coverage — BP coverage = "% of products SD points to that BP also
// has a page for"; SD coverage = symmetric.
const bpCoverageRatio = computed(() => agent.value.coverage?.bp?.ratio ?? null);
const sdCoverageRatio = computed(() => agent.value.coverage?.sd?.ratio ?? null);
const coverageOverallLabel = computed(() => {
  const parts = [];
  if (bpCoverageRatio.value !== null) parts.push(`BP ${formatPct(bpCoverageRatio.value)}`);
  if (sdCoverageRatio.value !== null) parts.push(`SD ${formatPct(sdCoverageRatio.value)}`);
  return parts.length ? parts.join(" · ") : "—";
});
const coverageSub = computed(() => {
  const bp = agent.value.coverage?.bp;
  const sd = agent.value.coverage?.sd;
  const parts = [];
  if (bp) parts.push(`${bp.covered}/${bp.expected} products`);
  if (sd) parts.push(`${sd.covered}/${sd.expected} services`);
  return parts.length ? parts.join(" · ") : "no cross-references";
});

// Freshness — median age since last refresh per domain.
const freshnessLabel = computed(() => {
  const bp = agent.value.freshness?.bp?.median_age_s;
  const sd = agent.value.freshness?.sd?.median_age_s;
  const parts = [];
  if (bp != null) parts.push(`BP ${formatAge(bp)}`);
  if (sd != null) parts.push(`SD ${formatAge(sd)}`);
  return parts.length ? parts.join(" · ") : "—";
});
const freshnessSub = computed(() => {
  const bp = agent.value.freshness?.bp;
  const sd = agent.value.freshness?.sd;
  const parts = [];
  if (bp) parts.push(`${bp.stale_count}/${bp.count} BP stale`);
  if (sd) parts.push(`${sd.stale_count}/${sd.count} SD stale`);
  return parts.length ? `>24h: ${parts.join(" · ")}` : "no pages indexed";
});

// Open-placeholder rate — % pages with at least one open
// SME-PLACEHOLDER. We surface the worst of (BP, SD) on the headline
// number so a single bad domain isn't averaged away.
const openPlaceholderRatio = computed(() => {
  const bp = agent.value.open_placeholder_rate?.bp?.ratio ?? 0;
  const sd = agent.value.open_placeholder_rate?.sd?.ratio ?? 0;
  return Math.max(bp, sd);
});
const openPlaceholderLabel = computed(() => {
  if (!agent.value.open_placeholder_rate) return "—";
  return formatPct(openPlaceholderRatio.value);
});
const openPlaceholderSub = computed(() => {
  const bp = agent.value.open_placeholder_rate?.bp;
  const sd = agent.value.open_placeholder_rate?.sd;
  const parts = [];
  if (bp) parts.push(`BP ${bp.count_with_open}/${bp.total_pages}`);
  if (sd) parts.push(`SD ${sd.count_with_open}/${sd.total_pages}`);
  return parts.length ? parts.join(" · ") : "—";
});

// SME resolution time — median / p95 across answered questions.
const smeResolutionLabel = computed(() => {
  const m = agent.value.sme_resolution_time?.median_s;
  if (m == null) return "—";
  return formatAge(m);
});
const smeResolutionSub = computed(() => {
  const t = agent.value.sme_resolution_time;
  if (!t) return "—";
  const p95 = t.p95_s != null ? `p95 ${formatAge(t.p95_s)}` : null;
  const counts = `${t.answered ?? 0} answered · ${t.pending ?? 0} pending`;
  return p95 ? `${p95} · ${counts}` : counts;
});

// ─── Helpers ────────────────────────────────────────────────────────
function formatPct(x) {
  if (x === null || x === undefined) return "—";
  return `${(x * 100).toFixed(1)}%`;
}

// Compact age formatter — turns seconds into a single-unit label
// (45s, 12m, 3h, 2d) so the freshness / SME-resolution KPI tiles
// stay readable. Uses the largest sensible unit; precision is one
// decimal for hours and below, integer for days.
function formatAge(seconds) {
  if (seconds == null || isNaN(seconds)) return "—";
  const s = Math.max(0, Number(seconds));
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${(s / 60).toFixed(1)}m`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`;
  return `${Math.round(s / 86400)}d`;
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
