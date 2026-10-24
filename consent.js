/* Cookie consent — shared by prahran.press, blairpurvis.com, gwendolen.com.au and matm.com.au.
   Load it in <head> WITHOUT async/defer, BEFORE Google Tag Manager:
       <script src="/consent.js"></script>
       <link rel="stylesheet" href="/consent.css">
   Only the SITE block below differs between sites.

   What it does
   1. Tells Google every optional category is "denied" until the visitor chooses (Essential is always on).
   2. Re-applies a saved choice straight away, before any Google tag runs.
   3. Shows the banner when there is no choice, the choice is over 12 months old, or VERSION has changed.
   4. Any element with data-cookie-settings (the footer link) reopens the banner.
   The choice lives only in this browser (localStorage), as {v, date, statistics, marketing}.
   Bump VERSION when a new tool is added, so everyone is asked again. */
(function () {
  'use strict';

  var SITE = {
    key: 'matm-consent',
    privacyUrl: '/privacy#cookies',
    title: 'Cookies on this site',
    intro: 'We use cookies to see which reviews and episodes people enjoy. Nothing optional runs until you choose.',
    statistics: 'Statistics — Google Analytics and Microsoft Clarity show us which pages are read and how the site is used. Anonymous.',
    marketing: 'Marketing — not used on this site at present. If advertising tools are ever added, they run only if this is on.'
  };
  var VERSION = 1;
  var MAX_AGE_DAYS = 365;

  window.dataLayer = window.dataLayer || [];
  function gtag() { window.dataLayer.push(arguments); }
  window.gtag = window.gtag || gtag;

  gtag('consent', 'default', {
    ad_storage: 'denied',
    ad_user_data: 'denied',
    ad_personalization: 'denied',
    analytics_storage: 'denied',
    functionality_storage: 'granted',
    security_storage: 'granted',
    wait_for_update: 500
  });
  gtag('set', 'ads_data_redaction', true);

  function read() {
    try {
      var c = JSON.parse(localStorage.getItem(SITE.key));
      if (!c || c.v !== VERSION) return null;
      if ((Date.now() - new Date(c.date).getTime()) / 864e5 > MAX_AGE_DAYS) return null;
      return c;
    } catch (e) { return null; }
  }

  function apply(c, isNewChoice) {
    var stat = c.statistics ? 'granted' : 'denied';
    var mkt = c.marketing ? 'granted' : 'denied';
    gtag('consent', 'update', {
      analytics_storage: stat,
      ad_storage: mkt,
      ad_user_data: mkt,
      ad_personalization: mkt
    });
    if (typeof window.clarity === 'function') {
      window.clarity('consentv2', { analytics_Storage: stat, ad_Storage: mkt });
    }
    // Lets Tag Manager fire tags (e.g. Clarity) the moment a visitor accepts, without a reload.
    window.dataLayer.push({ event: 'consent_update', consent_statistics: stat, consent_marketing: mkt, consent_new: !!isNewChoice });
  }

  function save(statistics, marketing) {
    var c = { v: VERSION, date: new Date().toISOString(), statistics: !!statistics, marketing: !!marketing };
    try { localStorage.setItem(SITE.key, JSON.stringify(c)); } catch (e) {}
    apply(c, true);
    close();
  }

  var saved = read();
  if (saved) apply(saved, false);

  // ---------- Banner ----------
  var box = null, lastFocus = null;

  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    for (var k in attrs) if (Object.prototype.hasOwnProperty.call(attrs, k)) n.setAttribute(k, attrs[k]);
    if (text) n.textContent = text;
    return n;
  }

  function build() {
    var current = read() || { statistics: false, marketing: false };
    box = el('section', { 'class': 'cc-banner', role: 'dialog', 'aria-modal': 'false', 'aria-labelledby': 'cc-title' });

    var h = el('h2', { id: 'cc-title', 'class': 'cc-title' }, SITE.title);
    var p = el('p', { 'class': 'cc-intro' }, SITE.intro + ' ');
    var more = el('a', { href: SITE.privacyUrl }, 'How we use cookies');
    p.appendChild(more);

    var choices = el('div', { 'class': 'cc-choices', hidden: '' });
    function row(id, label, checked, disabled) {
      var wrap = el('label', { 'class': 'cc-row', 'for': id });
      var cb = el('input', { type: 'checkbox', id: id });
      cb.checked = checked;
      if (disabled) cb.disabled = true;
      wrap.appendChild(cb);
      wrap.appendChild(el('span', {}, label));
      choices.appendChild(wrap);
      return cb;
    }
    row('cc-essential', 'Essential — remembers this choice and keeps the site working. Always on.', true, true);
    var cbStat = row('cc-statistics', SITE.statistics, current.statistics);
    var cbMkt = row('cc-marketing', SITE.marketing, current.marketing);

    var btns = el('div', { 'class': 'cc-buttons' });
    var bAccept = el('button', { type: 'button', 'class': 'cc-btn' }, 'Accept all');
    var bReject = el('button', { type: 'button', 'class': 'cc-btn' }, 'Reject all');
    var bChoose = el('button', { type: 'button', 'class': 'cc-btn cc-btn-quiet', 'aria-expanded': 'false' }, 'Choose');
    var bSave = el('button', { type: 'button', 'class': 'cc-btn', hidden: '' }, 'Save my choices');

    bAccept.addEventListener('click', function () { save(true, true); });
    bReject.addEventListener('click', function () { save(false, false); });
    bChoose.addEventListener('click', function () {
      choices.hidden = false; bSave.hidden = false; bChoose.hidden = true;
      bChoose.setAttribute('aria-expanded', 'true');
      cbStat.focus();
    });
    bSave.addEventListener('click', function () { save(cbStat.checked, cbMkt.checked); });

    btns.appendChild(bAccept); btns.appendChild(bReject); btns.appendChild(bChoose); btns.appendChild(bSave);
    box.appendChild(h); box.appendChild(p); box.appendChild(choices); box.appendChild(btns);

    box.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && read()) close();
    });
    document.body.appendChild(box);
    return bAccept;
  }

  function open() {
    if (box) return;
    lastFocus = document.activeElement;
    var first = build();
    if (read()) first.focus(); // reopened from the footer: move focus into the banner
  }

  function close() {
    if (!box) return;
    box.parentNode.removeChild(box);
    box = null;
    if (lastFocus && lastFocus.focus) { try { lastFocus.focus(); } catch (e) {} }
  }

  window.siteConsent = { open: open, get: read };

  function ready() {
    document.addEventListener('click', function (e) {
      var t = e.target.closest ? e.target.closest('[data-cookie-settings]') : null;
      if (t) { e.preventDefault(); open(); }
    });
    if (!read()) open();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready);
  else ready();
})();
