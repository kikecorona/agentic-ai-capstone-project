<template>
  <q-card flat bordered class="metrics-panel column">
    <q-toolbar class="bg-grey-9 text-white panel-bar">
      <q-icon name="bar_chart" class="q-mr-sm" />
      <span class="ellipsis title">{{ title }}</span>
    </q-toolbar>
    <q-card-section class="col q-pa-md">
      <div v-if="!rows.length" class="empty">{{ emptyText }}</div>

      <!-- bar: simple count visualisation -->
      <div v-else-if="kind === 'bar'" class="bar-grid">
        <div v-for="r in rows" :key="r.key" class="bar-row">
          <div class="bar-key" :title="r.key">{{ r.key }}</div>
          <div class="bar-track">
            <div
              class="bar-fill"
              :style="{ width: pct(r.value, maxCount) + '%' }"
            />
          </div>
          <div class="bar-num">{{ r.value }}</div>
        </div>
      </div>

      <!-- latency: p50 + p95 paired bars -->
      <div v-else-if="kind === 'latency'" class="bar-grid">
        <div v-for="r in rows" :key="r.key" class="lat-row">
          <div class="bar-key" :title="r.key">{{ r.key }}</div>
          <div class="bar-track">
            <div
              class="bar-fill bar-p50"
              :style="{ width: pct(r.p50, maxLatency) + '%' }"
              :title="`p50 ${r.p50} ms`"
            />
            <div
              class="bar-fill bar-p95"
              :style="{
                width: pct(r.p95 - r.p50, maxLatency) + '%',
                left: pct(r.p50, maxLatency) + '%',
              }"
              :title="`p95 ${r.p95} ms`"
            />
          </div>
          <div class="bar-num">
            <span class="num-p50">{{ r.p50 }}</span>
            <span class="num-sep">/</span>
            <span class="num-p95">{{ r.p95 }}</span>
          </div>
        </div>
      </div>

      <!-- status: stacked horizontal bars colored per status -->
      <div v-else-if="kind === 'status'" class="bar-grid">
        <div v-for="r in rows" :key="r.key" class="status-row">
          <div class="bar-key" :title="r.key">{{ r.key }}</div>
          <div class="status-track">
            <div
              v-for="(count, sname) in r.statuses"
              :key="sname"
              class="status-seg"
              :class="`status-${sname}`"
              :style="{ flex: count }"
              :title="`${sname}: ${count}`"
            >
              <span v-if="count">{{ sname }} {{ count }}</span>
            </div>
          </div>
        </div>
      </div>
    </q-card-section>
  </q-card>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
  title: { type: String, required: true },
  rows: { type: Array, required: true },
  emptyText: { type: String, default: "No data yet." },
  kind: { type: String, default: "bar" }, // "bar" | "latency" | "status"
});

function pct(num, max) {
  if (!max || max <= 0) return 0;
  return Math.min(100, (num / max) * 100);
}

const maxCount = computed(() =>
  props.kind === "bar"
    ? Math.max(1, ...props.rows.map((r) => r.value || 0))
    : 1,
);

const maxLatency = computed(() =>
  props.kind === "latency"
    ? Math.max(1, ...props.rows.map((r) => r.p95 || 0))
    : 1,
);
</script>

<style lang="scss" scoped>
.metrics-panel {
  background: var(--theme-bg-panel);
  color: var(--theme-text-primary);
}
.panel-bar {
  font-family: "JetBrains Mono", monospace;
}
.title {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 1.05rem;
  letter-spacing: 0.05em;
}
.empty {
  color: #777;
  font-family: "JetBrains Mono", monospace;
  font-style: italic;
}
.bar-grid {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-family: "JetBrains Mono", monospace;
  font-size: 0.85rem;
}
.bar-row,
.lat-row,
.status-row {
  display: grid;
  grid-template-columns: 220px 1fr 80px;
  align-items: center;
  gap: 0.5rem;
}
// Latency rows render `p50 / p95` in the right-hand numbers column;
// give it more breathing room so values like ``1234 / 5678`` don't
// crowd against the bar.
.lat-row {
  grid-template-columns: 220px 1fr 125px;
}
.bar-key {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: #ff9d63;
}
.bar-track {
  position: relative;
  height: 14px;
  background: var(--theme-bg-code);
  border-radius: 2px;
  overflow: hidden;
}
.bar-fill {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  background: linear-gradient(90deg, var(--theme-accent-primary), var(--theme-accent-secondary));
}
.bar-p50 {
  background: var(--theme-accent-info);
  z-index: 2;
}
.bar-p95 {
  background: var(--theme-accent-primary);
  z-index: 1;
}
.bar-num {
  text-align: right;
  color: var(--theme-text-primary);
}
.num-p50 {
  color: var(--theme-accent-info);
}
.num-sep {
  color: #555;
  margin: 0 4px;
}
.num-p95 {
  color: var(--theme-accent-primary);
}
.status-track {
  display: flex;
  height: 14px;
  background: var(--theme-bg-code);
  border-radius: 2px;
  overflow: hidden;
}
.status-seg {
  position: relative;
  font-size: 0.65rem;
  display: flex;
  align-items: center;
  padding: 0 4px;
  white-space: nowrap;
  color: #fff;
  letter-spacing: 0.04em;
  overflow: hidden;
  // Floor every segment at a width that fits "<status> <count>" so a
  // low-count outcome (e.g. one ``escalated_only`` page among many
  // ``enriched`` pages) doesn't shrink to a sliver and clip its label.
  // The track's ``overflow: hidden`` still bounds the row when the
  // floor would push the total past the available width.
  min-width: 110px;
  // Default background so an unrecognised status (one we haven't
  // styled below) is still visible against the panel rather than
  // rendering transparent.
  background: #455a64;
}
.status-ok {
  background: #2e7d32;
}
.status-low_confidence {
  background: #f9a825;
  color: var(--theme-bg-page);
}
.status-exhausted {
  background: #c62828;
}
.status-error,
.status-failed {
  background: #6a1b9a;
}
.status-not_found,
.status-page_deleted {
  background: #455a64;
}
.status-unset {
  background: #444;
}
// Enrichment-pipeline outcomes (bp_service.enrich_page /
// sd_service.enrich_page).
.status-enriched {
  background: #1565c0;
}
.status-unchanged {
  background: #616161;
}
.status-escalated_only,
.status-escalation,
.status-escalations_emitted {
  background: #ef6c00;
}
.status-stub_created {
  background: #00796b;
}
// Async task lifecycle (orchestrator /v1/tasks).
.status-accepted,
.status-in_progress {
  background: #5e35b1;
}
.status-completed {
  background: #2e7d32;
}
</style>
