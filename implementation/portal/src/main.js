// src/main.js
//
// Plain Vite entry point — replaces the @quasar/app-vite scaffolding
// driven by quasar.config.js. We register Vue + Pinia + Vue Router +
// Quasar manually so the SASS source-compile pipeline never triggers.
import { createApp } from "vue";
import { createPinia } from "pinia";
import { createRouter, createWebHistory } from "vue-router";
import { Quasar, Notify } from "quasar";

// Quasar CSS + icon set from the precompiled bundle. No SASS, no
// variable injection — theme overrides happen at runtime in
// src/boot/apis.js via setCssVar().
import "@quasar/extras/material-icons/material-icons.css";
import "quasar/dist/quasar.css";

// Project styles.
import "./css/app.scss";
import "./css/retro.scss";

import App from "./App.vue";
import routes from "./router/routes.js";
import apisBoot from "./boot/apis.js";

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ left: 0, top: 0 }),
});

const app = createApp(App);

app.use(Quasar, {
  plugins: { Notify },
});

// apisBoot installs Pinia + axios + setCssVar() palette overrides.
apisBoot({ app });

app.use(router);
app.mount("#app");
