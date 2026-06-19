<template>
  <q-page class="documentation-page">
    <!--
      Single docs entry point. Rooted at the repo's `documentation/`
      directory so the explorer can step into `bp/`, `sd/`, or any
      future sibling without needing its own tab.
    -->
    <docs-viewer base-path="documentation" />
  </q-page>
</template>

<script setup>
import DocsViewer from "components/DocsViewer.vue";
</script>
