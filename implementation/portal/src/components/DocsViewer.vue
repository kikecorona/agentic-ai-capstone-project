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
      <div class="docs-tree col-3 column">
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

      <q-separator vertical />

      <!-- ─── Right pane: file render ─────────────────────────────── -->
      <div class="docs-body col column">
        <q-toolbar class="bg-grey-8 text-white">
          <q-icon name="article" class="q-mr-sm" />
          <span class="ellipsis path-label">
            {{ selectedPath || "(select a file)" }}
          </span>
          <q-space />
          <q-btn
            v-if="selectedPath"
            flat
            dense
            icon="open_in_new"
            :href="githubBlobUrl"
            target="_blank"
            label="github"
            class="open-btn"
          />
        </q-toolbar>
        <q-scroll-area class="col">
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
});

const gh = inject("gh");
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
  themeVariables: {
    background: "#1a1a2e",
    primaryColor: "#1c1f33",
    primaryTextColor: "#f5e6d3",
    primaryBorderColor: "#ff6b35",
    lineColor: "#00acc1",
    secondaryColor: "#2c2c3e",
    tertiaryColor: "#11121f",
    fontFamily: "'JetBrains Mono', monospace",
  },
});

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
    const res = await gh.get(
      `/repos/${props.owner}/${props.repo}/contents/${encodeURI(currentPath.value)}`,
      { params: { ref: branch.value } },
    );
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

async function loadFile(path) {
  contentLoading.value = true;
  renderedHtml.value = "";
  try {
    // raw.githubusercontent.com gives us the body directly without
    // base64. Anonymous works for public repos.
    const url = `https://raw.githubusercontent.com/${props.owner}/${props.repo}/${encodeURIComponent(branch.value)}/${path}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status} fetching raw ${path}`);
    const text = await res.text();
    renderedHtml.value = path.toLowerCase().endsWith(".md")
      ? marked.parse(text)
      : `<pre>${escapeHtml(text)}</pre>`;
  } catch (e) {
    renderedHtml.value = `<pre class="text-negative">Error: ${escapeHtml(
      e.message || String(e),
    )}</pre>`;
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
// different basePath (BP vs SD tab).
watch(
  branch,
  () => {
    currentPath.value = props.basePath;
    selectedPath.value = "";
    renderedHtml.value = "";
    reload();
  },
  { immediate: true },
);
watch(
  () => props.basePath,
  (newBase) => {
    currentPath.value = newBase;
    selectedPath.value = "";
    renderedHtml.value = "";
    reload();
  },
);
</script>

<style lang="scss" scoped>
.docs-viewer {
  height: calc(100vh - 200px);
}
.docs-tree {
  background: #1c1f33;
  color: #f5e6d3;
}
.dir-list {
  background: #1c1f33;
}
.up-item {
  background: rgba(255, 214, 0, 0.06);
}
.path-label {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.85rem;
}
.path-label :deep(.q-breadcrumbs__el) {
  color: #f5e6d3;
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
  color: #f5e6d3;
  background: #1a1a2e;
  border-left: 4px solid #ff6b35;
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
  color: #ff6b35;
  letter-spacing: 0.04em;
  line-height: 1.4;
  margin: 1.4rem 0 0.6rem;
}
.markdown-body :deep(h1) { font-size: 1.35rem; }
.markdown-body :deep(h2) { font-size: 1.2rem; }
.markdown-body :deep(h3) { font-size: 1.1rem; }
.markdown-body :deep(h4) { font-size: 1.05rem; }
.markdown-body :deep(h5),
.markdown-body :deep(h6) { font-size: 1rem; color: #ffd600; }
.markdown-body :deep(a) {
  color: #ffd600;
}
.markdown-body :deep(code) {
  background: #2c2c3e;
  padding: 1px 4px;
  border-radius: 2px;
  font-size: 0.85em;
}
.markdown-body :deep(pre) {
  background: #11121f;
  padding: 0.75rem 1rem;
  overflow-x: auto;
  border-left: 3px solid #00acc1;
}
.markdown-body :deep(blockquote) {
  border-left: 3px solid #ffd600;
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
  background: #11121f;
  border-left: 3px solid #ff6b35;
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
</style>
