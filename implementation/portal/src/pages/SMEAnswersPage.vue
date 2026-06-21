<template>
  <!--
    §9.8.2 SME Answers tab.
    Pulls pending escalations from GET /v1/sme-questions, lets the SME
    pick one, see context (originating pages, retrieval trail, agent's
    best guess), and POST a reply that the orchestrator's
    ingest_sme_reply node fans out into BP page patches (§9.5).
  -->
  <q-page class="sme-page row no-wrap">
    <!-- Left: pending list -->
    <div class="col-4 q-pa-md left-pane column no-wrap">
      <q-toolbar class="bg-grey-9 text-white q-mb-sm rounded-borders pending-header">
        <q-icon name="forum" class="q-mr-sm" />
        <span class="retro-display">Pending</span>
        <q-space />
        <q-btn flat dense round icon="refresh" :loading="loading" @click="reload" />
      </q-toolbar>
      <q-banner v-if="error" class="bg-red-9 text-white">
        <template v-slot:avatar><q-icon name="error" /></template>
        {{ error }}
      </q-banner>
      <q-list bordered separator class="rounded-borders pending-list col scroll">
        <q-item
          v-for="q in questions"
          :key="q.question_id"
          :active="selected && selected.question_id === q.question_id"
          clickable
          @click="select(q)"
        >
          <q-item-section avatar>
            <q-icon
              :name="q.domain === 'sd' ? 'memory' : 'storefront'"
              :color="q.domain === 'sd' ? 'amber' : 'orange'"
              size="22px"
            />
          </q-item-section>
          <q-item-section>
            <q-item-label class="topic">
              {{ q.topic }}
              <span v-if="originLabel(q)" class="origin-suffix">
                ({{ originLabel(q) }})
              </span>
            </q-item-label>
            <q-item-label caption class="qid-line">
              <code>{{ q.question_id }}</code>
            </q-item-label>
          </q-item-section>
        </q-item>
        <q-item v-if="!loading && questions.length === 0">
          <q-item-section>
            <q-item-label class="text-grey-5 q-pa-sm">
              No pending SME questions. The agent will surface one here when
              <code>dispatch_refresh</code> emits an escalation envelope.
            </q-item-label>
          </q-item-section>
        </q-item>
      </q-list>
    </div>

    <q-separator vertical />

    <!-- Right: split into detail+reply form on the left, rendered
         originating page on the right so the SME can read the
         affected doc without leaving the tab. The page re-renders
         after a successful post (``pageReloadKey`` bump) so the
         SME sees the patched body. -->
    <div class="col row no-wrap right-pane">
      <div v-if="!selected" class="col empty-state q-pa-lg">
        Pick a pending question from the left to see its context and post a reply.
      </div>
      <template v-else>
        <div class="col-6 q-pa-md sme-form column no-wrap full-height">
          <q-toolbar class="bg-grey-9 text-white q-mb-sm rounded-borders">
            <q-icon name="help_outline" class="q-mr-sm" />
            <span class="retro-display ellipsis">{{ selected.topic }}</span>
            <code class="qid-chip q-ml-sm">{{ selected.question_id }}</code>
            <q-space />
            <q-chip dense square color="accent" text-color="dark">
              {{ selected.domain }}
            </q-chip>
          </q-toolbar>

          <q-card flat bordered class="q-mb-sm">
            <q-card-section>
              <div class="text-caption text-grey-5">Question</div>
              <div class="question-text q-mt-xs">{{ selected.question }}</div>
              <div class="text-caption text-grey-5 q-mt-md">
                Best guess (low-confidence)
              </div>
              <div class="best-guess q-mt-xs">
                {{ selected.best_guess || "(none)" }}
              </div>
            </q-card-section>
          </q-card>

          <q-card flat bordered class="q-mb-sm">
            <q-card-section>
              <div class="text-caption text-grey-5">Originating pages</div>
              <ul class="origin-list">
                <li v-for="p in selected.originating_pages" :key="p">
                  <code>{{ p }}</code>
                </li>
              </ul>
              <div class="text-caption text-grey-5 q-mt-md">Retrieval trail</div>
              <pre class="trail">{{ trailJson }}</pre>
            </q-card-section>
          </q-card>

          <q-card flat bordered class="reply-card col">
            <q-card-section class="column no-wrap full-height full-width">
              <div class="row items-center q-mb-sm">
                <q-input
                  v-model="smeId"
                  dense
                  filled
                  dark
                  label="your SME id"
                  placeholder="e.g. alice"
                />
                <q-space />
                <q-btn
                  color="primary"
                  icon="send"
                  label="post reply"
                  :loading="posting"
                  :disabled="!canPost"
                  @click="post"
                />
              </div>
              <q-input
                v-model="smeText"
                type="textarea"
                input-class="reply-textarea-input"
                filled
                dark
                label="reply text"
                placeholder="Markdown is fine — this becomes the new B&P page body and the link inserted into every originating page."
                class="reply-textarea col"
              />
            </q-card-section>
          </q-card>
        </div>

        <q-separator vertical />

        <!-- Right side: render the originating page so the SME can
             read the affected doc in-place. Container shape mirrors
             the "Pending" pane — toolbar + bordered/rounded scroll
             body — so both halves of the tab feel consistent.
             Keyed on ``pageReloadKey`` so re-mounting after a
             successful post bypasses raw.githubusercontent.com's
             HTTP cache and the patched body shows up. -->
        <div class="col-6 q-pa-md origin-render column no-wrap">
          <q-toolbar class="bg-grey-9 text-white q-mb-sm rounded-borders origin-header">
            <q-icon name="article" class="q-mr-sm" />
            <span class="retro-display">Originating page</span>
            <q-space />
            <q-btn
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
              (this question has no originating page recorded)
            </div>
          </div>
        </div>
      </template>
    </div>
  </q-page>
</template>

<script setup>
import { computed, inject, onMounted, ref } from "vue";
import { useQuasar } from "quasar";
import DocsViewer from "components/DocsViewer.vue";

const oc = inject("oc");
const $q = useQuasar();

const questions = ref([]);
const selected = ref(null);
const loading = ref(false);
const posting = ref(false);
const error = ref("");
const smeId = ref(localStorage.getItem("portal.smeId") || "Your friendly SME");
const smeText = ref("");
// Bumped after a successful post so the originating-page DocsViewer
// re-mounts and re-fetches from raw.githubusercontent.com — the SME
// sees the patched body without having to navigate away.
const pageReloadKey = ref(0);

const canPost = computed(
  () => !!selected.value && !!smeText.value.trim() && !posting.value,
);

const trailJson = computed(() =>
  selected.value
    ? JSON.stringify(selected.value.retrieval_trail || [], null, 2)
    : "",
);

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

function select(q) {
  selected.value = q;
  smeText.value = "";
}

// Render the originating page list for the pending list — strip the
// ``documentation/{sd|bp}/`` prefix so paths fit into the row, and
// if the question covers more than one page, append a ``+N more``
// hint instead of dumping the whole list.
function originLabel(q) {
  const pages = q?.originating_pages || [];
  if (!pages.length) return "";
  const first = String(pages[0]).replace(/^documentation\/(sd|bp)\//, "");
  if (pages.length === 1) return first;
  return `${first} +${pages.length - 1} more`;
}

async function post() {
  if (!canPost.value) return;
  posting.value = true;
  try {
    const res = await oc.post("/v1/sme-replies", {
      question_id: selected.value.question_id,
      sme_id: smeId.value || null,
      sme_text: smeText.value,
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
    smeText.value = "";
    // Force the originating-page DocsViewer to re-mount and re-fetch
    // so the SME sees the patched body. We deliberately KEEP
    // ``selected`` populated so the rendered page stays on screen —
    // the operator pivots to another question (or refreshes) when
    // they're ready. Drop the answered question from the local list
    // so it doesn't sit in the pending pane after being resolved;
    // the periodic refresh would do this anyway, this just makes it
    // immediate.
    pageReloadKey.value += 1;
    questions.value = questions.value.filter(
      (q) => q.question_id !== selected.value.question_id,
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
    posting.value = false;
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
  // Column flex container: header (toolbar + optional error banner)
  // stays pinned at the top while ``.pending-list.scroll`` takes the
  // remaining vertical space and scrolls on overflow.
}
.pending-header {
  flex: 0 0 auto;
}
.right-pane {
  overflow: auto;
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
// Question id rendered next to the topic in the selected-question
// header. Uses the JetBrains-Mono treatment (matching the rest of
// the qid styling on the pending list) but tones the colour down
// so it sits next to the retro topic display without competing.
.qid-chip {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.7rem;
  color: var(--theme-text-muted, #aaa);
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 6px;
  border-radius: 2px;
  letter-spacing: 0.04em;
}
.origin-suffix {
  color: var(--theme-text-muted, #888);
  font-weight: 400;
  font-size: 0.85em;
}
.qid-line {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.75rem;
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
}
.reply-card {
  display: flex;
  min-height: 280px;
}
.reply-textarea :deep(textarea) {
  font-family: "JetBrains Mono", monospace;
  min-height: 180px;
  // Cap the visible height so the textarea always shows scrollbars
  // instead of pushing the whole reply card taller (which would
  // reflow the docs viewer next door on every keystroke).
  max-height: 320px;
  overflow-y: auto;
  resize: vertical;
}
.empty-state {
  color: #888;
  font-family: "JetBrains Mono", monospace;
}
.pending-list {
  background: var(--theme-bg-panel);
  // Match the originating-page container's accent border so the
  // two halves of the tab read as a pair. Quasar's ``bordered``
  // class paints a low-contrast grey by default; override both
  // colour and width so the panel pops against the dark theme.
  border: 1px solid var(--theme-accent-primary);
}
// Originating-page container: same toolbar + bordered/rounded body
// shape as the Pending list on the left so the two halves of the
// SME tab read as a pair. ``DocsViewer`` paints its own surfaces
// inside; we just give it the frame.
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
  // Isolate layout/style from siblings so a reflow inside the reply
  // form (textarea grow, autocorrect underline) doesn't drag the
  // rendered markdown + mermaid SVGs through a layout pass too.
  contain: layout style;
}
// DocsViewer paints its own 4px accent left border on
// ``.markdown-body``; that's nice on the Documentation tab where
// there's no outer container, but inside ``.origin-frame`` it
// clashes with the container's own 1px accent border. Suppress it
// here so only the container border is visible.
.origin-frame :deep(.markdown-body) {
  border-left: 0;
}
// Theme the right-pane cards (question/best-guess, originating pages,
// reply form). Quasar's default ``.q-card`` is a white surface that
// blows out on the dark themes; we drop in the panel colour, theme
// border, and theme text so the SME view matches the rest of the app.
.right-pane :deep(.q-card) {
  background: var(--theme-bg-panel);
  color: var(--theme-text-primary);
  border-color: var(--theme-accent-primary);
}
.right-pane :deep(.q-card .text-grey-5) {
  color: var(--theme-text-muted, #aaa) !important;
}
.right-pane :deep(.q-field--filled .q-field__control) {
  background: var(--theme-bg-deep);
}
</style>
