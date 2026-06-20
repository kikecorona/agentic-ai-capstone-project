<template>
  <!--
    §9.8.4 Multi-Agents X-Ray — collapsible right-side drawer content.
    Subscribes to the merged `/v1/streams/events` SSE feed and renders
    every event (service log + LLM call) in a single console pane,
    descending order. Click any row → detail dialog with full text.

    The toolbar also exposes a refresh button that fires the
    orchestrator's `POST /v1/refresh` and tracks the resulting task
    via `GET /v1/tasks/{id}` polling. The task id is held in cookies
    (24h max-age, scoped to the portal origin) so re-clicking while a
    task is still running re-attaches to the existing one rather than
    spawning a new refresh, and an in-flight task survives a browser
    restart.
  -->
  <div class="x-ray-drawer column no-wrap">
    <q-toolbar class="bg-grey-9 text-white q-mb-sm">
      <q-icon :name="paused ? 'pause' : 'play_arrow'" class="q-mr-sm" />
      <span class="retro-display">Multi-Agents X-Ray</span>
      <q-space />
      <q-btn
        flat
        dense
        :icon="paused ? 'play_arrow' : 'pause'"
        :label="paused ? 'resume events' : 'pause events'"
        @click="togglePause"
      >
        <q-tooltip class="bg-grey-9">
          {{
            paused
              ? "Resume the SSE stream and accept new events"
              : "Pause incoming events (existing rows stay visible)"
          }}
        </q-tooltip>
      </q-btn>
      <q-btn flat dense icon="clear_all" label="clear events" @click="clear">
        <q-tooltip class="bg-grey-9">
          Drop every event from the buffer (the upstream stream keeps tailing)
        </q-tooltip>
      </q-btn>
      <!-- Refresh control. While a task is in flight we render a plain
           button that opens the dialog onto the existing task — no
           need to expose the menu since both options would be locked
           anyway. Otherwise we render a q-btn-dropdown whose entire
           surface opens the mode-picker menu (no split / no default
           action), so the operator always picks "changed" vs "force"
           explicitly. -->
      <q-btn
        v-if="refreshInFlight"
        flat
        dense
        icon="hourglass_top"
        label="running…"
        @click="refreshDialogOpen = true"
      >
        <q-tooltip class="bg-grey-9">
          Refresh in progress — click to view
        </q-tooltip>
      </q-btn>
      <q-btn-dropdown
        v-else
        flat
        dense
        icon="refresh"
        label="refresh"
        :loading="refreshLoading"
        content-class="xray-refresh-menu"
      >
        <q-tooltip class="bg-grey-9">
          Pick a refresh mode: changed-only or force re-index
        </q-tooltip>
        <q-list>
          <q-item
            clickable
            v-close-popup
            @click="onRefreshClick({ force: false })"
          >
            <q-item-section avatar>
              <q-icon name="refresh" color="accent" />
            </q-item-section>
            <q-item-section>
              <q-item-label>Refresh changed</q-item-label>
              <q-item-label caption>
                Skip docs whose content hash matches the inventory
              </q-item-label>
            </q-item-section>
          </q-item>
          <q-item
            clickable
            v-close-popup
            @click="onRefreshClick({ force: true })"
          >
            <q-item-section avatar>
              <q-icon name="autorenew" color="warning" />
            </q-item-section>
            <q-item-section>
              <q-item-label>Force re-index</q-item-label>
              <q-item-label caption>
                Re-run the pipeline on every doc, bypassing skip-unchanged
              </q-item-label>
            </q-item-section>
          </q-item>
        </q-list>
      </q-btn-dropdown>
    </q-toolbar>

    <log-console
      class="col"
      title="events"
      :events="events"
      :status="status"
    />

    <!-- Refresh progress dialog. ``persistent`` keeps it from being
         dismissed by click-outside, but the close button works at any
         time — including while a task is in flight. The polling lives
         on the component, not the dialog, so closing just hides the
         UI; the ``xray_refreshTaskId`` cookie carries the in-flight
         task across closes (and across full browser restarts) so the
         next click on the toolbar's "running…" button re-attaches to
         the SAME task instead of spawning a new refresh. -->
    <q-dialog v-model="refreshDialogOpen" persistent>
      <q-card class="refresh-card">
        <q-toolbar class="bg-grey-9 text-white">
          <q-icon name="refresh" class="q-mr-sm" />
          <span class="title">Refresh task</span>
          <q-space />
          <q-btn
            flat
            dense
            round
            icon="close"
            @click="refreshDialogOpen = false"
          >
            <q-tooltip class="bg-grey-9">
              {{
                refreshInFlight
                  ? "Close — task keeps running; reopen via the toolbar's “running…” button"
                  : "Close"
              }}
            </q-tooltip>
          </q-btn>
        </q-toolbar>

        <q-card-section class="refresh-meta">
          <div class="meta-grid">
            <div class="meta-key">task id</div>
            <div class="meta-val">{{ refreshTaskId || "—" }}</div>
            <div class="meta-key">mode</div>
            <div class="meta-val">
              <q-chip
                dense
                square
                :color="refreshForce ? 'warning' : 'accent'"
                text-color="dark"
              >
                {{ refreshForce ? "force re-index" : "refresh changed" }}
              </q-chip>
            </div>
            <div class="meta-key">status</div>
            <div class="meta-val">
              <q-chip
                dense
                square
                :color="refreshStatusColor"
                text-color="dark"
              >
                {{ refreshTask?.status || "pending" }}
              </q-chip>
            </div>
            <div class="meta-key">started</div>
            <div class="meta-val">
              {{ refreshTask?.started_at ? formatTs(refreshTask.started_at) : "—" }}
            </div>
            <div v-if="refreshTask?.completed_at" class="meta-key">
              completed
            </div>
            <div v-if="refreshTask?.completed_at" class="meta-val">
              {{ formatTs(refreshTask.completed_at) }}
              <span class="duration-hint">
                ({{ formatDuration(refreshTask) }})
              </span>
            </div>
          </div>
        </q-card-section>

        <q-linear-progress
          :indeterminate="refreshInFlight"
          :value="refreshInFlight ? undefined : 1"
          :color="refreshTask?.status === 'failed' ? 'negative' : 'accent'"
          size="6px"
        />

        <q-card-section v-if="refreshError" class="refresh-error">
          <div class="section-label error-label">error</div>
          <pre class="refresh-pre">{{ refreshError }}</pre>
        </q-card-section>

        <q-card-section
          v-else-if="refreshTask?.result"
          class="refresh-result"
        >
          <div class="section-label">result</div>
          <pre class="refresh-pre">{{ formatResult(refreshTask.result) }}</pre>
        </q-card-section>

        <q-card-section v-else class="refresh-running">
          <div class="running-line">
            <q-spinner-puff color="accent" size="1.4em" />
            <span class="q-ml-sm">
              waiting for orchestrator… polling every {{ POLL_INTERVAL_MS }}ms
            </span>
          </div>
        </q-card-section>
      </q-card>
    </q-dialog>
  </div>
</template>

<script setup>
import { computed, inject, onBeforeUnmount, ref } from "vue";
import LogConsole from "components/LogConsole.vue";

const ocBase = inject("ocBase", "http://127.0.0.1:8000");
const oc = inject("oc");

// ─── SSE event stream (existing) ───────────────────────────────────
const events = ref([]);
const status = ref("connecting…");
const paused = ref(false);

const MAX_BUFFER = 500;

let src = null;

function openStream() {
  const url = `${ocBase}/v1/streams/events?since_seconds_ago=3600`;
  console.info(`[XRay] opening SSE → ${url}`);
  const es = new EventSource(url);
  status.value = "connecting…";
  es.onopen = () => {
    console.info(`[XRay] /v1/streams/events opened (readyState=${es.readyState})`);
    status.value = "live";
  };
  es.onerror = (e) => {
    console.warn(
      `[XRay] /v1/streams/events error (readyState=${es.readyState})`,
      e,
    );
    status.value = "reconnecting…";
  };
  es.onmessage = (ev) => {
    if (paused.value) return;
    try {
      const obj = JSON.parse(ev.data);
      events.value.push(obj);
      if (events.value.length > MAX_BUFFER) {
        events.value.splice(0, events.value.length - MAX_BUFFER);
      }
    } catch (err) {
      console.error("[XRay] /v1/streams/events parse failed:", err, "data:", ev.data);
    }
  };
  return es;
}

function startSse() {
  src = openStream();
}

function stopSse() {
  if (src) {
    src.close();
    src = null;
  }
  status.value = "paused";
}

function togglePause() {
  paused.value = !paused.value;
  if (paused.value) {
    stopSse();
  } else {
    startSse();
  }
}

function clear() {
  events.value = [];
}

// ─── Refresh task (orchestrator /v1/refresh) ───────────────────────
// Persisted to **cookies** so an in-flight task survives browser
// restarts — re-opening the tab re-attaches to the same task instead
// of spawning a new refresh. Cookie scope is the portal origin
// (``path=/``); a 24h max-age caps a stuck task from haunting future
// sessions and gets reset on every write so an active task keeps
// rolling. Cookies are cleared as soon as the task hits a terminal
// status (or returns 404 from the orchestrator after a restart).
const REFRESH_KEY = "xray_refreshTaskId";
const REFRESH_FORCE_KEY = "xray_refreshForce";
const COOKIE_MAX_AGE_S = 24 * 60 * 60; // 24 hours
const POLL_INTERVAL_MS = 1500;
const TERMINAL_STATUSES = new Set(["completed", "failed"]);

function _readCookie(name) {
  const safe = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m = document.cookie.match(new RegExp(`(?:^|; )${safe}=([^;]*)`));
  return m ? decodeURIComponent(m[1]) : "";
}

function _writeCookie(name, value) {
  document.cookie =
    `${name}=${encodeURIComponent(value)}; ` +
    `path=/; max-age=${COOKIE_MAX_AGE_S}; samesite=Lax`;
}

function _deleteCookie(name) {
  document.cookie = `${name}=; path=/; max-age=0; samesite=Lax`;
}

const refreshTaskId = ref(_readCookie(REFRESH_KEY) || "");
const refreshForce = ref(_readCookie(REFRESH_FORCE_KEY) === "1");
const refreshTask = ref(null);
const refreshError = ref("");
const refreshLoading = ref(false);
const refreshDialogOpen = ref(false);
let pollTimer = null;

const refreshInFlight = computed(() => {
  if (!refreshTaskId.value) return false;
  const s = refreshTask.value?.status;
  if (!s) return true;
  return !TERMINAL_STATUSES.has(s);
});

const refreshStatusColor = computed(() => {
  const s = refreshTask.value?.status || "pending";
  if (s === "completed") return "accent";
  if (s === "failed") return "red-5";
  if (s === "in_progress") return "amber";
  return "grey-5";
});

async function onRefreshClick(opts = {}) {
  // Already running → just open the dialog onto the existing task.
  if (refreshInFlight.value && refreshTaskId.value) {
    refreshDialogOpen.value = true;
    return;
  }
  // No in-flight task → fire a new POST /v1/refresh.
  const force = !!opts.force;
  refreshLoading.value = true;
  refreshError.value = "";
  refreshTask.value = null;
  refreshForce.value = force;
  try {
    const res = await oc.post("/v1/refresh", {
      event_type: "trigger_refresh",
      change_kind: "modified",
      source: "portal-xray",
      force,
    });
    const data = res.data || {};
    if (!data.task_id) throw new Error("no task_id in response");
    refreshTaskId.value = data.task_id;
    _writeCookie(REFRESH_KEY, data.task_id);
    _writeCookie(REFRESH_FORCE_KEY, force ? "1" : "0");
    // Seed task ref with the accept response so the dialog has
    // something to render before the first poll lands.
    refreshTask.value = {
      task_id: data.task_id,
      status: data.status || "accepted",
      started_at: data.accepted_at || Date.now() / 1000,
      completed_at: null,
      result: null,
      error: null,
    };
    refreshDialogOpen.value = true;
    startPoll();
  } catch (e) {
    refreshError.value = e?.response?.data?.detail || e.message || String(e);
    refreshDialogOpen.value = true;
  } finally {
    refreshLoading.value = false;
  }
}

async function pollOnce() {
  if (!refreshTaskId.value) return;
  try {
    const res = await oc.get(`/v1/tasks/${refreshTaskId.value}`);
    refreshTask.value = res.data || null;
    if (
      refreshTask.value &&
      TERMINAL_STATUSES.has(refreshTask.value.status)
    ) {
      stopPoll();
      // Clear the cookies so the NEXT click starts a fresh task. The
      // dialog stays open (operator dismisses manually) so the result
      // is visible after completion.
      _deleteCookie(REFRESH_KEY);
      _deleteCookie(REFRESH_FORCE_KEY);
      if (refreshTask.value.status === "failed") {
        refreshError.value =
          refreshTask.value.error || "task failed (no detail)";
      }
    }
  } catch (e) {
    // 404 → orchestrator never knew about this task (maybe it
    // restarted and lost in-memory state). Drop the stale id so the
    // next click creates a new one.
    if (e?.response?.status === 404) {
      console.warn(`[XRay] task ${refreshTaskId.value} not found — clearing`);
      stopPoll();
      _deleteCookie(REFRESH_KEY);
      _deleteCookie(REFRESH_FORCE_KEY);
      refreshError.value = "task not found on orchestrator (restart?)";
      refreshTaskId.value = "";
    } else {
      console.warn("[XRay] poll error:", e);
    }
  }
}

function startPoll() {
  stopPoll();
  // Fire one immediately so the dialog gets fresh state without
  // waiting a full poll interval, then recur.
  pollOnce();
  pollTimer = window.setInterval(pollOnce, POLL_INTERVAL_MS);
}

function stopPoll() {
  if (pollTimer) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

function formatTs(seconds) {
  if (!seconds) return "—";
  const d = new Date(seconds * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

function formatDuration(task) {
  if (!task?.started_at || !task?.completed_at) return "";
  const ms = (task.completed_at - task.started_at) * 1000;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function formatResult(result) {
  if (result == null) return "(empty)";
  if (typeof result === "string") return result;
  try {
    return JSON.stringify(result, null, 2);
  } catch (_) {
    return String(result);
  }
}

// ─── Lifecycle ─────────────────────────────────────────────────────
startSse();
// Resume polling silently if a previous mount started a refresh and
// it hasn't terminated yet — the cookie carries the task id across
// drawer open/close cycles AND across full browser restarts.
if (refreshTaskId.value) {
  startPoll();
}
onBeforeUnmount(() => {
  stopSse();
  stopPoll();
});
</script>

<style lang="scss" scoped>
.x-ray-drawer {
  height: 100%;
  background: var(--theme-bg-deep);
}
.retro-display {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 1.15rem;
  letter-spacing: 0.05em;
}
</style>

<!-- Unscoped block so the styles reach the q-btn-dropdown's menu, which
     Quasar teleports to <body> outside this component's scope token. -->
<style lang="scss">
.xray-refresh-menu {
  background: var(--theme-bg-page);
  color: var(--theme-text-primary);
  border: 1px solid var(--theme-accent-primary);
  font-family: "JetBrains Mono", monospace;

  .q-item {
    color: var(--theme-text-primary);
    min-height: 0;
  }
  .q-item:hover {
    background: rgba(255, 107, 53, 0.12);
  }
  .q-item--disabled {
    opacity: 0.5;
  }
  .q-item__label {
    color: var(--theme-text-primary);
  }
  .q-item__label--caption {
    color: #888;
    font-size: 0.72rem;
  }
  .q-separator {
    background: var(--theme-bg-code);
  }
}

// ──────────────────────────────────────────── refresh task dialog
.refresh-card {
  background: var(--theme-bg-page);
  color: var(--theme-text-primary);
  border: 1px solid var(--theme-accent-primary);
  width: min(560px, 92vw);
  max-width: 92vw;
  font-family: "JetBrains Mono", monospace;
}
.refresh-card .title {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 1.1rem;
  letter-spacing: 0.05em;
}
.refresh-meta {
  background: var(--theme-bg-panel);
  padding: 12px 16px;
  font-size: 0.85rem;
}
.meta-grid {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 4px 12px;
  align-items: center;
}
.meta-key {
  color: #888;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-size: 0.7rem;
}
.meta-val {
  color: var(--theme-text-primary);
  word-break: break-word;
}
.duration-hint {
  color: #888;
  margin-left: 6px;
  font-size: 0.75rem;
}
.refresh-running,
.refresh-error,
.refresh-result {
  background: var(--theme-bg-deep);
  padding: 12px 16px;
}
.running-line {
  display: flex;
  align-items: center;
  color: #aaa;
  font-size: 0.85rem;
}
.section-label {
  color: var(--theme-accent-secondary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-size: 0.7rem;
  margin-bottom: 4px;
}
.section-label.error-label {
  color: #ff5252;
}
.refresh-pre {
  background: var(--theme-bg-deeper);
  border-left: 3px solid var(--theme-accent-info);
  color: var(--theme-text-primary);
  padding: 8px 10px;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: "JetBrains Mono", monospace;
  font-size: 0.82rem;
  line-height: 1.45;
  max-height: 300px;
  overflow-y: auto;
}
.refresh-error .refresh-pre {
  border-left-color: #ff5252;
  color: #ffb3b3;
}
</style>
