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
    <div class="col-4 q-pa-md left-pane">
      <q-toolbar class="bg-grey-9 text-white q-mb-sm rounded-borders">
        <q-icon name="forum" class="q-mr-sm" />
        <span class="retro-display">Pending</span>
        <q-space />
        <q-btn flat dense round icon="refresh" :loading="loading" @click="reload" />
      </q-toolbar>
      <q-banner v-if="error" class="bg-red-9 text-white">
        <template v-slot:avatar><q-icon name="error" /></template>
        {{ error }}
      </q-banner>
      <q-list bordered separator class="rounded-borders pending-list">
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
            <q-item-label class="topic">{{ q.topic }}</q-item-label>
            <q-item-label caption class="qid-line">
              <code>{{ q.question_id }}</code>
              · {{ q.originating_pages.length }} page(s)
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

    <!-- Right: detail + reply form -->
    <div class="col q-pa-md right-pane">
      <div v-if="!selected" class="empty-state q-pa-lg">
        Pick a pending question from the left to see its context and post a reply.
      </div>
      <div v-else class="column no-wrap full-height">
        <q-toolbar class="bg-grey-8 text-white q-mb-sm rounded-borders">
          <q-icon name="help_outline" class="q-mr-sm" />
          <span class="ellipsis topic">{{ selected.topic }}</span>
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
          <q-card-section class="column no-wrap full-height">
            <div class="row items-center q-mb-sm">
              <q-input
                v-model="smeId"
                dense
                filled
                dark
                label="your SME id"
                placeholder="e.g. alice"
                style="min-width: 220px"
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
              autogrow
              filled
              dark
              label="reply text"
              placeholder="Markdown is fine — this becomes the new B&P page body and the link inserted into every originating page."
              class="reply-textarea col"
            />
          </q-card-section>
        </q-card>
      </div>
    </div>
  </q-page>
</template>

<script setup>
import { computed, inject, onMounted, ref } from "vue";
import { useQuasar } from "quasar";

const oc = inject("oc");
const $q = useQuasar();

const questions = ref([]);
const selected = ref(null);
const loading = ref(false);
const posting = ref(false);
const error = ref("");
const smeId = ref(localStorage.getItem("portal.smeId") || "");
const smeText = ref("");

const canPost = computed(
  () => !!selected.value && !!smeText.value.trim() && !posting.value,
);

const trailJson = computed(() =>
  selected.value
    ? JSON.stringify(selected.value.retrieval_trail || [], null, 2)
    : "",
);

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
    selected.value = null;
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
.left-pane,
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
}
.empty-state {
  color: #888;
  font-family: "JetBrains Mono", monospace;
}
.pending-list {
  background: var(--theme-bg-panel);
}
</style>
