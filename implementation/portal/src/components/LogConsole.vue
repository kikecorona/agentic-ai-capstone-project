<template>
  <q-card flat bordered class="log-console column no-wrap">
    <q-toolbar class="bg-grey-9 text-white console-bar">
      <q-icon name="terminal" class="q-mr-sm" />
      <span class="ellipsis title">{{ title }}</span>
      <q-space />
      <q-chip dense square :color="statusColor" text-color="dark">
        {{ status }}
      </q-chip>
      <q-chip dense square color="grey-7" text-color="white">
        {{ events.length }}
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
            <pre class="detail-pre">{{ detail.request || "(empty)" }}</pre>
            <div class="section-label q-mt-md">response</div>
            <pre class="detail-pre">{{ detail.response || "(empty)" }}</pre>
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
import { computed, ref } from "vue";

const props = defineProps({
  title: { type: String, required: true },
  events: { type: Array, required: true },
  status: { type: String, default: "" },
});

// Reverse without mutating the parent's array. `[...arr].reverse()` is
// O(n) and Vue will re-run this computed whenever the source array
// reactively changes (push from EventSource handler triggers it).
const reversedEvents = computed(() => [...props.events].reverse());

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
</script>

<style lang="scss" scoped>
.log-console {
  height: 100%;
  background: #0e0f1a;
}
.console-bar {
  font-family: "JetBrains Mono", monospace;
}
.title {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 1.15rem;
  letter-spacing: 0.05em;
}
.scroll-area {
  background: #0e0f1a;
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
  color: #00acc1;
}
.lvl-warn {
  color: #ffd600;
}
.lvl-error {
  color: #ff5252;
}
.lvl-debug {
  color: #777;
}
.lvl-llm {
  color: #ff6b35;
}
.mod {
  color: #ff9d63;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.msg {
  color: #f5e6d3;
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
  background: #1a1a2e;
  color: #f5e6d3;
  border: 1px solid #ff6b35;
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
  background: #1c1f33;
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
  color: #f5e6d3;
  word-break: break-word;
}
.detail-body {
  background: #0e0f1a;
  padding: 12px 16px;
  max-height: 60vh;
  overflow-y: auto;
}
.section-label {
  color: #ffd600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-size: 0.7rem;
  margin-bottom: 4px;
}
.section-label.error-label {
  color: #ff5252;
}
.detail-pre {
  background: #11121f;
  border-left: 3px solid #00acc1;
  color: #f5e6d3;
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
