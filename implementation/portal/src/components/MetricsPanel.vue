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
  background: #1c1f33;
  color: #f5e6d3;
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
.bar-key {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: #ff9d63;
}
.bar-track {
  position: relative;
  height: 14px;
  background: #2c2c3e;
  border-radius: 2px;
  overflow: hidden;
}
.bar-fill {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  background: linear-gradient(90deg, #ff6b35, #ffd600);
}
.bar-p50 {
  background: #00acc1;
  z-index: 2;
}
.bar-p95 {
  background: #ff6b35;
  z-index: 1;
}
.bar-num {
  text-align: right;
  color: #f5e6d3;
}
.num-p50 {
  color: #00acc1;
}
.num-sep {
  color: #555;
  margin: 0 4px;
}
.num-p95 {
  color: #ff6b35;
}
.status-track {
  display: flex;
  height: 14px;
  background: #2c2c3e;
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
}
.status-ok {
  background: #2e7d32;
}
.status-low_confidence {
  background: #f9a825;
  color: #1a1a2e;
}
.status-exhausted {
  background: #c62828;
}
.status-error {
  background: #6a1b9a;
}
.status-not_found {
  background: #455a64;
}
.status-unset {
  background: #444;
}
</style>
