<template>
  <q-card flat bordered class="log-console column no-wrap">
    <q-toolbar class="bg-grey-9 text-white console-bar">
      <q-icon name="terminal" class="q-mr-sm" />
      <span class="ellipsis title">{{ title }}</span>
      <!-- Level filters — small on/off toggles. Flat = off, solid = on.
           State is internal to LogConsole; filtering happens after the
           reverse + before render so the count chip reflects the
           filtered set. -->
      <div class="row no-wrap items-center q-ml-md filter-toggles">
        <q-btn
          v-for="f in FILTER_DEFS"
          :key="f.key"
          dense
          no-caps
          :flat="!filters[f.key]"
          :outline="!filters[f.key]"
          :color="filters[f.key] ? f.color : 'grey-7'"
          :text-color="filters[f.key] ? 'dark' : undefined"
          :label="f.label"
          class="filter-btn"
          @click="filters[f.key] = !filters[f.key]"
        >
          <q-tooltip class="bg-grey-9">
            {{ filters[f.key] ? "Hide" : "Show" }} {{ f.label }} events
          </q-tooltip>
        </q-btn>
      </div>
      <q-space />
      <q-chip dense square :color="statusColor" text-color="dark">
        {{ status }}
      </q-chip>
      <q-chip dense square color="grey-7" text-color="white">
        {{ filteredCount }}
        <span v-if="filteredCount !== events.length" class="of-total">
          / {{ events.length }}
        </span>
      </q-chip>
    </q-toolbar>
    <q-scroll-area class="col scroll-area">
      <div class="log-body">
        <!-- Descending order — newest event sits at the top so the
             operator never has to scroll to see what just happened.
             Each row is clickable and pops a dialog with the full text.
             Each event carries a ``kind`` discriminator (set server-side
             by /v1/streams/events) so the same component renders both
             service-log rows and LLM-call rows. -->
        <div
          v-for="ev in reversedEvents"
          :key="rowKey(ev)"
          class="log-line"
          :class="lineClass(ev)"
          @click="openDetail(ev)"
        >
          <span class="ts">{{ formatTs(ev) }}</span>
          <span class="lvl" :class="levelClass(ev)">
            {{ levelLabel(ev) }}
          </span>
          <span class="mod">{{ ev.module }}</span>
          <span class="msg">{{ formatMessage(ev) }}</span>
          <span v-if="ev.error" class="err">!{{ ev.error }}</span>
        </div>
        <div v-if="events.length === 0" class="empty-line">
          (waiting for events…)
        </div>
      </div>
    </q-scroll-area>

    <!-- Click-to-detail dialog. Layout switches based on the captured
         event's ``kind`` — service rows show level + message, LLM rows
         show model / latency / request / response / error. -->
    <q-dialog v-model="detailOpen">
      <q-card class="detail-card">
        <q-toolbar class="bg-grey-9 text-white">
          <q-icon name="terminal" class="q-mr-sm" />
          <span class="title">
            {{ detail?.kind === "llm" ? "LLM call" : "service log" }}
            · #{{ detail?.id }}
          </span>
          <q-space />
          <q-btn flat dense round icon="close" v-close-popup />
        </q-toolbar>
        <q-card-section v-if="detail" class="detail-meta">
          <div class="meta-grid">
            <div class="meta-key">timestamp</div>
            <div class="meta-val">{{ formatTsFull(detail) }}</div>
            <div class="meta-key">module</div>
            <div class="meta-val">{{ detail.module }}</div>
            <template v-if="detail.kind === 'service'">
              <div class="meta-key">level</div>
              <div class="meta-val">{{ detail.level }}</div>
            </template>
            <template v-else>
              <div class="meta-key">model</div>
              <div class="meta-val">{{ detail.model }}</div>
              <div class="meta-key">temperature</div>
              <div class="meta-val">{{ detail.temperature ?? "—" }}</div>
              <div class="meta-key">json mode</div>
              <div class="meta-val">{{ detail.json_mode ? "yes" : "no" }}</div>
              <div class="meta-key">latency</div>
              <div class="meta-val">{{ Math.round(detail.latency_ms || 0) }} ms</div>
            </template>
          </div>
        </q-card-section>
        <q-separator />
        <q-card-section v-if="detail" class="detail-body">
          <template v-if="detail.kind === 'service'">
            <div class="section-label">message</div>
            <pre class="detail-pre">{{ detail.message || "(empty)" }}</pre>
          </template>
          <template v-else>
            <div class="section-label">request</div>
            <pre class="detail-pre">{{ formatMaybeJson(detail.request) }}</pre>
            <div class="section-label q-mt-md">response</div>
            <pre class="detail-pre">{{ formatMaybeJson(detail.response) }}</pre>
            <template v-if="detail.error">
              <div class="section-label q-mt-md error-label">error</div>
              <pre class="detail-pre detail-err">{{ detail.error }}</pre>
            </template>
          </template>
        </q-card-section>
      </q-card>
    </q-dialog>
  </q-card>
</template>

<script setup>
import { computed, reactive, ref } from "vue";

const props = defineProps({
  title: { type: String, required: true },
  events: { type: Array, required: true },
  status: { type: String, default: "" },
});

// Level filters — solid pill = on, outline = off. Each toggle hides
// one bucket of events. ``llm`` covers non-error LLM-call rows; the
// ``error`` toggle covers BOTH service-log error level AND LLM rows
// whose ev.error is set, since both render with the red ERR label.
const FILTER_DEFS = [
  { key: "info", label: "info", color: "info" },
  { key: "warn", label: "warn", color: "warning" },
  { key: "error", label: "error", color: "negative" },
  { key: "llm", label: "llm", color: "positive" },
];
const filters = reactive({ info: true, warn: true, error: true, llm: true });

function passes(ev) {
  if (ev.kind === "llm") {
    // ev.error → counts as an "error" event; otherwise an "llm" event.
    return ev.error ? filters.error : filters.llm;
  }
  // service-log row.
  const lvl = (ev.level || "info").toLowerCase();
  if (lvl === "error") return filters.error;
  if (lvl === "warn" || lvl === "warning") return filters.warn;
  // info, debug, anything else → "info" bucket.
  return filters.info;
}

// Reverse without mutating the parent's array, then apply the level
// filter. ``[...arr].reverse()`` is O(n); Vue re-runs this computed
// whenever the source array reactively changes (push from EventSource
// handler triggers it) OR a filter toggle flips.
const reversedEvents = computed(() =>
  [...props.events].reverse().filter(passes),
);

const filteredCount = computed(() => reversedEvents.value.length);

// Click-to-detail state — separate `detail` ref so the dialog can keep
// rendering the captured event even when the underlying events array
// rolls past the buffer cap.
const detail = ref(null);
const detailOpen = ref(false);

function openDetail(ev) {
  detail.value = ev;
  detailOpen.value = true;
}

// Vue v-for key: kind+id avoids collisions across the two source
// tables (service.id=5 and llm.id=5 would otherwise clash).
function rowKey(ev) {
  return `${ev.kind || "?"}:${ev.id}`;
}

const statusColor = computed(() => {
  if (props.status === "live") return "accent";
  if (props.status === "paused") return "grey-5";
  return "amber";
});

function levelLabel(ev) {
  if (ev.kind === "service") return (ev.level || "info").toUpperCase().slice(0, 5);
  if (ev.error) return "ERR  ";
  return "LLM  ";
}

function levelClass(ev) {
  if (ev.kind === "llm") return ev.error ? "lvl-error" : "lvl-llm";
  switch ((ev.level || "info").toLowerCase()) {
    case "error":
      return "lvl-error";
    case "warn":
    case "warning":
      return "lvl-warn";
    case "debug":
      return "lvl-debug";
    default:
      return "lvl-info";
  }
}

function lineClass(ev) {
  if (ev.kind === "llm" && ev.error) return "row-error";
  if (ev.kind === "service" && (ev.level || "").toLowerCase() === "error") return "row-error";
  return "";
}

function formatTs(ev) {
  const t = ev.timestamp ?? ev.started_at;
  if (!t) return "        ";
  const d = new Date(t * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

function formatTsFull(ev) {
  const t = ev.timestamp ?? ev.started_at;
  if (!t) return "—";
  return new Date(t * 1000).toISOString();
}

function formatMessage(ev) {
  if (ev.kind === "service") return ev.message || "";
  // LLM: show latency + a head of the response (or the error).
  const ms = Math.round(ev.latency_ms || 0);
  const head = (ev.response || "")
    .replace(/\s+/g, " ")
    .slice(0, 140);
  return `${ms}ms · ${head}${head.length === 140 ? "…" : ""}`;
}

// Pretty-print a value that *might* be a JSON string (or a JSON
// string nested inside a JSON string — the LLM audit log sometimes
// double-encodes when the content of a message is itself JSON). We
// peel one layer of JSON, and if the result is still a string that
// also parses as JSON, peel again. Falls back to the raw text when
// nothing parses, so plain-prose responses render naturally.
function formatMaybeJson(raw) {
  if (raw == null || raw === "") return "(empty)";
  if (typeof raw !== "string") {
    try {
      return JSON.stringify(raw, null, 2);
    } catch (_) {
      return String(raw);
    }
  }
  const trimmed = raw.trim();
  // Cheap probe — only attempt JSON parse on input that *could*
  // plausibly be JSON, so prose like `Sure, here is...` doesn't get
  // shoved through `JSON.parse` and burn cycles on a guaranteed throw.
  const looksJson = /^[\[{"]/.test(trimmed);
  if (!looksJson) return raw;
  try {
    let parsed = JSON.parse(trimmed);
    // One more peel if the value is itself a JSON string.
    if (typeof parsed === "string") {
      const inner = parsed.trim();
      if (/^[\[{"]/.test(inner)) {
        try {
          parsed = JSON.parse(inner);
        } catch (_) {
          /* leave as-is */
        }
      }
    }
    return JSON.stringify(parsed, null, 2);
  } catch (_) {
    return raw;
  }
}
</script>

<style lang="scss" scoped>
.log-console {
  height: 100%;
  background: var(--theme-bg-deep);
}
.console-bar {
  font-family: "JetBrains Mono", monospace;
}
.title {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 1.15rem;
  letter-spacing: 0.05em;
}
// Level filter row — small pill buttons sat between the title and the
// status chip.
.filter-toggles {
  gap: 4px;
}
.filter-btn {
  min-height: 0;
  padding: 0 8px;
  font-family: "JetBrains Mono", monospace;
  font-size: 0.7rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  border-radius: 3px;
}
.of-total {
  color: var(--theme-text-muted, #888);
  margin-left: 2px;
  font-size: 0.7rem;
}
.scroll-area {
  background: var(--theme-bg-deep);
}
.log-body {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.8rem;
  padding: 0.5rem 0.75rem;
}
.log-line {
  display: grid;
  grid-template-columns: 64px 56px 220px 1fr;
  gap: 0.5rem;
  padding: 1px 0;
  color: #d0d0d0;
  cursor: pointer;
  transition: background 0.08s ease-in-out;
}
.log-line:hover {
  background: rgba(255, 107, 53, 0.12);
}
.row-error {
  background: rgba(255, 0, 0, 0.08);
}
.ts {
  color: #888;
}
.lvl {
  font-weight: 600;
  letter-spacing: 0.05em;
}
.lvl-info {
  color: var(--theme-accent-info);
}
.lvl-warn {
  color: var(--theme-accent-secondary);
}
.lvl-error {
  color: #ff5252;
}
.lvl-debug {
  color: #777;
}
.lvl-llm {
  color: #7cdb40;
}
.mod {
  color: #ff9d63;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.msg {
  color: var(--theme-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.err {
  color: #ff5252;
}
.empty-line {
  color: #555;
  font-style: italic;
}

// ──────────────────────────────────────────── click-to-detail dialog
.detail-card {
  // Match the rest of the X-Ray panel — dark background, orange
  // accent border, retro-font headings — instead of Quasar's default
  // white q-card surface that bleeds through otherwise.
  background: var(--theme-bg-page);
  color: var(--theme-text-primary);
  border: 1px solid var(--theme-accent-primary);
  width: min(720px, 92vw);
  max-width: 92vw;
  font-family: "JetBrains Mono", monospace;
}
.detail-card .title {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 1.05rem;
  letter-spacing: 0.05em;
}
.detail-meta {
  background: var(--theme-bg-panel);
  padding: 12px 16px;
  font-size: 0.85rem;
}
.meta-grid {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 4px 12px;
}
.meta-key {
  color: #888;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-size: 0.7rem;
  align-self: center;
}
.meta-val {
  color: var(--theme-text-primary);
  word-break: break-word;
}
.detail-body {
  background: var(--theme-bg-deep);
  padding: 12px 16px;
  max-height: 60vh;
  overflow-y: auto;
}
.section-label {
  color: var(--theme-accent-secondary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-size: 0.7rem;
  margin-bottom: 4px;
}
.section-label.error-label {
  color: #ff5252;
}
.detail-pre {
  background: var(--theme-bg-deeper);
  border-left: 3px solid var(--theme-accent-info);
  color: var(--theme-text-primary);
  padding: 8px 10px;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: "JetBrains Mono", monospace;
  font-size: 0.82rem;
  line-height: 1.45;
}
.detail-pre.detail-err {
  border-left-color: #ff5252;
  color: #ffb3b3;
}
</style>
