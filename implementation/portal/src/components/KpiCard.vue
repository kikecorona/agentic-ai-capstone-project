<template>
  <!--
    Headline KPI tile for the §9.8.4 Dashboard. A single number with a
    label above and an optional sub-label below. The accent colour
    comes from the Quasar palette (accent / warning / negative) so
    threshold-crossing tiles can flip colour without re-styling.
    The sub-label slot always renders (placeholder   when empty)
    so cards in the same row stay vertically equal.
  -->
  <q-card flat bordered class="kpi-card column">
    <div class="kpi-label">{{ label }}</div>
    <div class="kpi-value" :class="`text-${accent}`">{{ value }}</div>
    <div class="kpi-sub">{{ sub || " " }}</div>
  </q-card>
</template>

<script setup>
defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], required: true },
  sub: { type: String, default: "" },
  // Quasar palette token: accent / warning / negative / positive / etc.
  accent: { type: String, default: "accent" },
});
</script>

<style lang="scss" scoped>
.kpi-card {
  background: var(--theme-bg-panel);
  color: var(--theme-text-primary);
  border-color: var(--theme-bg-code);
  padding: 12px 14px;
  height: 100%;
}
.kpi-label {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 0.95rem;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: #888;
}
.kpi-value {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 2.2rem;
  letter-spacing: 0.04em;
  line-height: 1.05;
  margin-top: 4px;
  word-break: break-word;
}
.kpi-sub {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.78rem;
  color: #888;
  margin-top: 2px;
  word-break: break-word;
}
</style>
