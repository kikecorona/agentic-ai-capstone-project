<template>
  <!--
    Section header for the §9.8.4 Dashboard. A retro-orange label + a
    thin underline to delimit the Service-vs-Agent metrics split.
  -->
  <div class="section-header q-mt-lg q-mb-sm">
    <q-icon :name="icon" class="q-mr-sm section-icon" />
    <span class="section-label">{{ label }}</span>
    <span class="section-sub">{{ sub }}</span>
  </div>
</template>

<script setup>
defineProps({
  icon: { type: String, default: "" },
  label: { type: String, required: true },
  sub: { type: String, default: "" },
});
</script>

<style lang="scss" scoped>
.section-header {
  display: flex;
  align-items: center;
  border-bottom: 2px solid var(--theme-accent-primary);
  padding: 8px 4px;
  margin-top: 8px;
  margin-bottom: 12px;
}
.section-icon {
  color: var(--theme-accent-primary);
}
.section-label {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 1.3rem;
  letter-spacing: 0.08em;
  color: var(--theme-accent-primary);
  text-transform: uppercase;
}
.section-sub {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.78rem;
  color: #888;
  margin-left: 12px;
}
</style>
