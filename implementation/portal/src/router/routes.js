// src/router/routes.js
//
// One layout, three tabs. The Agent X-Ray lives in a right-side
// collapsible drawer instead of a tab — see MainLayout.vue. Default
// route lands on Documentation.
const routes = [
  {
    path: "/",
    component: () => import("layouts/MainLayout.vue"),
    children: [
      { path: "", redirect: "/docs" },
      {
        path: "docs",
        name: "docs",
        component: () => import("pages/DocumentationPage.vue"),
        meta: { title: "Documentation" },
      },
      {
        path: "sme",
        name: "sme",
        component: () => import("pages/SMEAnswersPage.vue"),
        meta: { title: "SME Answers" },
      },
      {
        path: "dashboard",
        name: "dashboard",
        component: () => import("pages/TelemetryPage.vue"),
        meta: { title: "Dashboard" },
      },
    ],
  },
  // Catch-all (including legacy /bp, /sd, /x-ray, /telemetry links): bounce to /docs.
  { path: "/:catchAll(.*)*", redirect: "/docs" },
];

export default routes;
