<template>
  <!--
    §9.8.3 branch selector. Three modes:
      * known branches (main, starting-point) via the dropdown
      * sd-* / bp-* tags fetched from GitHub, shown under a separator
      * "custom…" reveals a free-text input that accepts any ref name
    Selected branch lives in the Pinia settings store; the docs viewer
    re-fetches on watch().
  -->
  <div class="row items-center q-gutter-sm branch-selector">
    <q-icon name="alt_route" size="20px" />
    <q-select
      :model-value="settings.branch"
      emit-value
      map-options
      @update:model-value="onSelect"
      :options="options"
      dense
      filled
      dark
      hide-bottom-space
      label="branch"
      class="branch-dropdown"
      style="min-width: 180px"
      :loading="tagsLoading"
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
    <q-btn
      flat
      dense
      round
      icon="swap_horiz"
      :color="settings.compareMode ? 'accent' : 'white'"
      @click="settings.toggleCompareMode()"
    >
      <q-tooltip class="bg-grey-9">
        {{ settings.compareMode ? "disable" : "enable" }} branch compare
      </q-tooltip>
    </q-btn>
    <template v-if="settings.compareMode">
      <q-select
        :model-value="settings.compareBranch"
        emit-value
        map-options
        @update:model-value="onSelectCompare"
        :options="options"
        dense
        filled
        dark
        hide-bottom-space
        label="compare"
        style="min-width: 180px"
        :loading="tagsLoading"
      />
      <q-input
        v-if="customCompareMode"
        v-model="customCompareText"
        @keyup.enter="commitCustomCompare"
        @blur="commitCustomCompare"
        dense
        filled
        dark
        label="compare ref"
        placeholder="e.g. feature/foo"
        style="min-width: 180px"
      >
        <template v-slot:append>
          <q-btn dense flat round icon="check" @click="commitCustomCompare" />
        </template>
      </q-input>
    </template>
  </div>
</template>

<script setup>
import { computed, inject, onMounted, ref, watch } from "vue";
import { useSettingsStore } from "stores/settings.js";

const props = defineProps({
  owner: { type: String, default: "kikecorona" },
  repo:  { type: String, default: "pear-store" },
});

const settings = useSettingsStore();
const gh = inject("gh");

const customMode = ref(settings.isCustomBranch);
const customText = ref(settings.customBranchInput || settings.branch);
const tags = ref([]);
const tagsLoading = ref(false);

// All refs that can be selected directly (no custom box needed).
const knownRefs = computed(() => [...settings.knownBranches, ...tags.value]);

// Reflect initial state once we know what's a known ref.
function syncCustomMode() {
  customMode.value = !knownRefs.value.includes(settings.branch);
}

const options = computed(() => {
  const items = settings.knownBranches.map((b) => ({ label: b, value: b }));
  for (const t of tags.value) {
    items.push({ label: t, value: t });
  }
  items.push({ label: "custom…", value: "custom…" });
  return items;
});

function onSelect(value) {
  if (!value) return;
  if (value === "custom…") {
    customMode.value = true;
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

const customCompareMode = ref(false);
const customCompareText = ref("");

function onSelectCompare(value) {
  if (!value) return;
  if (value === "custom…") {
    customCompareMode.value = true;
    if (!customCompareText.value) customCompareText.value = settings.compareBranch;
    return;
  }
  customCompareMode.value = false;
  settings.setCompareBranch(value);
}

function commitCustomCompare() {
  const v = (customCompareText.value || "").trim();
  if (!v) return;
  settings.setCompareBranch(v);
}

async function fetchTags() {
  if (!gh) return;
  tagsLoading.value = true;
  try {
    const res = await gh.get(`/repos/${props.owner}/${props.repo}/tags`, {
      params: { per_page: 100 },
    });
    tags.value = (res.data || [])
      .map((t) => t.name)
      .filter((n) => n.startsWith("sd-") || n.startsWith("bp-"))
      .sort();
    syncCustomMode();
  } catch (_) {
    // tags are best-effort — ignore failures
  } finally {
    tagsLoading.value = false;
  }
}

onMounted(fetchTags);

// Keep the textbox in sync when the store changes externally.
watch(
  () => settings.branch,
  (b) => {
    if (settings.isCustomBranch && !tags.value.includes(b)) {
      customText.value = b;
    }
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
