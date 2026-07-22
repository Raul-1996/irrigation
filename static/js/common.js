/*
 * Shared helpers used across pages. Loaded from base.html BEFORE all other
 * scripts (including inline page scripts), so these globals are always
 * available to app.js, per-page JS files and template inline code.
 */

/**
 * Escape HTML special characters to prevent XSS in innerHTML.
 * @param {*} str - Value to escape
 * @returns {string} Escaped HTML string
 */
function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// CSRF token from base.html meta tag
function getCsrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute('content') : '';
}
