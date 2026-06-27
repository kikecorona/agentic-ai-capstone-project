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
        <q-separator vertical inset class="q-mx-sm filter-sep" />
        <q-btn
          v-for="t in TIME_DEFS"
          :key="t.value"
          dense
          no-caps
          :flat="timeFilter !== t.value"
          :outline="timeFilter !== t.value"
          :color="timeFilter === t.value ? 'purple-4' : 'grey-7'"
          :text-color="timeFilter === t.value ? 'dark' : undefined"
          :label="t.label"
          class="filter-btn"
          @click="timeFilter = t.value"
        >
          <q-tooltip class="bg-grey-9">Show last {{ t.label }} of events</q-tooltip>
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
    <!-- Split content area: left = event list, right = latest-event detail -->
    <div class="log-split col row no-wrap">
      <q-scroll-area class="log-list-pane">
        <div class="log-body">
          <!-- Descending order — newest event sits at the top so the
               operator never has to scroll to see what just happened.
               Each row is clickable and pops a dialog with the full text.
               The first row (latest) is highlighted and its detail is
               shown inline in the right pane. -->
          <div
            v-for="(ev, idx) in reversedEvents"
            :key="rowKey(ev)"
            class="log-line"
            :class="[lineClass(ev), { 'log-line--latest': rowKey(ev) === displayKey }]"
            @click="selectEvent(ev)"
          >
            <span class="ts">{{ formatTs(ev) }}</span>
            <span class="lvl" :class="levelClass(ev)">
              <q-icon
                :name="ev.kind === 'llm' ? 'smart_toy' : 'description'"
                size="14px"
                class="row-icon"
              />
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

      <q-separator vertical color="grey-8" />

      <!-- Right pane: always shows the latest (top) event's full detail. -->
      <div class="log-detail-pane column no-wrap">
        <template v-if="displayEvent">
          <div class="detail-pane-header">
            <q-icon
              :name="displayEvent.kind === 'llm' ? 'smart_toy' : 'description'"
              size="14px"
              class="q-mr-xs"
            />
            <span class="detail-pane-title">
              {{ displayEvent.kind === "llm" ? "LLM call" : "service log" }}
              · #{{ displayEvent.id }}
            </span>
          </div>
          <div class="detail-meta-inline">
            <div class="meta-grid">
              <div class="meta-key">timestamp</div>
              <div class="meta-val">{{ formatTsFull(displayEvent) }}</div>
              <div class="meta-key">module</div>
              <div class="meta-val">{{ displayEvent.module }}</div>
              <template v-if="displayEvent.kind === 'service'">
                <div class="meta-key">level</div>
                <div class="meta-val">{{ displayEvent.level }}</div>
              </template>
              <template v-else>
                <div class="meta-key">model</div>
                <div class="meta-val">{{ displayEvent.model }}</div>
                <div class="meta-key">temperature</div>
                <div class="meta-val">{{ displayEvent.temperature ?? "—" }}</div>
                <div class="meta-key">json mode</div>
                <div class="meta-val">{{ displayEvent.json_mode ? "yes" : "no" }}</div>
                <div class="meta-key">latency</div>
                <div class="meta-val">{{ Math.round(displayEvent.latency_ms || 0) }} ms</div>
              </template>
            </div>
          </div>
          <q-separator color="grey-8" />
          <div class="detail-body-inline col">
            <template v-if="displayEvent.kind === 'service'">
              <div class="section-label">message</div>
              <pre class="detail-pre">{{ displayEvent.message || "(empty)" }}</pre>
            </template>
            <template v-else>
              <div class="section-label">request</div>
              <pre class="detail-pre">{{ formatMaybeJson(displayEvent.request) }}</pre>
              <div class="section-label q-mt-md">response</div>
              <pre class="detail-pre">{{ formatMaybeJson(displayEvent.response) }}</pre>
              <template v-if="displayEvent.error">
                <div class="section-label q-mt-md error-label">error</div>
                <pre class="detail-pre detail-err">{{ displayEvent.error }}</pre>
              </template>
            </template>
          </div>
        </template>
        <div v-else class="detail-empty">
          (no events yet)
        </div>
      </div>
    </div>

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

const TIME_DEFS = [
  { label: "15 min", value: 900 },
  { label: "1 hr",   value: 3600 },
  { label: "1 day",  value: 86400 },
  { label: "1 wk",   value: 604800 },
  { label: "1 mo",   value: 2592000 },
  { label: "All",    value: 0 },
];
const timeFilter = ref(900);

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

// Sort by event timestamp descending (newest first), then apply the
// level filter. We use ``timestamp`` first (set on every event by
// the orchestrator's ``_tail_combined_events`` for both kinds) and
// fall back to ``started_at`` for older LLM rows that pre-date the
// alias. **Why not just reverse arrival order?** The merged SSE feed
// can deliver out-of-order events across poll cycles (a late LLM row
// with an older ``started_at`` arrives after a newer service row),
// so a plain reverse leaves the late row stranded mid-list. Sorting
// by timestamp puts every event in the right place regardless of
// when it arrived. The filter doesn't affect order.
const reversedEvents = computed(() => {
  const now = Date.now() / 1000;
  const cutoff = timeFilter.value > 0 ? now - timeFilter.value : 0;
  const items = [...props.events].filter(ev => {
    const t = ev.timestamp ?? ev.started_at ?? 0;
    return t >= cutoff;
  });
  items.sort((a, b) => {
    const ta = a.timestamp ?? a.started_at ?? 0;
    const tb = b.timestamp ?? b.started_at ?? 0;
    return tb - ta;
  });
  return items.filter(passes);
});

const filteredCount = computed(() => reversedEvents.value.length);

// Selected event — null means "track latest automatically". Clicking a
// row pins to that event; the highlight and right pane follow the selection.
const selectedEvent = ref(null);
const displayEvent = computed(() => selectedEvent.value ?? reversedEvents.value[0] ?? null);
const displayKey = computed(() => displayEvent.value ? rowKey(displayEvent.value) : null);

function selectEvent(ev) {
  selectedEvent.value = ev;
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
  // LLM start record: no response yet — show the request snippet instead.
  if (!ev.response) {
    const head = (ev.request || "")
      .replace(/\s+/g, " ")
      .slice(0, 140);
    return `starting… · ${head}${head.length === 140 ? "…" : ""}`;
  }
  // LLM result record: show latency + response head.
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
.filter-sep {
  background: #555;
  height: 16px;
  align-self: center;
}
.of-total {
  color: var(--theme-text-muted, #888);
  margin-left: 2px;
  font-size: 0.7rem;
}
.log-split {
  overflow: hidden;
}
.log-list-pane {
  width: 50%;
  min-width: 280px;
  height: 100%;
  background: var(--theme-bg-deep);
}
.log-detail-pane {
  flex: 1;
  min-width: 0;
  background: var(--theme-bg-page);
  overflow: hidden;
}
.detail-pane-header {
  display: flex;
  align-items: center;
  padding: 6px 12px;
  background: var(--theme-bg-panel);
  border-bottom: 1px solid var(--theme-bg-code);
  font-family: "JetBrains Mono", monospace;
  font-size: 0.78rem;
  color: #aaa;
  flex-shrink: 0;
}
.detail-pane-title {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 1rem;
  letter-spacing: 0.04em;
  color: var(--theme-text-primary);
}
.detail-meta-inline {
  background: var(--theme-bg-panel);
  padding: 8px 12px;
  font-size: 0.8rem;
  flex-shrink: 0;
}
.detail-body-inline {
  padding: 10px 12px;
  overflow-y: auto;
  background: var(--theme-bg-deep);
}
.detail-empty {
  color: #555;
  font-style: italic;
  font-family: "JetBrains Mono", monospace;
  font-size: 0.8rem;
  padding: 16px 12px;
}
.log-line--latest {
  background: rgba(255, 107, 53, 0.15);
  border-left: 2px solid var(--theme-accent-primary);
  padding-left: calc(0px);
}
.log-body {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.8rem;
  padding: 0.5rem 0.75rem;
}
.log-line {
  display: grid;
  grid-template-columns: 64px 84px 220px 1fr;
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
  white-space: nowrap;
  display: flex;
  align-items: center;
}
// Tiny inline icon — sits at the start of the level / message span
// for service-log and LLM rows respectively. Slightly nudged up so it
// optically centres against the monospace letterforms.
.row-icon {
  margin-right: 4px;
  vertical-align: -2px;
  opacity: 0.85;
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

// ──────────────────────────────────────────── inline detail pane
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
