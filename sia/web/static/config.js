// Superradiant frontend config.
//
// When the dashboard is deployed as a static site (e.g. Vercel) separately from
// the Rust backend (e.g. Railway), set the backend's public origin here so all
// API + SSE calls are routed to it. Leave empty ('') for same-origin (the
// backend serving this page directly, as `sia superradiant` does).
//
// Example for a split deploy:
//   window.SUPERRADIANT_API_BASE = 'https://your-app.up.railway.app';
//
// The in-page "API base" field (topbar) overrides this at runtime and persists
// to localStorage.
window.SUPERRADIANT_API_BASE = window.SUPERRADIANT_API_BASE || '';
