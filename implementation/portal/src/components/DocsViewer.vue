<template>
  <!--
    §9.8.2 BP / SD doc viewer — simple breadcrumb explorer.
    Left pane: current-directory listing with a clickable breadcrumb.
    Right pane: rendered Markdown for the selected file.
    No recursive tree, no lazy-load wiring — one directory request per
    click of either a folder or a breadcrumb segment.
  -->
  <div class="docs-viewer column no-wrap">
    <q-banner v-if="error" class="bg-red-9 text-white q-ma-md">
      <template v-slot:avatar>
        <q-icon name="error" />
      </template>
      <strong>GitHub fetch failed</strong> — {{ error }}
      <template v-slot:action>
        <q-btn flat dense label="retry" @click="reload" />
      </template>
    </q-banner>

    <div class="row col grow no-wrap">
      <!-- ─── Left pane: directory listing ─────────────────────────── -->
      <div v-if="!hideTree" class="docs-tree col-3 column">
        <q-toolbar class="bg-grey-9 text-white">
          <q-icon name="folder_open" class="q-mr-sm" />
          <q-breadcrumbs class="path-label" active-color="white">
            <q-breadcrumbs-el
              v-for="(crumb, i) in breadcrumbs"
              :key="i"
              :label="crumb.label"
              :class="{ 'cursor-pointer': i < breadcrumbs.length - 1 }"
              @click="i < breadcrumbs.length - 1 && goTo(crumb.path)"
            />
          </q-breadcrumbs>
          <q-space />
          <q-chip dense square color="accent" text-color="dark" class="branch-chip">
            {{ branch }}
          </q-chip>
        </q-toolbar>
        <q-scroll-area class="col">
          <q-list separator class="dir-list">
            <q-item
              v-if="canGoUp"
              clickable
              @click="goUp"
              class="up-item"
            >
              <q-item-section avatar>
                <q-icon name="arrow_upward" color="amber" />
              </q-item-section>
              <q-item-section>
                <q-item-label>..</q-item-label>
                <q-item-label caption>parent directory</q-item-label>
              </q-item-section>
            </q-item>
            <q-item
              v-for="entry in entries"
              :key="entry.path"
              clickable
              :active="entry.type === 'file' && entry.path === selectedPath"
              @click="onClickEntry(entry)"
            >
              <q-item-section avatar>
                <q-icon
                  :name="entry.type === 'dir' ? 'folder' : 'description'"
                  :color="entry.type === 'dir' ? 'orange' : 'cyan'"
                />
              </q-item-section>
              <q-item-section>
                <q-item-label class="ellipsis">{{ entry.name }}</q-item-label>
                <q-item-label caption v-if="entry.type === 'file' && entry.size != null">
                  {{ formatSize(entry.size) }}
                </q-item-label>
              </q-item-section>
            </q-item>
            <q-item v-if="loading">
              <q-item-section>
                <q-item-label>
                  <q-spinner-puff color="accent" size="1.2em" />
                  <span class="q-ml-sm">listing…</span>
                </q-item-label>
              </q-item-section>
            </q-item>
            <q-item v-else-if="!entries.length && !error">
              <q-item-section>
                <q-item-label class="text-grey-5">
                  (empty directory)
                </q-item-label>
              </q-item-section>
            </q-item>
          </q-list>
        </q-scroll-area>
      </div>

      <q-separator v-if="!hideTree" vertical />

      <!-- ─── Right pane: file render ─────────────────────────────── -->
      <div class="docs-body col column">
        <q-toolbar class="bg-grey-8 text-white">
          <q-icon name="article" class="q-mr-sm" />
          <span class="ellipsis path-label">
            {{ selectedPath || "(select a file)" }}
          </span>
          <q-space />
          <!-- Edit toggle: in view mode → enter editor; in edit
               mode → save (push to GitHub) and re-render. The save
               button is disabled while a write is in flight so
               accidental double-clicks don't queue two commits. -->
          <q-btn
            v-if="selectedPath && !editing"
            flat
            dense
            icon="edit"
            label="edit"
            class="edit-btn q-mr-sm"
            @click="startEdit"
          />
          <q-btn
            v-if="editing"
            flat
            dense
            icon="close"
            label="cancel"
            class="cancel-btn q-mr-sm"
            :disable="saving"
            @click="cancelEdit"
          />
          <q-btn
            v-if="editing"
            flat
            dense
            icon="save"
            label="save"
            color="accent"
            class="save-btn q-mr-sm"
            :loading="saving"
            :disable="!editorDirty"
            @click="saveEdit"
          />
          <q-btn
            v-if="selectedPath && !editing"
            flat
            dense
            icon="open_in_new"
            :href="githubBlobUrl"
            target="_blank"
            label="github"
            class="open-btn"
          />
        </q-toolbar>
        <q-scroll-area v-if="!editing" class="col">
          <div v-if="contentLoading" class="q-pa-md">
            <q-spinner-puff color="accent" size="2em" />
            <span class="q-ml-sm">fetching…</span>
          </div>
          <div v-else-if="!selectedPath" class="q-pa-lg empty-state">
            <p>Pick a file from the left to render its Markdown.</p>
            <p class="text-caption">
              Source:
              <a :href="githubTreeUrl" target="_blank">{{ currentPath }}</a>
              on branch
              <code>{{ branch }}</code>
            </p>
          </div>
          <article v-else class="markdown-body q-pa-lg" v-html="renderedHtml" />
        </q-scroll-area>
        <div v-else class="col column no-wrap editor-pane">
          <q-banner v-if="editError" class="bg-red-9 text-white">
            <template v-slot:avatar><q-icon name="error" /></template>
            {{ editError }}
          </q-banner>
          <textarea
            ref="editorRef"
            v-model="editorBody"
            class="md-editor col"
            spellcheck="false"
          />
          <div class="editor-footer">
            saving to <code>{{ branch }}</code> ·
            <code>{{ selectedPath }}</code>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, inject, nextTick, ref, watch } from "vue";
import { marked } from "marked";
import mermaid from "mermaid";
import { useSettingsStore } from "stores/settings.js";

const props = defineProps({
  // Repo root path (e.g. "documentation/bp"). Used as the deepest
  // boundary the explorer can climb back up to.
  basePath: { type: String, required: true },
  owner: { type: String, default: "kikecorona" },
  repo: { type: String, default: "pear-store" },
  // Optional: pre-select a specific file path inside the repo on
  // mount. Used by deep-links from the chat sources list (each
  // source is a router-link to ``/docs?file=...``).
  initialFile: { type: String, default: "" },
  // Optional: hide the left directory listing and only render the
  // selected file. Used by the SME Answers pane where we just want
  // to show the originating page next to the reply form.
  hideTree: { type: Boolean, default: false },
});

const gh = inject("gh");
const oc = inject("oc", null);
const settings = useSettingsStore();

// Where we are right now in the repo. Starts at basePath, changes via
// goTo / goUp. Always normalised (no leading slash).
const currentPath = ref(props.basePath);
const entries = ref([]);
const loading = ref(false);
const contentLoading = ref(false);
const error = ref("");
const selectedPath = ref("");
const renderedHtml = ref("");
// Raw markdown text alongside the rendered HTML so the manual
// editor (Edit button on the Documentation tab) opens the actual
// source instead of trying to recover Markdown from rendered HTML.
const rawText = ref("");

const branch = computed(() => settings.branch);

const canGoUp = computed(() => currentPath.value !== props.basePath);

const breadcrumbs = computed(() => {
  const segs = currentPath.value.split("/").filter(Boolean);
  const baseSegs = props.basePath.split("/").filter(Boolean);
  // Crumbs always anchor at basePath; deeper segments accumulate.
  const out = [];
  for (let i = 0; i < segs.length; i++) {
    out.push({
      label: segs[i],
      path: segs.slice(0, i + 1).join("/"),
    });
    // Anything before basePath isn't navigable here.
    if (i < baseSegs.length - 1) {
      out[i].fixed = true;
    }
  }
  return out;
});

const githubTreeUrl = computed(
  () =>
    `https://github.com/${props.owner}/${props.repo}/tree/${encodeURIComponent(branch.value)}/${currentPath.value}`,
);
const githubBlobUrl = computed(() =>
  selectedPath.value
    ? `https://github.com/${props.owner}/${props.repo}/blob/${encodeURIComponent(branch.value)}/${selectedPath.value}`
    : "",
);

// Mermaid: dark-ish 80s palette. `startOnLoad: false` so we only render
// the diagrams we deliberately drop into the page, not anything that
// happens to match `.mermaid` elsewhere.
mermaid.initialize({
  startOnLoad: false,
  securityLevel: "loose",
  theme: "base",
  // Mermaid validates colour values up-front and rejects `var(...)`,
  // so we keep literal hex here. Default seed = orange palette (the
  // shipped default theme); the settings-store watcher below pushes
  // the green palette when the operator toggles.
  themeVariables: _mermaidThemeVars(settings.theme || "orange"),
});

// Per-theme literal palette for mermaid (it doesn't honour CSS vars).
function _mermaidThemeVars(theme) {
  if (theme === "green") {
    return {
      background: "#050a05",
      primaryColor: "#0a1108",
      primaryTextColor: "#3fbf3f",
      primaryBorderColor: "#2fbf5f",
      lineColor: "#5cbf5c",
      secondaryColor: "#0d1d0d",
      tertiaryColor: "#000200",
      fontFamily: "'JetBrains Mono', monospace",
    };
  }
  return {
    background: "#1a1a2e",
    primaryColor: "#1c1f33",
    primaryTextColor: "#f5e6d3",
    primaryBorderColor: "#ff6b35",
    lineColor: "#00acc1",
    secondaryColor: "#2c2c3e",
    tertiaryColor: "#11121f",
    fontFamily: "'JetBrains Mono', monospace",
  };
}

// Custom marked renderer: ```mermaid blocks become a <pre class="mermaid">
// element that mermaid.run() can pick up after the HTML lands in the DOM.
// We also sniff for mermaid by content so untagged ``` fences (the LLM
// occasionally drops the language hint) still render as diagrams.
// Other code blocks fall through to marked's default highlighter.
const _MERMAID_FIRST_TOKEN = /^(?:sequenceDiagram|flowchart|graph|stateDiagram(?:-v2)?|classDiagram|erDiagram|journey|gantt|pie|gitGraph|mindmap|timeline|quadrantChart|requirementDiagram|C4Context|C4Container|C4Component|C4Dynamic|sankey-beta|xychart-beta)\b/;

function _looksLikeMermaid(text) {
  return _MERMAID_FIRST_TOKEN.test((text || "").trimStart());
}

marked.use({
  gfm: true,
  breaks: false,
  renderer: {
    code(token) {
      if (token.lang === "mermaid" || _looksLikeMermaid(token.text)) {
        return `<pre class="mermaid">${_escForMermaid(token.text)}</pre>`;
      }
      // `false` → fall back to the default code renderer.
      return false;
    },
  },
});

function _escForMermaid(text) {
  // Encode HTML special chars so the source survives DOM parsing.
  // Mermaid reads .textContent, which the browser decodes back to the
  // original chars — so this is invisible to the diagram engine.
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

async function reload() {
  loading.value = true;
  error.value = "";
  entries.value = [];
  try {
    // Same reasoning as ``loadFile`` — proxy through the
    // orchestrator's authenticated endpoint so a few clicks don't
    // exhaust the 60 req/hour anonymous quota on api.github.com.
    if (!oc) throw new Error("orchestrator client not provided");
    const res = await oc.get("/v1/docs/list", {
      params: { path: currentPath.value, ref: branch.value },
    });
    if (!Array.isArray(res.data)) {
      throw new Error(`expected directory listing for ${currentPath.value}`);
    }
    entries.value = res.data
      .map((e) => ({
        name: e.name,
        path: e.path,
        type: e.type,
        size: e.size,
      }))
      .sort((a, b) => {
        if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
        return a.name.localeCompare(b.name);
      });
  } catch (e) {
    error.value = e?.response?.status
      ? `HTTP ${e.response.status} fetching ${currentPath.value}@${branch.value}`
      : e.message || String(e);
  } finally {
    loading.value = false;
  }
}

function onClickEntry(entry) {
  if (entry.type === "dir") {
    goTo(entry.path);
  } else {
    selectedPath.value = entry.path;
    loadFile(entry.path);
  }
}

function goTo(path) {
  currentPath.value = path;
  // Clear right pane when navigating directories — the previously
  // shown file may no longer be visible from this level.
  selectedPath.value = "";
  renderedHtml.value = "";
  reload();
}

function goUp() {
  if (!canGoUp.value) return;
  const segs = currentPath.value.split("/").filter(Boolean);
  segs.pop();
  goTo(segs.join("/"));
}

// Open a specific file path (used both by router deep-links and any
// cross-component "open this doc" callers). The file path is the
// full repo path, e.g. ``documentation/sd/services/billing.md``.
// Sets ``currentPath`` to the parent dir, selects the file, and
// triggers the directory listing + content fetch.
function openFile(filePath) {
  if (!filePath) return;
  // Reject anything outside the basePath so the explorer doesn't
  // climb above its own root.
  const norm = String(filePath).replace(/^\/+/, "");
  if (!norm.startsWith(props.basePath)) return;
  const segs = norm.split("/").filter(Boolean);
  segs.pop();
  const parent = segs.join("/") || props.basePath;
  if (parent !== currentPath.value) {
    currentPath.value = parent;
    reload();
  }
  selectedPath.value = norm;
  loadFile(norm);
}

defineExpose({ openFile });

async function loadFile(path) {
  contentLoading.value = true;
  renderedHtml.value = "";
  rawText.value = "";
  try {
    // Route the read through the orchestrator's ``/v1/docs/raw``
    // proxy. Two reasons:
    //   * ``raw.githubusercontent.com`` sits behind Fastly with a
    //     multi-minute CDN TTL that ignores ``?v=`` cache busts and
    //     ``cache: no-store``, so the SME panel sees stale bodies
    //     for several minutes after a patch.
    //   * Going direct to ``api.github.com`` from the browser works
    //     anonymously but eats into the 60 req/hour shared anon
    //     limit fast (every render hits both the directory listing
    //     and the file body), so a few SME replies in quick
    //     succession produce 403s.
    // The orchestrator authenticates with the same PAT the GitHub
    // MCP uses, returns the file body verbatim, and isn't CDN-cached.
    const ocClient = oc;
    if (!ocClient) {
      throw new Error("orchestrator client not provided");
    }
    const res = await ocClient.get("/v1/docs/raw", {
      params: { path, ref: branch.value },
    });
    const text = typeof res.data?.content === "string" ? res.data.content : "";
    rawText.value = text;
    renderedHtml.value = path.toLowerCase().endsWith(".md")
      ? marked.parse(text)
      : `<pre>${escapeHtml(text)}</pre>`;
  } catch (e) {
    const msg = e?.response?.status
      ? `HTTP ${e.response.status} fetching ${path}`
      : e.message || String(e);
    renderedHtml.value = `<pre class="text-negative">Error: ${escapeHtml(msg)}</pre>`;
  } finally {
    contentLoading.value = false;
  }
  // The <article class="markdown-body"> only exists in the DOM once
  // `contentLoading` is false — running the mermaid sweep before this
  // point would silently no-op (querySelector finds nothing). Wait for
  // Vue to flush the v-html into the article, THEN render diagrams.
  await nextTick();
  await _renderMermaidBlocks();
}

// ─── Manual editor (Documentation tab) ─────────────────────────────
// Lets the operator open the raw Markdown in a textarea, edit it,
// and push the result to the configured branch via the orchestrator's
// ``POST /v1/docs/edit`` endpoint (which round-trips through the
// GitHub MCP). The render flips to the editor on ``Edit`` and back
// to the rendered Markdown after a successful save.
const editing = ref(false);
const editorBody = ref("");
const editorOriginal = ref("");
const saving = ref(false);
const editError = ref("");
const editorRef = ref(null);

const editorDirty = computed(() => editorBody.value !== editorOriginal.value);

function startEdit() {
  if (!selectedPath.value) return;
  // Strip the rendered-error banner so the editor opens with empty
  // content for files that failed to fetch — saving from that state
  // would just overwrite the doc with the error text.
  if (!rawText.value) {
    editError.value = "no raw content available — refresh the page first";
    return;
  }
  editorOriginal.value = rawText.value;
  editorBody.value = rawText.value;
  editError.value = "";
  editing.value = true;
}

function cancelEdit() {
  if (saving.value) return;
  editing.value = false;
  editorBody.value = "";
  editorOriginal.value = "";
  editError.value = "";
}

async function saveEdit() {
  if (!editorDirty.value || !selectedPath.value || !oc) return;
  saving.value = true;
  editError.value = "";
  try {
    await oc.post("/v1/docs/edit", {
      path: selectedPath.value,
      content: editorBody.value,
      branch: branch.value,
    });
    // Persist locally + flip back to view mode. The reload picks up
    // the new content from raw.githubusercontent.com — adding a
    // cache-bust query string forces a fresh fetch even though
    // GitHub usually serves the new revision immediately.
    rawText.value = editorBody.value;
    editing.value = false;
    await loadFile(selectedPath.value);
  } catch (e) {
    editError.value = e?.response?.data?.detail || e.message || "save failed";
  } finally {
    saving.value = false;
  }
}

function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// Belt-and-braces sweep run after marked emits HTML: collect every
// mermaid candidate (fenced ```mermaid blocks marked tagged with
// language-mermaid AND any plain <pre> whose decoded text starts with
// a mermaid keyword), then render each one via `mermaid.render()` and
// drop the resulting SVG into the DOM. Using `render()` per block is
// more reliable than `mermaid.run({querySelector})` across versions.
let _mermaidCounter = 0;

async function _renderMermaidBlocks() {
  const root = document.querySelector(".markdown-body");
  if (!root) {
    console.warn(
      "[DocsViewer] mermaid sweep skipped — .markdown-body not in DOM yet",
    );
    return;
  }

  const candidates = [];
  // 1. fenced ```mermaid → marked emits <pre><code class="language-mermaid">.
  root
    .querySelectorAll('pre > code[class*="language-mermaid"]')
    .forEach((code) => {
      const pre = code.parentElement;
      if (pre && !pre.classList.contains("mermaid-rendered")) {
        candidates.push({ pre, src: code.textContent || "" });
      }
    });
  // 2. content-sniffed: untagged ``` blocks that look like mermaid.
  root.querySelectorAll("pre").forEach((pre) => {
    if (pre.classList.contains("mermaid-rendered")) return;
    if (candidates.some((c) => c.pre === pre)) return;
    const src = (pre.textContent || "").trim();
    if (_looksLikeMermaid(src)) candidates.push({ pre, src });
  });

  if (!candidates.length) return;
  console.info(`[DocsViewer] rendering ${candidates.length} mermaid block(s)`);

  for (const { pre, src } of candidates) {
    const id = `mermaid-${++_mermaidCounter}`;
    try {
      const { svg } = await mermaid.render(id, src.trim());
      const host = document.createElement("div");
      host.className = "mermaid-rendered";
      host.innerHTML = svg;
      pre.replaceWith(host);
    } catch (e) {
      console.error(
        `[DocsViewer] mermaid render failed for block ${id}:`,
        e,
        "\nsource:\n",
        src,
      );
      // Mark so we don't retry this same broken block on a re-render.
      pre.classList.add("mermaid-rendered");
    }
  }
}

function formatSize(n) {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / 1024 / 1024).toFixed(1)} MiB`;
}

// Re-fetch when the operator switches branches, or when we mount with a
// different basePath. **Branch switch keeps both the current directory
// and the selected file** so the operator can compare the same page
// across branches without re-navigating; the directory + file content
// are simply re-fetched against the new branch ref. ``basePath``
// changes (BP vs SD pane in different mounts) reset the path because
// the old path may not be valid under the new root.
watch(
  branch,
  () => {
    // ``reload()`` lists a directory via the unauthenticated GitHub
    // Contents API and would burn a 60-req/hour anon quota when the
    // SME panel mounts (``hide-tree`` mode shows no tree, so the
    // listing is wasted anyway). Only run it when the tree is on
    // screen.
    if (!props.hideTree) reload();
    if (selectedPath.value) {
      loadFile(selectedPath.value);
    }
  },
  { immediate: true },
);
watch(
  () => props.basePath,
  (newBase) => {
    currentPath.value = newBase;
    selectedPath.value = "";
    renderedHtml.value = "";
    if (!props.hideTree) reload();
  },
);
// Deep-link: on mount (and on subsequent changes via the route query
// being reused) jump to the requested file. ``immediate: true`` so a
// page load with ``?file=...`` lands on that file without a manual
// click; subsequent changes also re-route the explorer.
watch(
  () => props.initialFile,
  (file) => {
    if (file) openFile(file);
  },
  { immediate: true },
);

// Theme flip: re-init mermaid with the matching literal palette and
// re-render any currently-visible diagrams so the existing SVGs flip
// too. Mermaid stores rendered SVGs keyed by id; resetting via
// `mermaid.initialize()` and re-running the merge sweep is enough.
watch(
  () => settings.theme,
  async (theme) => {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "loose",
      theme: "base",
      themeVariables: _mermaidThemeVars(theme),
    });
    if (selectedPath.value && renderedHtml.value) {
      // Re-render the current file so the diagrams pick up the new
      // palette — cheaper than a full reload.
      await loadFile(selectedPath.value);
    }
  },
);
</script>

<style lang="scss" scoped>
.docs-viewer {
  height: calc(100vh - 200px);
}
.docs-tree {
  background: var(--theme-bg-panel);
  color: var(--theme-text-primary);
}
.dir-list {
  background: var(--theme-bg-panel);
}
.up-item {
  background: rgba(255, 214, 0, 0.06);
}
.path-label {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.85rem;
}
.path-label :deep(.q-breadcrumbs__el) {
  color: var(--theme-text-primary);
}
.branch-chip {
  font-family: "JetBrains Mono", monospace;
}
.empty-state {
  color: #888;
  font-family: "JetBrains Mono", monospace;
}
.markdown-body {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.95rem;
  line-height: 1.55;
  color: var(--theme-text-primary);
  background: var(--theme-bg-page);
  border-left: 4px solid var(--theme-accent-primary);
  min-height: 100%;
}
// Tighter heading sizes — the Press Start 2P pixel font + browser
// defaults made these absurdly large. Keep the retro look on the
// font + colour, tone the scale down to documentation-readable sizes.
.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4),
.markdown-body :deep(h5),
.markdown-body :deep(h6) {
  font-family: "VT323", "JetBrains Mono", monospace;
  color: var(--theme-accent-primary);
  letter-spacing: 0.04em;
  line-height: 1.4;
  margin: 1.4rem 0 0.6rem;
}
.markdown-body :deep(h1) { font-size: 1.35rem; }
.markdown-body :deep(h2) { font-size: 1.2rem; }
.markdown-body :deep(h3) { font-size: 1.1rem; }
.markdown-body :deep(h4) { font-size: 1.05rem; }
.markdown-body :deep(h5),
.markdown-body :deep(h6) { font-size: 1rem; color: var(--theme-accent-secondary); }
.markdown-body :deep(a) {
  color: var(--theme-accent-secondary);
}
.markdown-body :deep(code) {
  background: var(--theme-bg-code);
  padding: 1px 4px;
  border-radius: 2px;
  font-size: 0.85em;
}
.markdown-body :deep(pre) {
  background: var(--theme-bg-deeper);
  padding: 0.75rem 1rem;
  overflow-x: auto;
  border-left: 3px solid var(--theme-accent-info);
}
.markdown-body :deep(blockquote) {
  border-left: 3px solid var(--theme-accent-secondary);
  padding-left: 1rem;
  color: #cfc7b0;
}
.markdown-body :deep(table) {
  border-collapse: collapse;
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid #444;
  padding: 4px 8px;
}
// Mermaid container: matches the rest of the panel and lets the SVG
// scale to the available width without bleeding past the column.
.markdown-body :deep(pre.mermaid),
.markdown-body :deep(.mermaid-rendered) {
  background: var(--theme-bg-deeper);
  border-left: 3px solid var(--theme-accent-primary);
  padding: 0.75rem;
  text-align: center;
  overflow-x: auto;
  margin: 0.8rem 0;
}
.markdown-body :deep(pre.mermaid svg),
.markdown-body :deep(.mermaid-rendered svg) {
  max-width: 100%;
  height: auto;
}

// ─── Manual editor ─────────────────────────────────────────────────
.editor-pane {
  background: var(--theme-bg-deep);
}
.md-editor {
  width: 100%;
  border: 0;
  outline: 0;
  resize: none;
  background: var(--theme-bg-deep);
  color: var(--theme-text-primary);
  padding: 16px;
  font-family: "JetBrains Mono", monospace;
  font-size: 0.85rem;
  line-height: 1.55;
  white-space: pre;
  // ``tab-size`` keeps Markdown indentation predictable in the editor.
  tab-size: 2;
}
.editor-footer {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.7rem;
  color: var(--theme-text-muted, #888);
  padding: 6px 12px;
  border-top: 1px solid var(--theme-bg-code);
  background: var(--theme-bg-panel);
}
.editor-footer code {
  background: transparent;
  color: var(--theme-accent-secondary);
}
</style>
