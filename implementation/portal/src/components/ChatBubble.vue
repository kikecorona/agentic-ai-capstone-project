<template>
  <!--
    §9.8 chat bubble — sits in a fixed corner across every tab and
    fires `POST /v1/queries` against the orchestrator. Domain hint
    follows the active tab when known, defaulting to "both" otherwise.
  -->
  <div class="chat-bubble">
    <!-- Toggle button (the bubble itself) -->
    <q-btn
      v-if="!open"
      round
      size="lg"
      color="primary"
      icon="forum"
      class="chat-fab"
      @click="open = true"
    >
      <q-tooltip class="bg-grey-9">Ask the agents</q-tooltip>
    </q-btn>

    <!-- Expanded panel -->
    <q-card v-else flat bordered class="chat-panel column no-wrap">
      <q-toolbar class="bg-primary text-white">
        <q-icon name="forum" class="q-mr-sm" />
        <span class="retro-display">Ask the agents</span>
        <q-space />
        <q-select
          v-model="domainHint"
          :options="['both', 'bp', 'sd']"
          dense
          dark
          borderless
          options-dense
          class="domain-select"
        />
        <q-btn flat dense round icon="close" @click="open = false" />
      </q-toolbar>

      <q-scroll-area ref="scroll" class="col chat-stream">
        <div class="stream-pad">
          <div v-if="!messages.length" class="empty-hint">
            <p>Ask anything about the documented products or services.</p>
          </div>
          <div
            v-for="m in messages"
            :key="m.id"
            class="msg"
            :class="`msg-${m.role}`"
          >
            <div class="msg-meta">
              <span class="msg-role">{{ m.role }}</span>
              <span v-if="m.status" class="msg-status">{{ m.status }}</span>
              <span v-if="m.dispatchedTo" class="msg-status">
                → {{ m.dispatchedTo }}
              </span>
            </div>
            <div class="msg-body" v-html="m.html" />
            <div v-if="m.sources && m.sources.length" class="msg-sources">
              <div class="sources-label">sources</div>
              <ul>
                <li v-for="(s, i) in m.sources" :key="i">
                  <code>{{ s.source_uri }}</code>
                  <span v-if="s.distance != null" class="src-d">
                    d={{ s.distance.toFixed(3) }}
                  </span>
                </li>
              </ul>
            </div>
          </div>
          <div v-if="loading" class="msg msg-pending">
            <q-spinner-puff color="accent" size="1.4em" />
            <span class="q-ml-sm">thinking…</span>
          </div>
        </div>
      </q-scroll-area>

      <div class="chat-input-bar">
        <q-input
          v-model="draft"
          @keyup.enter="send"
          :disable="loading"
          dense
          filled
          dark
          autogrow
          placeholder="Ask a question…"
          class="col"
        />
        <q-btn
          color="accent"
          text-color="dark"
          icon="send"
          :loading="loading"
          :disable="!draft.trim()"
          @click="send"
        />
      </div>
    </q-card>
  </div>
</template>

<script setup>
import { inject, nextTick, ref } from "vue";
import { marked } from "marked";

const oc = inject("oc");

const open = ref(false);
const draft = ref("");
const loading = ref(false);
const messages = ref([]);
const scroll = ref(null);
const domainHint = ref("both");

// Domain hint defaults to "both" — the merged Documentation tab covers
// both BP and SD content so we can't pick from the route alone.
// Operators override via the dropdown in the toolbar when they know.

let _id = 0;
function nextId() {
  _id += 1;
  return _id;
}

async function send() {
  const q = draft.value.trim();
  if (!q || loading.value) return;
  draft.value = "";
  messages.value.push({
    id: nextId(),
    role: "you",
    html: escapeHtml(q),
    sources: null,
  });
  loading.value = true;
  await scrollToBottom();
  try {
    const res = await oc.post("/v1/queries", {
      query: q,
      domain_hint: domainHint.value,
    });
    const data = res.data || {};
    const answerMd = data.answer || "_(no answer returned)_";
    messages.value.push({
      id: nextId(),
      role: "agent",
      status: data.status,
      dispatchedTo: data.dispatched_to,
      html: marked.parse(answerMd),
      sources: (data.sources || []).slice(0, 6),
    });
  } catch (e) {
    messages.value.push({
      id: nextId(),
      role: "error",
      html: escapeHtml(
        e?.response?.data?.detail || e.message || "request failed",
      ),
      sources: null,
    });
  } finally {
    loading.value = false;
    await scrollToBottom();
  }
}

async function scrollToBottom() {
  await nextTick();
  if (scroll.value && scroll.value.setScrollPercentage) {
    scroll.value.setScrollPercentage("vertical", 1, 0);
  }
}

function escapeHtml(s) {
  return (s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\n/g, "<br>");
}
</script>

<style lang="scss" scoped>
.chat-bubble {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 3000;
}
.chat-fab {
  box-shadow: 0 6px 18px rgba(255, 107, 53, 0.4);
}
.chat-panel {
  width: 380px;
  height: 540px;
  max-width: calc(100vw - 48px);
  max-height: calc(100vh - 96px);
  background: var(--theme-bg-page);
  color: var(--theme-text-primary);
  box-shadow: 0 12px 36px rgba(0, 0, 0, 0.45);
  border-color: var(--theme-accent-primary);
}
.retro-display {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 1.05rem;
  letter-spacing: 0.05em;
}
.domain-select {
  font-family: "JetBrains Mono", monospace;
  min-width: 88px;
  font-size: 0.8rem;
}
.chat-stream {
  background: var(--theme-bg-deep);
}
.stream-pad {
  padding: 10px 12px;
}
.empty-hint {
  color: #888;
  font-family: "JetBrains Mono", monospace;
  font-size: 0.85rem;
}
.msg {
  margin-bottom: 12px;
  padding: 8px 10px;
  border-radius: 4px;
  font-family: "JetBrains Mono", monospace;
  font-size: 0.85rem;
  line-height: 1.5;
}
.msg-you {
  background: rgba(255, 107, 53, 0.12);
  border-left: 3px solid var(--theme-accent-primary);
}
.msg-agent {
  background: rgba(0, 172, 193, 0.1);
  border-left: 3px solid var(--theme-accent-info);
}
.msg-error {
  background: rgba(255, 82, 82, 0.12);
  border-left: 3px solid #ff5252;
  color: #ffb3b3;
}
.msg-pending {
  display: flex;
  align-items: center;
  color: #888;
  font-family: "JetBrains Mono", monospace;
  font-size: 0.85rem;
}
.msg-meta {
  display: flex;
  gap: 8px;
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--theme-accent-secondary);
  margin-bottom: 4px;
}
.msg-role {
  font-weight: 600;
}
.msg-status {
  color: var(--theme-accent-info);
}
.msg-body :deep(h1),
.msg-body :deep(h2),
.msg-body :deep(h3) {
  font-size: 1.4rem;
  margin: 0.4rem 0 0.2rem;
  color: var(--theme-accent-primary);
  font-family: "VT323", "JetBrains Mono", monospace;
  letter-spacing: 0.03em;
}
.msg-body :deep(p) {
  margin: 0.3rem 0;
}
.msg-body :deep(code) {
  background: var(--theme-bg-code);
  padding: 1px 4px;
  border-radius: 2px;
}
.msg-body :deep(a) {
  color: var(--theme-accent-secondary);
}
.msg-sources {
  margin-top: 6px;
  border-top: 1px dashed var(--theme-bg-code);
  padding-top: 4px;
}
.sources-label {
  font-size: 0.65rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #888;
}
.msg-sources ul {
  margin: 2px 0 0 0;
  padding-left: 1rem;
  font-size: 0.75rem;
}
.src-d {
  color: #888;
  margin-left: 6px;
}
.chat-input-bar {
  display: flex;
  gap: 6px;
  align-items: flex-end;
  padding: 8px;
  background: var(--theme-bg-panel);
  border-top: 1px solid var(--theme-bg-code);
}
</style>
