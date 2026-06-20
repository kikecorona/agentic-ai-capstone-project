<template>
  <q-page class="documentation-page">
    <!--
      Single docs entry point. Rooted at the repo's `documentation/`
      directory so the explorer can step into `bp/`, `sd/`, or any
      future sibling without needing its own tab.

      ``?file=<path>`` deep-links open the file directly — used by
      the chat sources list (each source becomes a router-link).
    -->
    <docs-viewer base-path="documentation" :initial-file="initialFile" />
  </q-page>
</template>

<script setup>
import { computed } from "vue";
import { useRoute } from "vue-router";
import DocsViewer from "components/DocsViewer.vue";

const route = useRoute();
const initialFile = computed(() => {
  const f = route.query?.file;
  return Array.isArray(f) ? f[0] : (f || "");
});
</script>
