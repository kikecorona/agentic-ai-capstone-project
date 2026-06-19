<template>
  <!--
    §9.8.3 branch selector. Three modes:
      * known branches (main, starting-point) via the dropdown
      * "custom…" reveals a free-text input that accepts any ref name
    Selected branch lives in the Pinia settings store; the docs viewer
    re-fetches on watch().
  -->
  <div class="row items-center q-gutter-sm branch-selector">
    <q-icon name="alt_route" size="20px" />
    <q-select
      :model-value="selectedOption"
      @update:model-value="onSelect"
      :options="options"
      dense
      filled
      dark
      hide-bottom-space
      label="branch"
      class="branch-dropdown"
      style="min-width: 180px"
    />
    <q-input
      v-if="customMode"
      v-model="customText"
      @keyup.enter="commitCustom"
      @blur="commitCustom"
      dense
      filled
      dark
      label="ref"
      placeholder="e.g. feature/foo"
      style="min-width: 180px"
    >
      <template v-slot:append>
        <q-btn dense flat round icon="check" @click="commitCustom" />
      </template>
    </q-input>
  </div>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { useSettingsStore } from "stores/settings.js";

const settings = useSettingsStore();

// Local "show the custom textbox" flag — separate from the active
// branch in the store. Picking "custom…" in the dropdown flips this on
// without touching the store, so the textbox stays visible until the
// operator actually commits a ref name.
const customMode = ref(settings.isCustomBranch);
const customText = ref(settings.customBranchInput || settings.branch);

const options = computed(() => [...settings.knownBranches, "custom…"]);

const selectedOption = computed(() =>
  customMode.value ? "custom…" : settings.branch,
);

function onSelect(value) {
  if (value === "custom…") {
    customMode.value = true;
    // Pre-fill the textbox with the current branch so the operator has
    // something to edit from rather than starting blank.
    if (!customText.value) customText.value = settings.branch;
    return;
  }
  customMode.value = false;
  settings.setBranch(value);
}

function commitCustom() {
  const v = (customText.value || "").trim();
  if (!v) return;
  settings.setBranch(v);
  settings.setCustomBranchInput(v);
}

// Keep the textbox in sync when the store changes externally
// (e.g. another component sets a branch programmatically).
watch(
  () => settings.branch,
  (b) => {
    if (settings.isCustomBranch) customText.value = b;
  },
);
</script>

<style lang="scss" scoped>
.branch-selector {
  margin-left: auto;
}
.branch-dropdown :deep(.q-field__native) {
  font-family: "JetBrains Mono", monospace;
}
</style>
