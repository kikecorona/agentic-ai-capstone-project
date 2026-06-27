<template>
  <!--
    §9.8 Documentation Portal — main layout.
    Header + 3 tabs + footer + a right-side collapsible drawer for
    the §9.8.4 Multi-Agents X-Ray (toggled from the header). Branch selector
    sits at the toolbar level so it applies across whichever tab is
    active; X-Ray and Telemetry ignore branch.
  -->
  <q-layout view="hHh LpR fFf">
    <q-header bordered class="text-white app-header" height-hint="98">
      <q-toolbar>
        <q-toolbar-title>
          <q-avatar>
            <img src="/logo/logo-mono-white.svg" alt="Logo" />
          </q-avatar>
          <span class="retro-display">Documentation Portal</span>
          <!-- Theme toggle. Sits inline with the title so it's
               immediately accessible — orange ↔ phosphor green. -->
          <q-btn
            flat
            dense
            round
            :icon="settings.theme === 'green' ? 'wb_sunny' : 'desktop_windows'"
            class="q-ml-sm theme-toggle"
            @click="settings.toggleTheme()"
          >
            <q-tooltip class="bg-grey-9">
              {{
                settings.theme === "green"
                  ? "Switch to 80s orange retro theme"
                  : "Switch to phosphor-green CRT theme"
              }}
            </q-tooltip>
          </q-btn>
        </q-toolbar-title>

        <!-- §9.8.3 branch selector. -->
        <branch-selector />

        <!-- §9.8.4 X-Ray drawer toggle. Lives in the header so it's
             reachable from every tab. The icon flips to `visibility_off`
             while the drawer is open so operators can see the state. -->
        <q-btn
          flat
          dense
          round
          :icon="xrayOpen ? 'visibility_off' : 'visibility'"
          class="q-ml-sm"
          @click="xrayOpen = !xrayOpen"
        >
          <q-tooltip class="bg-grey-9">
            {{ xrayOpen ? "Close Multi-Agents X-Ray" : "Open Multi-Agents X-Ray" }}
          </q-tooltip>
        </q-btn>
      </q-toolbar>

      <!-- Tabs row: navigational tabs on the left, the Dashboard tab
           pinned to the right edge so observability lives visually
           apart from the operator workflows. Two q-tabs blocks share
           the same row via a q-space spacer. -->
      <div class="row no-wrap items-center tab-row">
        <q-tabs align="left" class="retro-tabs" indicator-color="accent" active-color="white">
          <q-route-tab to="/docs" label="Documentation" />
          <q-route-tab to="/sme" label="SME Answers" />
        </q-tabs>
        <q-space />
        <q-tabs align="right" class="retro-tabs" indicator-color="accent" active-color="white">
          <q-route-tab to="/dashboard" label="Dashboard" />
        </q-tabs>
      </div>
    </q-header>

    <q-page-container>
      <router-view />
    </q-page-container>

    <!-- Right-side collapsible drawer for the Multi-Agents X-Ray. `overlay`
         keeps it floating on top of the page content (doesn't squish
         the doc viewer). Width is operator-resizable via the handle on
         the left edge — value persists in localStorage so it survives
         reloads. The drawer mounts XRayDrawer ONLY when open so we
         don't keep the SSE EventSource alive in the background. -->
    <q-drawer
      v-model="xrayOpen"
      side="right"
      overlay
      bordered
      :width="xrayWidth"
      :breakpoint="0"
      class="xray-drawer-surface"
    >
      <!-- Drag handle: a thin vertical strip on the left edge.
           mousedown attaches global listeners that update width on
           every mousemove until mouseup tears them down again. -->
      <div
        class="xray-resize-handle"
        @mousedown.prevent="startResize"
        @dblclick="resetWidth"
        title="Drag to resize · double-click to reset"
      />
      <x-ray-drawer v-if="xrayOpen" />
    </q-drawer>

    <q-footer bordered class="bg-grey-10 text-white">
      <q-toolbar>
        <q-toolbar-title>
          <span class="retro-footer">
            Agentic AI Capstone Project - Enrique Corona
          </span>
        </q-toolbar-title>
      </q-toolbar>
    </q-footer>

    <!-- Floating chat bubble — visible across every tab. -->
    <chat-bubble />
  </q-layout>
</template>

<script setup>
import { inject, onBeforeUnmount, ref } from "vue";
import BranchSelector from "components/BranchSelector.vue";
import ChatBubble from "components/ChatBubble.vue";
import XRayDrawer from "components/XRayDrawer.vue";
import { useSettingsStore } from "stores/settings.js";

// Cross-tab UI state — branch selector lives here, theme toggle too.
const settings = useSettingsStore();

// Surface the orchestrator base URL in the footer so an operator can
// quickly tell which backend the portal is wired to.
const ocBase = inject("ocBase", "http://127.0.0.1:8000");

// Local UI state for the collapsible X-Ray drawer. Default closed so
// the operator opts in — when opened, XRayDrawer mounts and starts
// the SSE subscription; when closed, the unmount stops the stream.
const xrayOpen = ref(false);

// ─── Operator-resizable drawer ─────────────────────────────────────
// Width is bound to q-drawer's :width prop. Persisted to localStorage
// so the operator's preferred size survives reloads. Bounds keep the
// drawer usable across screen sizes — too narrow and timestamps wrap;
// too wide and the page content disappears.
const XRAY_DEFAULT_WIDTH = Math.round(window.innerWidth * 0.8);
const XRAY_MIN_WIDTH = Math.round(window.innerWidth * 0.8);
const XRAY_MAX_WIDTH = 1400;

function loadStoredWidth() {
  const raw = Number.parseInt(localStorage.getItem("portal.xrayWidth") || "", 10);
  if (Number.isFinite(raw) && raw >= XRAY_MIN_WIDTH && raw <= XRAY_MAX_WIDTH) {
    return raw;
  }
  return XRAY_DEFAULT_WIDTH;
}
const xrayWidth = ref(loadStoredWidth());

let _moveHandler = null;
let _upHandler = null;

function startResize(downEvent) {
  // Capture document-level move/up so the drag continues even if the
  // pointer leaves the handle's narrow strip. Selection disabled
  // during the drag so the cursor doesn't grab page content.
  document.body.style.userSelect = "none";
  document.body.style.cursor = "col-resize";
  _moveHandler = (e) => {
    // Drawer is on the right, so width = viewport-right - pointer.x.
    const next = Math.max(
      XRAY_MIN_WIDTH,
      Math.min(XRAY_MAX_WIDTH, window.innerWidth - e.clientX),
    );
    xrayWidth.value = next;
  };
  _upHandler = () => {
    document.removeEventListener("mousemove", _moveHandler);
    document.removeEventListener("mouseup", _upHandler);
    document.body.style.userSelect = "";
    document.body.style.cursor = "";
    _moveHandler = null;
    _upHandler = null;
    try {
      localStorage.setItem("portal.xrayWidth", String(xrayWidth.value));
    } catch (_) {
      /* private mode — ignore */
    }
  };
  document.addEventListener("mousemove", _moveHandler);
  document.addEventListener("mouseup", _upHandler);
}

function resetWidth() {
  xrayWidth.value = XRAY_DEFAULT_WIDTH;
  try {
    localStorage.setItem("portal.xrayWidth", String(XRAY_DEFAULT_WIDTH));
  } catch (_) {
    /* ignore */
  }
}

// Defensive cleanup: if the layout unmounts mid-drag (route change,
// HMR), drop the document-level listeners we attached in startResize.
onBeforeUnmount(() => {
  if (_moveHandler) document.removeEventListener("mousemove", _moveHandler);
  if (_upHandler) document.removeEventListener("mouseup", _upHandler);
  document.body.style.userSelect = "";
  document.body.style.cursor = "";
});
</script>

<style lang="scss" scoped>
.retro-display {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 1.35rem;
  letter-spacing: 0.04em;
  margin-left: 0.75rem;
}
.retro-footer {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.8rem;
  letter-spacing: 0.04em;
  margin-left: 0.75rem;
}
.retro-tabs :deep(.q-tab__label) {
  font-family: "VT323", "JetBrains Mono", monospace;
  font-size: 1.05rem;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}
.tab-row {
  width: 100%;
}
// Header surface — pulled out of `bg-primary` so we can colour it
// independently per theme (orange theme keeps the iconic orange bar;
// green theme dims to a near-black bezel so it doesn't glow).
.app-header {
  background: var(--theme-header-bg);
}
.xray-drawer-surface {
  background: var(--theme-bg-deep);
  border-color: var(--theme-accent-primary);
}
// Drag handle for resizing the X-Ray drawer. Sits as a 6px-wide
// vertical strip pinned to the drawer's left edge. Visible only on
// hover so it doesn't compete visually with the drawer chrome.
.xray-resize-handle {
  position: absolute;
  top: 0;
  left: 0;
  width: 6px;
  height: 100%;
  cursor: col-resize;
  z-index: 5;
  background: transparent;
  transition: background 0.12s ease-in-out;
}
.xray-resize-handle:hover,
.xray-resize-handle:active {
  background: var(--theme-accent-primary-stroke);
}
</style>
