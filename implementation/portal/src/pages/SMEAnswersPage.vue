<template>
  <!--
    §9.8.2 SME Answers tab.
    Pulls pending escalations from GET /v1/sme-questions, lets the SME
    answer each one inline (text box + submit per question), and POST a
    reply that the orchestrator's ingest_sme_reply node fans out into
    BP page patches (§9.5). The right pane renders the originating
    page for the most recently clicked question so the SME can read
    the affected doc without leaving the tab.
  -->
  <q-page class="sme-page row no-wrap">
    <!-- Left: pending list — each question is a self-contained card
         with its own reply textarea + submit, so the SME doesn't have
         to pick one and pivot to a separate form. -->
    <div class="col-6 q-pa-md left-pane column no-wrap">
      <q-toolbar class="bg-grey-9 text-white q-mb-sm rounded-borders pending-header">
        <q-icon name="forum" class="q-mr-sm" />
        <span class="retro-display">Pending</span>
        <q-space />
        <q-input
          v-model="smeId"
          dense
          filled
          dark
          label="SME id"
          placeholder="alice"
          class="sme-id-input q-mr-sm"
        />
        <q-btn flat dense round icon="refresh" :loading="loading" @click="reload" />
      </q-toolbar>
      <q-banner v-if="error" class="bg-red-9 text-white q-mb-sm">
        <template v-slot:avatar><q-icon name="error" /></template>
        {{ error }}
      </q-banner>
      <div class="col scroll pending-scroll">
        <q-card
          v-for="q in questions"
          :key="q.question_id"
          flat
          bordered
          class="pending-card q-mb-sm"
          :class="{ active: selected && selected.question_id === q.question_id }"
          @click="select(q)"
        >
          <q-card-section class="q-pa-md">
            <div class="row items-center q-mb-xs">
              <q-icon
                :name="q.domain === 'sd' ? 'memory' : 'storefront'"
                :color="q.domain === 'sd' ? 'amber' : 'orange'"
                size="20px"
                class="q-mr-sm"
              />
              <span class="topic ellipsis">{{ q.topic }}</span>
              <q-space />
              <code class="qid-chip">{{ q.question_id }}</code>
              <q-chip dense square color="accent" text-color="dark" class="q-ml-sm">
                {{ q.domain }}
              </q-chip>
            </div>

            <div class="text-caption text-grey-5 q-mt-sm">Question</div>
            <div class="question-text q-mt-xs">{{ q.question }}</div>

            <div class="text-caption text-grey-5 q-mt-md">
              Best guess (low-confidence)
            </div>
            <div class="best-guess q-mt-xs">
              {{ q.best_guess || "(none)" }}
            </div>

            <div class="text-caption text-grey-5 q-mt-md">Originating pages</div>
            <ul class="origin-list">
              <li v-for="p in q.originating_pages || []" :key="p">
                <code>{{ p }}</code>
              </li>
              <li v-if="!(q.originating_pages || []).length">
                <span class="text-grey-6">(none)</span>
              </li>
            </ul>

            <q-expansion-item
              dense
              dark
              icon="data_object"
              label="retrieval trail"
              header-class="trail-header q-mt-sm text-grey-5"
              class="trail-expand"
              @click.stop
            >
              <pre class="trail">{{ trailJson(q) }}</pre>
            </q-expansion-item>

            <!-- Inline reply: own textarea + submit per question, so
                 the SME can answer without selecting and switching to
                 a separate form panel. -->
            <q-input
              :model-value="replies[q.question_id] || ''"
              @update:model-value="(v) => (replies[q.question_id] = v)"
              type="textarea"
              filled
              dark
              label="reply"
              placeholder="Markdown is fine — this becomes the new B&P page body and the link inserted into every originating page."
              autogrow
              class="reply-textarea q-mt-md"
              input-class="reply-textarea-input"
              @click.stop
            />
            <div class="row q-mt-sm justify-end">
              <q-btn
                color="primary"
                icon="send"
                label="post reply"
                :loading="!!postingFor[q.question_id]"
                :disabled="!canPost(q)"
                @click.stop="post(q)"
              />
            </div>
          </q-card-section>
        </q-card>

        <div v-if="!loading && questions.length === 0" class="empty-state q-pa-md">
          No pending SME questions. The agent will surface one here when
          <code>dispatch_refresh</code> emits an escalation envelope.
        </div>
      </div>
    </div>

    <q-separator vertical />

    <!-- Right: render the originating page for the most recently
         clicked card, keyed on ``pageReloadKey`` so a successful post
         re-mounts the viewer and bypasses raw.githubusercontent.com's
         HTTP cache. -->
    <div class="col-6 q-pa-md origin-render column no-wrap">
      <q-toolbar class="bg-grey-9 text-white q-mb-sm rounded-borders origin-header">
        <q-icon name="article" class="q-mr-sm" />
        <span class="retro-display">Originating page</span>
        <q-space />
        <q-btn
          v-if="originPageFile"
          flat
          dense
          round
          icon="refresh"
          @click="pageReloadKey += 1"
        >
          <q-tooltip class="bg-grey-9">Re-fetch from GitHub</q-tooltip>
        </q-btn>
      </q-toolbar>
      <div class="origin-frame col rounded-borders">
        <docs-viewer
          v-if="originPageFile"
          :key="`${originPageFile}:${pageReloadKey}`"
          base-path="documentation"
          :initial-file="originPageFile"
          hide-tree
          class="full-height"
        />
        <div v-else class="empty-state q-pa-lg">
          Pick a pending question on the left to render its originating page here.
        </div>
      </div>
    </div>
  </q-page>
</template>

<script setup>
import { computed, inject, onMounted, reactive, ref } from "vue";
import { useQuasar } from "quasar";
import DocsViewer from "components/DocsViewer.vue";

const oc = inject("oc");
const $q = useQuasar();

const questions = ref([]);
const selected = ref(null);
const loading = ref(false);
const error = ref("");
const smeId = ref(localStorage.getItem("portal.smeId") || "Your friendly SME");

// Per-question reply state. Keyed by question_id so each card owns
// its own draft and posting flag — switching cards never blows away
// what the SME has typed elsewhere.
const replies = reactive({});
const postingFor = reactive({});

// Bumped after a successful post so the originating-page DocsViewer
// re-mounts and re-fetches from raw.githubusercontent.com — the SME
// sees the patched body without having to navigate away.
const pageReloadKey = ref(0);

function canPost(q) {
  return (
    !!q &&
    !!(replies[q.question_id] || "").trim() &&
    !postingFor[q.question_id]
  );
}

function trailJson(q) {
  return JSON.stringify(q?.retrieval_trail || [], null, 2);
}

// Page path the right-hand DocsViewer renders. The SME-question
// envelope holds the originating URI as either a bare relative path
// (``architecture/foo.md``, written by the specialist) or a
// repo-rooted path (``documentation/sd/...``) — normalise to the
// rooted form using the ``domain`` field on the question.
const originPageFile = computed(() => {
  const q = selected.value;
  if (!q) return "";
  const first = (q.originating_pages || [])[0];
  if (!first) return "";
  let p = String(first).trim().replace(/^\/+/, "");
  if (!p) return "";
  if (p.startsWith("documentation/")) return p;
  if (/^(bp|sd)\//.test(p)) return `documentation/${p}`;
  if (q.domain === "bp" || q.domain === "sd") {
    return `documentation/${q.domain}/${p}`;
  }
  return p;
});

async function reload() {
  loading.value = true;
  error.value = "";
  try {
    const res = await oc.get("/v1/sme-questions", {
      params: { status: "pending" },
    });
    questions.value = res.data || [];
    if (
      selected.value &&
      !questions.value.some((q) => q.question_id === selected.value.question_id)
    ) {
      selected.value = null;
    }
  } catch (e) {
    error.value = e?.response?.status
      ? `HTTP ${e.response.status} from /v1/sme-questions`
      : e.message;
  } finally {
    loading.value = false;
  }
}

// Selecting a card only drives the right-hand originating-page
// render — it does NOT touch ``replies`` so the SME's draft is
// preserved across clicks.
function select(q) {
  selected.value = q;
}

async function post(q) {
  if (!canPost(q)) return;
  postingFor[q.question_id] = true;
  try {
    const res = await oc.post("/v1/sme-replies", {
      question_id: q.question_id,
      sme_id: smeId.value || null,
      sme_text: replies[q.question_id],
    });
    const patched = (res.data?.patched_pages || [])
      .filter((p) => p.patched)
      .map((p) => p.page_uri);
    $q.notify({
      type: "positive",
      message: patched.length
        ? `Patched ${patched.length} page(s): ${patched.join(", ")}`
        : "Reply persisted; no pages were patched (placeholder may already be cleared).",
      timeout: 6000,
      multiLine: true,
    });
    if (smeId.value) localStorage.setItem("portal.smeId", smeId.value);
    delete replies[q.question_id];
    // If the SME was looking at this question's originating page,
    // bump the reload key so the patched body shows up. Drop the
    // answered question from the local list so it doesn't sit in
    // the pending pane after being resolved; the periodic refresh
    // would do this anyway, this just makes it immediate.
    if (selected.value && selected.value.question_id === q.question_id) {
      pageReloadKey.value += 1;
    }
    questions.value = questions.value.filter(
      (x) => x.question_id !== q.question_id,
    );
    await reload();
  } catch (e) {
    error.value = e?.response?.data?.detail || e.message;
    $q.notify({
      type: "negative",
      message: `Reply failed: ${error.value}`,
      timeout: 6000,
    });
  } finally {
    postingFor[q.question_id] = false;
  }
}

onMounted(reload);
</script>

<style lang="scss" scoped>
.sme-page {
  height: calc(100vh - 200px);
}
.left-pane {
  overflow: hidden;
}
.pending-header {
  flex: 0 0 auto;
}
.sme-id-input {
  width: 160px;
}
.pending-scroll {
  background: transparent;
}
.retro-display {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 1.15rem;
  letter-spacing: 0.05em;
}
.topic {
  font-family: "JetBrains Mono", monospace;
  font-weight: 600;
}
// Question id rendered next to the topic in the card header. Uses
// the JetBrains-Mono treatment (matching the qid styling on the
// originating-page caption) but tones the colour down so it sits
// next to the retro topic display without competing.
.qid-chip {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.7rem;
  color: var(--theme-text-muted, #aaa);
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 6px;
  border-radius: 2px;
  letter-spacing: 0.04em;
}
.question-text {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.95rem;
  line-height: 1.5;
}
.best-guess {
  font-family: "JetBrains Mono", monospace;
  color: #cfc7b0;
}
.origin-list {
  margin: 4px 0 0 0;
  padding-left: 1.25rem;
  font-family: "JetBrains Mono", monospace;
}
.trail {
  background: var(--theme-bg-deeper);
  padding: 0.5rem 0.75rem;
  border-left: 3px solid var(--theme-accent-info);
  font-family: "JetBrains Mono", monospace;
  font-size: 0.75rem;
  white-space: pre-wrap;
  max-height: 220px;
  overflow: auto;
  margin: 0.25rem 0 0 0;
}
.trail-expand :deep(.q-expansion-item__container > .q-item) {
  padding-left: 0;
  padding-right: 0;
  min-height: 28px;
}
.reply-textarea :deep(textarea) {
  font-family: "JetBrains Mono", monospace;
  min-height: 90px;
  // Cap the visible height so the textarea always shows scrollbars
  // instead of pushing the whole card taller as the SME types.
  max-height: 240px;
  overflow-y: auto;
  resize: vertical;
}
.empty-state {
  color: #888;
  font-family: "JetBrains Mono", monospace;
}
.pending-card {
  background: var(--theme-bg-panel);
  color: var(--theme-text-primary);
  cursor: pointer;
  border-color: var(--theme-accent-primary);
  transition: border-color 0.15s ease, background-color 0.15s ease;
}
.pending-card:hover {
  border-color: var(--theme-accent-info);
}
.pending-card.active {
  border-color: var(--theme-accent-info);
  background: var(--theme-bg-deep);
}
.pending-card :deep(.text-grey-5) {
  color: var(--theme-text-muted, #aaa) !important;
}
.pending-card :deep(.q-field--filled .q-field__control) {
  background: var(--theme-bg-deep);
}
// Originating-page container: same toolbar + bordered/rounded body
// shape as the Pending list on the left so the two halves of the
// SME tab read as a pair.
.origin-render {
  overflow: hidden;
}
.origin-header {
  flex: 0 0 auto;
}
.origin-frame {
  background: var(--theme-bg-panel);
  border: 1px solid var(--theme-accent-primary);
  overflow: hidden;
  // Isolate layout/style from siblings so a reflow inside one of
  // the reply textareas doesn't drag the rendered markdown +
  // mermaid SVGs through a layout pass too.
  contain: layout style;
}
// DocsViewer paints its own 4px accent left border on
// ``.markdown-body``; suppress it inside ``.origin-frame`` so only
// the container border is visible.
.origin-frame :deep(.markdown-body) {
  border-left: 0;
}
</style>
