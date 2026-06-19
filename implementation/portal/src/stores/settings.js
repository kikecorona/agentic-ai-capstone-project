// src/stores/settings.js
//
// Single Pinia store for portal-wide UI state. Today that's just the
// active branch the BP/SD doc viewers use; future additions (auth
// token, theme override, multi-repo selector) plug in here without
// touching pages.
import { defineStore } from "pinia";

const KNOWN_BRANCHES = ["main", "starting-point"];

export const useSettingsStore = defineStore("settings", {
  state: () => ({
    // Branch the BP / SD tabs read pear-store from. The §9.8.3 selector
    // in the header writes to this; pages watch it and re-fetch.
    branch: localStorage.getItem("portal.branch") || "main",
    // Free-text "custom branch" input value — preserved when the user
    // toggles the dropdown so they don't lose what they typed.
    customBranchInput: localStorage.getItem("portal.customBranch") || "",
  }),
  getters: {
    knownBranches: () => KNOWN_BRANCHES,
    isCustomBranch: (state) => !KNOWN_BRANCHES.includes(state.branch),
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
  },
});
