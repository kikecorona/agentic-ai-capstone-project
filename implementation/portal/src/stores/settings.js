// src/stores/settings.js
//
// Single Pinia store for portal-wide UI state. Today that's:
//   * the active branch the BP/SD doc viewers use
//   * the active theme (orange retro vs. phosphor green CRT)
import { defineStore } from "pinia";
import { setCssVar } from "quasar";

const KNOWN_BRANCHES = ["main", "starting-point"];
const KNOWN_THEMES = ["orange", "green"];

// Quasar palette per theme — kept in sync with --quasar-* CSS vars in
// src/css/themes.scss. Pushed via `setCssVar()` so utility classes
// (`bg-primary`, `text-accent`, …) flip alongside the scoped styles.
const THEME_PALETTES = {
  orange: {
    primary: "#ff6b35",
    secondary: "#ffd600",
    accent: "#00acc1",
    dark: "#1a1a2e",
    "dark-page": "#1a1a2e",
    positive: "#2e7d32",
    negative: "#ff5252",
    info: "#00acc1",
    warning: "#ffd600",
  },
  green: {
    primary: "#2fbf5f",
    secondary: "#88bb22",
    accent: "#5cbf5c",
    dark: "#050a05",
    "dark-page": "#050a05",
    positive: "#2fbf5f",
    negative: "#ff5050",
    info: "#5cbf5c",
    warning: "#aacc00",
  },
};

function _applyTheme(theme) {
  // Cascade: setting `data-theme` on <html> picks the matching block in
  // themes.scss, and `setCssVar()` fans the same palette into Quasar's
  // utility classes.
  if (typeof document !== "undefined" && document.documentElement) {
    document.documentElement.setAttribute("data-theme", theme);
  }
  const palette = THEME_PALETTES[theme] || THEME_PALETTES.orange;
  for (const [k, v] of Object.entries(palette)) {
    try {
      setCssVar(k, v);
    } catch (_) {
      /* ignore — non-fatal */
    }
  }
}

export const useSettingsStore = defineStore("settings", {
  state: () => ({
    branch: localStorage.getItem("portal.branch") || "main",
    customBranchInput: localStorage.getItem("portal.customBranch") || "",
    // Orange retro is the default — green CRT is opt-in via the toggle.
    theme: localStorage.getItem("portal.theme") || "orange",
  }),
  getters: {
    knownBranches: () => KNOWN_BRANCHES,
    isCustomBranch: (state) => !KNOWN_BRANCHES.includes(state.branch),
    knownThemes: () => KNOWN_THEMES,
  },
  actions: {
    setBranch(value) {
      const v = (value || "main").trim() || "main";
      this.branch = v;
      try {
        localStorage.setItem("portal.branch", v);
      } catch (_) {
        /* private mode etc — ignore */
      }
    },
    setCustomBranchInput(value) {
      this.customBranchInput = value || "";
      try {
        localStorage.setItem("portal.customBranch", this.customBranchInput);
      } catch (_) {
        /* ignore */
      }
    },
    setTheme(value) {
      const v = KNOWN_THEMES.includes(value) ? value : "orange";
      this.theme = v;
      _applyTheme(v);
      try {
        localStorage.setItem("portal.theme", v);
      } catch (_) {
        /* ignore */
      }
    },
    toggleTheme() {
      this.setTheme(this.theme === "orange" ? "green" : "orange");
    },
    // Boot helper: apply whatever's in state right now. The portal's
    // entry-point calls this once after the store is constructed so
    // the persisted theme survives a reload.
    applyCurrentTheme() {
      _applyTheme(this.theme);
    },
  },
});
