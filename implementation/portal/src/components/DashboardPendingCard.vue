<template>
  <!--
    Placeholder tile for §9.7 metrics whose data source isn't wired
    yet (BP/SD doc_index, OC SME state, validator pass, etc.). Same
    chrome as KpiCard but visually dimmed + dashed border so the
    operator can tell the data isn't live.
  -->
  <q-card flat bordered class="pending-card column">
    <div class="pending-label">{{ label }}</div>
    <div class="pending-value">pending</div>
    <div class="pending-sub">{{ sub }}</div>
    <div class="pending-source">{{ source }}</div>
  </q-card>
</template>

<script setup>
defineProps({
  label: { type: String, required: true },
  sub: { type: String, default: "" },
  source: { type: String, default: "" },
});
</script>

<style lang="scss" scoped>
.pending-card {
  background: #1c1f33;
  color: #888;
  border-color: #2c2c3e;
  border-style: dashed;
  padding: 12px 14px;
  height: 100%;
}
.pending-label {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 0.95rem;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: #888;
}
.pending-value {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 1.6rem;
  letter-spacing: 0.04em;
  line-height: 1.05;
  margin-top: 4px;
  color: #5a5a6e;
  font-style: italic;
}
.pending-sub {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.74rem;
  color: #888;
  margin-top: 4px;
  word-break: break-word;
}
.pending-source {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.7rem;
  color: #5a5a6e;
  margin-top: 6px;
  font-style: italic;
}
</style>
