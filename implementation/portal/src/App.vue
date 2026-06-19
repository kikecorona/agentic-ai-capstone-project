<template>
  <!--
    Capstone POC — Documentation Portal (§9.8)
    Single root component; layouts + pages are driven by Vue Router
    so this stays intentionally minimal.
  -->
  <router-view />
</template>

<script setup>
// No script body — the layout component owns header/footer and
// provides the active branch via Pinia, see src/stores/settings.js.
</script>
