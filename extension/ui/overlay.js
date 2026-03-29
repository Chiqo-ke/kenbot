// ui/overlay.js — KenBot Shadow DOM overlay helpers
// Plain JS. Loaded as a web-accessible resource inside the Shadow DOM.
//
// This module is NOT imported by content.js because Shadow DOM scripts don't
// share scope with the page or the content script.
// Instead, content.js builds the overlay structure directly (see content.js §1).
//
// This file exists as a companion reference / extension point:
//   - Defines the overlay's DOM skeleton template (used if content.js needs to
//     rebuild the panel e.g. after an extension update or SPA navigation).
//   - Provides helper factory functions that construct specific UI sub-components
//     (step-progress bars, field-summary cards) without touching the real DOM.
//
// All functions here are pure — they return DocumentFragment or HTMLElement and
// do NOT access `document` directly so they can be unit-tested in Node.js if needed.

'use strict';

/**
 * Build the full panel skeleton as a DocumentFragment.
 * content.js clones this into the Shadow DOM root.
 *
 * @returns {DocumentFragment}
 */
function buildPanelSkeleton() {
  const frag = document.createDocumentFragment();

  // ── Toggle button ──────────────────────────────────────────────────────────
  const toggleBtn = document.createElement('button');
  toggleBtn.id = 'kb-toggle';
  toggleBtn.className = 'kb-toggle';
  toggleBtn.setAttribute('aria-label', 'Open KenBot panel');
  toggleBtn.setAttribute('aria-expanded', 'false');
  toggleBtn.innerHTML = '<span class="kb-logo" aria-hidden="true">KB</span>';
  frag.appendChild(toggleBtn);

  // ── Chat panel ─────────────────────────────────────────────────────────────
  const panel = document.createElement('div');
  panel.id = 'kb-chat';
  panel.className = 'kb-chat';
  panel.setAttribute('hidden', '');
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'KenBot assistant');

  panel.appendChild(buildHeader());
  panel.appendChild(buildProgressBar());
  panel.appendChild(buildMessageLog());
  panel.appendChild(buildConfirmationArea());
  panel.appendChild(buildInputForm());

  frag.appendChild(panel);
  return frag;
}

// ─── Sub-component builders ────────────────────────────────────────────────────

function buildHeader() {
  const header = document.createElement('header');
  header.className = 'kb-header';

  const title = document.createElement('span');
  title.className = 'kb-title';
  title.textContent = 'KenBot';

  const dot = document.createElement('span');
  dot.id = 'kb-status-dot';
  dot.className = 'kb-status-dot kb-status-disconnected';
  dot.setAttribute('aria-label', 'Disconnected');
  dot.setAttribute('title', 'Disconnected');

  const closeBtn = document.createElement('button');
  closeBtn.id = 'kb-close';
  closeBtn.className = 'kb-close';
  closeBtn.setAttribute('aria-label', 'Close panel');
  closeBtn.textContent = '×';

  header.appendChild(title);
  header.appendChild(dot);
  header.appendChild(closeBtn);
  return header;
}

function buildProgressBar() {
  const wrapper = document.createElement('div');
  wrapper.id = 'kb-progress-wrapper';
  wrapper.className = 'kb-progress-wrapper';
  wrapper.setAttribute('hidden', '');
  wrapper.setAttribute('role', 'status');
  wrapper.setAttribute('aria-live', 'polite');

  const bar = document.createElement('div');
  bar.id = 'kb-progress-bar';
  bar.className = 'kb-progress-bar';
  bar.setAttribute('role', 'progressbar');
  bar.setAttribute('aria-valuemin', '0');
  bar.setAttribute('aria-valuemax', '100');
  bar.setAttribute('aria-valuenow', '0');

  const fill = document.createElement('div');
  fill.id = 'kb-progress-fill';
  fill.className = 'kb-progress-fill';

  bar.appendChild(fill);

  const label = document.createElement('span');
  label.id = 'kb-progress-label';
  label.className = 'kb-progress-label';

  wrapper.appendChild(bar);
  wrapper.appendChild(label);
  return wrapper;
}

function buildMessageLog() {
  const log = document.createElement('div');
  log.id = 'kb-messages';
  log.className = 'kb-messages';
  log.setAttribute('role', 'log');
  log.setAttribute('aria-live', 'polite');
  log.setAttribute('aria-relevant', 'additions');
  return log;
}

function buildConfirmationArea() {
  const area = document.createElement('div');
  area.id = 'kb-confirmation-area';
  area.className = 'kb-confirmation-area';
  area.setAttribute('hidden', '');
  return area;
}

function buildInputForm() {
  const form = document.createElement('form');
  form.id = 'kb-input-form';
  form.className = 'kb-input-form';
  form.setAttribute('autocomplete', 'off');

  const input = document.createElement('input');
  input.id = 'kb-user-input';
  input.className = 'kb-user-input';
  input.type = 'text';
  input.placeholder = 'Type a task in English or Swahili…';
  input.setAttribute('aria-label', 'Task input');
  input.setAttribute('autocomplete', 'off');

  const sendBtn = document.createElement('button');
  sendBtn.type = 'submit';
  sendBtn.className = 'kb-send-btn';
  sendBtn.setAttribute('aria-label', 'Send');
  sendBtn.innerHTML = '&#9658;';

  form.appendChild(input);
  form.appendChild(sendBtn);
  return form;
}

// ─── Step Progress Card ────────────────────────────────────────────────────────

/**
 * Build a step-progress card element.
 * Shows workflow steps with visual pass/fail/pending indicators.
 *
 * @param {{ steps: Array<{ label: string, status: 'pending'|'running'|'done'|'failed' }> }} options
 * @returns {HTMLElement}
 */
function buildStepProgressCard(options) {
  const card = document.createElement('div');
  card.className = 'kb-step-card';

  const list = document.createElement('ol');
  list.className = 'kb-step-list';

  (options.steps || []).forEach((step) => {
    const item = document.createElement('li');
    item.className = `kb-step-item kb-step-item--${step.status}`;
    item.setAttribute('aria-label', `${step.label}: ${step.status}`);

    const icon = document.createElement('span');
    icon.className = 'kb-step-icon';
    icon.setAttribute('aria-hidden', 'true');
    const icons = { pending: '○', running: '◎', done: '✓', failed: '✗' };
    icon.textContent = icons[step.status] || '○';

    const label = document.createElement('span');
    label.className = 'kb-step-label';
    label.textContent = step.label;

    item.appendChild(icon);
    item.appendChild(label);
    list.appendChild(item);
  });

  card.appendChild(list);
  return card;
}

/**
 * Build a field-summary card for the confirmation prompt.
 * Lists vault key names (NOT values) so the user knows what will be submitted.
 *
 * @param {string[]} fieldKeys  e.g. ['national_id', 'kra_pin']
 * @returns {HTMLElement}
 */
function buildFieldSummaryCard(fieldKeys) {
  const card = document.createElement('div');
  card.className = 'kb-field-summary-card';

  const heading = document.createElement('p');
  heading.className = 'kb-field-summary-heading';
  heading.textContent = 'The following vault fields will be used:';
  card.appendChild(heading);

  const list = document.createElement('ul');
  list.className = 'kb-field-list';

  fieldKeys.forEach((key) => {
    const li = document.createElement('li');
    li.className = 'kb-field-item';
    // Display a human-readable label from the key name
    li.textContent = key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
    list.appendChild(li);
  });

  card.appendChild(list);
  return card;
}

/**
 * Update the progress bar element in the shadow root.
 *
 * @param {ShadowRoot} shadow
 * @param {number} percent  0–100
 * @param {string} label
 */
function updateProgressBar(shadow, percent, label) {
  const wrapper = shadow.getElementById('kb-progress-wrapper');
  const fill    = shadow.getElementById('kb-progress-fill');
  const bar     = shadow.getElementById('kb-progress-bar');
  const labelEl = shadow.getElementById('kb-progress-label');

  if (!wrapper) return;

  wrapper.hidden = false;
  bar.setAttribute('aria-valuenow', String(percent));
  fill.style.width = `${Math.min(100, Math.max(0, percent))}%`;
  if (labelEl) labelEl.textContent = label || '';

  if (percent >= 100) {
    setTimeout(() => { wrapper.hidden = true; }, 1500);
  }
}

// Expose helpers on the global scope so content.js (same Shadow DOM context)
// can call them if this file is ever loaded as a module script inside the shadow.
// In MV3 content scripts, each file is isolated, so we attach to `self`.
self.KenbotOverlay = {
  buildPanelSkeleton,
  buildStepProgressCard,
  buildFieldSummaryCard,
  updateProgressBar
};
