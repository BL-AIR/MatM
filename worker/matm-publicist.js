// ─────────────────────────────────────────────────────────────────────────────
// Cloudflare Worker: matm-publicist
// Handles publicist transcript excerpt requests for matm.com.au, and keeps
// the list of episodes under a publicist's embargo.
//
//   POST /           publicist transcript request (publicist.html)
//   GET  /embargo    active embargoes, read by index.html and films.html
//   POST /embargo    set or lift an embargo (the secret sprocket; needs the key)
//
// Environment variables required (set in Cloudflare dashboard → Worker → Settings → Variables):
//   RESEND_API_KEY   — from resend.com
//   EMBARGO_KEY      — the passphrase Madeleine types the first time she uses the sprocket
//
// Bindings required (Worker → Settings → Bindings):
//   EMBARGO          — KV namespace holding one key, "embargoes"
//
// CORS: accepts requests from https://matm.com.au and localhost (testing)
// ─────────────────────────────────────────────────────────────────────────────

const R2_BASE    = 'https://pub-fca72aca0d2a44489ca717888abac149.r2.dev';
const MATM_URL   = 'https://matm.com.au';
const CC_EMAIL   = 'madeleine@matm.com.au';
const FROM_EMAIL = 'Madeleine at the Movies <madeleine@matm.com.au>';

const ALLOWED_ORIGINS = ['https://matm.com.au', 'http://localhost', 'http://127.0.0.1'];

// ── Entry point ───────────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    const origin = request.headers.get('Origin') || '';
    // Origin matches if exact, or an allowed host plus a port (e.g. http://localhost:8080)
    const originAllowed = ALLOWED_ORIGINS.some(o => origin === o || origin.startsWith(o + ':'));
    const corsOrigin    = originAllowed ? origin : ALLOWED_ORIGINS[0];
    const path          = new URL(request.url).pathname.replace(/\/+$/, '');

    const corsHeaders = {
      'Access-Control-Allow-Origin':  corsOrigin,
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Vary':                         'Origin',
    };

    // CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    // ── Embargo list: anyone may read it ─────────────────────────────────────
    if (path === '/embargo' && request.method === 'GET') {
      return json(await activeEmbargoes(env), 200,
        { ...corsHeaders, 'Cache-Control': 'public, max-age=30' });
    }

    if (request.method !== 'POST') {
      return json({ error: 'Method not allowed' }, 405, corsHeaders);
    }

    // Reject requests that don't come from the website. Browsers always send
    // an Origin header on cross-origin POSTs, so a missing or foreign Origin
    // means a script or curl is calling directly — refuse to send email.
    if (!originAllowed) {
      return json({ error: 'Forbidden' }, 403, corsHeaders);
    }

    // Rate limit: max 3 requests per minute per visitor IP (binding in wrangler.toml).
    // If the binding isn't configured yet, fail open rather than break the form.
    // This also slows anyone trying to guess the embargo key.
    if (env.RATE_LIMITER) {
      const ip = request.headers.get('CF-Connecting-IP') || 'unknown';
      const { success } = await env.RATE_LIMITER.limit({ key: ip });
      if (!success) {
        return json({ error: 'Too many requests. Please wait a minute and try again.' }, 429, corsHeaders);
      }
    }

    if (path === '/embargo') {
      return setEmbargo(request, env, corsHeaders);
    }

    // ── Parse body ──────────────────────────────────────────────────────────
    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: 'Invalid request body.' }, 400, corsHeaders);
    }

    const { email, episode, chapter } = body;

    if (!email || !episode || !chapter) {
      return json({ error: 'Missing required fields.' }, 400, corsHeaders);
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      return json({ error: 'Invalid email address.' }, 400, corsHeaders);
    }

    const epNum = String(parseInt(episode)).padStart(4, '0');

    // ── Refuse while a publicist's embargo is in force ──────────────────────
    const embargo = (await activeEmbargoes(env))[String(parseInt(episode))];
    if (embargo) {
      return json({ error: `This review is under embargo until ${embargo.label}.` }, 403, corsHeaders);
    }

    // ── Fetch and validate chapters VTT ─────────────────────────────────────
    let chapters;
    try {
      const res = await fetch(`${R2_BASE}/MatM_${epNum}.chapters.vtt`);
      if (!res.ok) throw new Error('not found');
      chapters = parseVTT(await res.text());
      if (chapters.length === 0) throw new Error('empty');
    } catch {
      return json({ error: 'Episode not found. Please check the episode number.' }, 404, corsHeaders);
    }

    // Find the requested chapter (case-insensitive)
    const matched = chapters.find(c =>
      c.text.toLowerCase().trim() === chapter.toLowerCase().trim()
    );
    if (!matched) {
      return json({ error: 'Film not found in this episode.' }, 404, corsHeaders);
    }

    // ── Extract full review from subtitle cues ──────────────────────────────
    let excerpt = '';

    try {
      const vttRes = await fetch(`${R2_BASE}/MatM_${epNum}.vtt`);
      if (!vttRes.ok) throw new Error('no subtitle vtt');

      const cues       = parseVTT(await vttRes.text());
      const matchIdx   = chapters.indexOf(matched);
      const chapterEnd = chapters[matchIdx + 1]?.start ?? Infinity;

      // Collect all subtitle cues within this chapter's time range
      const chapterCues = cues.filter(c =>
        c.start >= matched.start && c.start < chapterEnd
      );

      const fullText = chapterCues.map(c => c.text).join(' ');
      excerpt = fullText.replace(/\s+/g, ' ').trim()
                        .replace(/\bMadeline\b/g, 'Madeleine');
    } catch {}

    if (!excerpt) {
      return json({ error: 'Transcript not yet available for this episode.' }, 404, corsHeaders);
    }

    // ── Look up episode date ────────────────────────────────────────────────
    let reviewDate = '';
    try {
      const epRes = await fetch(`${MATM_URL}/episodes.json`);
      if (epRes.ok) {
        const episodes = await epRes.json();
        const epEntry  = episodes.find(e => e.ep === parseInt(episode));
        if (epEntry?.date) {
          reviewDate = formatDate(epEntry.date);
        }
      }
    } catch {}

    // ── Build deep-link ─────────────────────────────────────────────────────
    const deepLink = `${MATM_URL}/?ep=${parseInt(episode)}&chapter=${encodeURIComponent(chapter)}`;

    // ── Send email via Resend ───────────────────────────────────────────────
    const { html, text } = buildEmail({
      chapter,
      episode: parseInt(episode),
      excerpt,
      deepLink,
      reviewDate,
    });

    try {
      const resendRes = await fetch('https://api.resend.com/emails', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${env.RESEND_API_KEY}`,
          'Content-Type':  'application/json',
        },
        body: JSON.stringify({
          from:    FROM_EMAIL,
          to:      [email],
          cc:      [CC_EMAIL],
          subject: `MatM Transcript Request: ${chapter}`,
          html,
          text,
        }),
      });

      if (!resendRes.ok) {
        const err = await resendRes.json().catch(() => ({}));
        throw new Error(err.message || `Resend HTTP ${resendRes.status}`);
      }
    } catch (e) {
      console.error('Resend error:', e.message);
      return json({ error: 'Failed to send email. Please try again shortly.' }, 500, corsHeaders);
    }

    return json({ success: true }, 200, corsHeaders);
  },
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function json(body, status, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

function parseVTT(text) {
  const result = [];
  text.split(/\n\n+/).forEach(block => {
    const lines = block.trim().split('\n');
    const tl    = lines.find(l => l.includes('-->'));
    if (!tl) return;
    const [a, b] = tl.split('-->').map(t => {
      const p = t.trim().replace(/,/g, '.').split(':');
      return +p[0] * 3600 + +p[1] * 60 + parseFloat(p[2]);
    });
    const txt = lines
      .filter(l => !l.includes('-->') && !/^\d+$/.test(l.trim()) && !/^WEBVTT/.test(l.trim()))
      .join(' ')
      .trim();
    if (txt) result.push({ start: a, end: b, text: txt });
  });
  return result;
}

// ── Embargo ────────────────────────────────────────────────────────────────
// Stored as one KV key, "embargoes": { "744": { until, label, days, set } }.
// `until` is epoch ms for midnight in Melbourne at the start of the release
// day, so an embargo of 7 days set on a Thursday lifts as the next Thursday
// begins.

const TZ = 'Australia/Melbourne';
const MAX_EMBARGO_DAYS = 7;    // cap, so a leaked key cannot blank the site for long

async function readEmbargoes(env) {
  if (!env.EMBARGO) return {};
  try { return (await env.EMBARGO.get('embargoes', 'json')) || {}; }
  catch { return {}; }
}

async function activeEmbargoes(env) {
  const all = await readEmbargoes(env);
  const now = Date.now();
  const out = {};
  for (const [ep, e] of Object.entries(all)) {
    if (e && e.until > now) out[ep] = e;
  }
  return out;
}

async function setEmbargo(request, env, corsHeaders) {
  if (!env.EMBARGO || !env.EMBARGO_KEY) {
    return json({ error: 'Embargo storage is not set up on the worker yet.' }, 500, corsHeaders);
  }

  let body;
  try { body = await request.json(); }
  catch { return json({ error: 'Invalid request body.' }, 400, corsHeaders); }

  if (!safeEqual(String(body.key || ''), env.EMBARGO_KEY)) {
    return json({ error: 'That passphrase is not right.' }, 401, corsHeaders);
  }

  // A key-only request just checks the passphrase.
  if (body.ep === undefined) {
    return json({ ok: true, embargoes: await activeEmbargoes(env) }, 200, corsHeaders);
  }

  const ep   = parseInt(body.ep);
  const days = parseInt(body.days);
  if (!(ep > 0 && ep < 10000)) {
    return json({ error: 'Invalid episode number.' }, 400, corsHeaders);
  }
  if (!(days >= 0 && days <= MAX_EMBARGO_DAYS)) {
    return json({ error: `Days must be between 0 and ${MAX_EMBARGO_DAYS}.` }, 400, corsHeaders);
  }

  // Start from the live list, which also drops anything that has expired.
  const list = await activeEmbargoes(env);
  if (days === 0) {
    delete list[ep];
  } else {
    const { y, m, d } = melbourneToday();
    const release     = new Date(Date.UTC(y, m - 1, d + days));   // calendar arithmetic
    const until       = melbourneMidnight(release.getUTCFullYear(), release.getUTCMonth() + 1, release.getUTCDate());
    list[ep] = {
      until,
      label: new Intl.DateTimeFormat('en-AU', {
        timeZone: TZ, weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
      }).format(new Date(until)),
      days,
      set: Date.now(),
    };
  }
  await env.EMBARGO.put('embargoes', JSON.stringify(list));
  return json({ ok: true, embargoes: list }, 200, corsHeaders);
}

function melbourneToday() {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
    timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date()).map(p => [p.type, p.value]));
  return { y: +parts.year, m: +parts.month, d: +parts.day };
}

// Epoch ms of 00:00 in Melbourne on the given calendar date (DST-aware).
function melbourneMidnight(y, m, d) {
  const guess = Date.UTC(y, m - 1, d);
  let t = guess - tzOffset(guess);
  t = guess - tzOffset(t);           // second pass settles DST changeover days
  return t;
}

function tzOffset(t) {
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
    timeZone: TZ, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).formatToParts(new Date(t)).map(x => [x.type, x.value]));
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second) - t;
}

function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// Convert "7 September 2025" → "07/09/2025"
function formatDate(dateStr) {
  const months = {
    January: '01', February: '02', March: '03', April: '04',
    May: '05', June: '06', July: '07', August: '08',
    September: '09', October: '10', November: '11', December: '12',
  };
  const parts = dateStr.trim().split(' ');
  if (parts.length !== 3) return dateStr;
  const day   = String(parseInt(parts[0])).padStart(2, '0');
  const month = months[parts[1]] || '??';
  const year  = parts[2];
  return `${day}/${month}/${year}`;
}

function buildEmail({ chapter, episode, excerpt, deepLink, reviewDate }) {
  const dateLine = reviewDate
    ? `<p style="font-size:13px;color:#777;margin:0 0 20px;">Review date: ${reviewDate}</p>`
    : '';
  const dateLineTxt = reviewDate ? `Review date: ${reviewDate}\n` : '';

  const html = `<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Georgia,serif;max-width:580px;margin:0 auto;padding:28px 24px;color:#222;background:#fff;">

  <table width="100%" cellpadding="0" cellspacing="0" style="border-bottom:2px solid #c9a84c;padding-bottom:14px;margin-bottom:20px;">
    <tr>
      <td>
        <div style="font-family:Georgia,serif;font-size:22px;color:#c9a84c;letter-spacing:0.04em;">Madeleine at the Movies</div>
        <div style="font-size:11px;color:#999;letter-spacing:0.12em;text-transform:uppercase;margin-top:3px;">Publicist Transcript Request</div>
      </td>
    </tr>
  </table>

  <p style="font-size:14px;color:#555;margin:0 0 6px;">Episode ${episode} &nbsp;&middot;&nbsp; <strong style="color:#333;">${chapter}</strong></p>
  ${dateLine}

  <h3 style="font-size:14px;letter-spacing:0.08em;text-transform:uppercase;color:#999;margin:0 0 10px;">Review Excerpt</h3>
  <blockquote style="margin:0 0 20px;padding:16px 20px;background:#fafaf5;border-left:3px solid #c9a84c;font-style:italic;line-height:1.8;font-size:15px;color:#333;">
    ${excerpt}
  </blockquote>

  <p style="font-size:14px;margin:0 0 6px;">
    <a href="${deepLink}" style="color:#c9a84c;text-decoration:none;">&#9654; Listen to this review at matm.com.au</a>
  </p>
  <p style="font-size:12px;color:#888;margin:0 0 24px;">
    <span style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:0.06em;">Online attribution link to the review</span><br>
    <span style="word-break:break-all;">${deepLink}</span>
  </p>

  <hr style="border:none;border-top:1px solid #e8e0cc;margin:0 0 20px;">

  <h3 style="font-size:14px;letter-spacing:0.08em;text-transform:uppercase;color:#999;margin:0 0 10px;">Terms of Use</h3>
  <p style="font-size:13px;line-height:1.7;color:#444;">The transcript excerpt provided through this service is made available exclusively for promotional and editorial use in connection with the film reviewed. By requesting and using this transcript, you agree to the following conditions.</p>
  <p style="font-size:13px;line-height:1.7;color:#444;"><strong>Attribution.</strong> Any use of the excerpt must be accompanied by a clear credit reading: <em>Madeleine at the Movies, Golden Days Radio 95.7FM Melbourne</em> along with a link to the review &mdash; either to the homepage at matm.com.au, or directly to the film review via the coded link provided in this email.</p>
  <p style="font-size:13px;line-height:1.7;color:#444;"><strong>Accuracy.</strong> Transcripts are generated using an AI transcription tool and may contain errors, mishearings, or incorrect proper nouns. It is your responsibility to verify the text for accuracy before publication.</p>
  <p style="font-size:13px;line-height:1.7;color:#444;"><strong>Permitted use.</strong> The excerpt may be used in press releases, media kits, promotional copy, and editorial coverage of the reviewed film. It may not be used out of context, edited to alter the meaning of the review, or presented in a way that misrepresents Madeleine Swain&rsquo;s opinion.</p>
  <p style="font-size:13px;line-height:1.7;color:#444;"><strong>No endorsement.</strong> Receipt of a transcript does not constitute an endorsement of any product, campaign, or organisation beyond the review itself.</p>
  <p style="font-size:13px;line-height:1.7;color:#444;"><strong>Enquiries.</strong> For permissions beyond the scope of these terms, or to request a correction, contact <a href="mailto:Madeleine@matm.com.au" style="color:#c9a84c;">Madeleine@matm.com.au</a>.</p>

  <hr style="border:none;border-top:1px solid #e8e0cc;margin:24px 0 16px;">

  <p style="font-size:12px;color:#c9a84c;font-style:italic;line-height:1.6;margin:0 0 20px;">
    This transcript was produced using an AI transcription tool.<br>
    Please check carefully for accuracy, spelling, and proper nouns before publication.
  </p>

  <hr style="border:none;border-top:1px solid #e8e0cc;margin:0 0 16px;">

  <p style="font-size:11px;color:#bbb;line-height:1.7;margin:0;">
    Madeleine at the Movies &nbsp;&middot;&nbsp; Golden Days Radio 95.7FM Melbourne<br>
    1st floor 1236 Glen Huntly Road Glen Huntly VIC 3163<br>
    <a href="https://matm.com.au" style="color:#c9a84c;">matm.com.au</a>
  </p>

</body></html>`;

  const text = `MADELEINE AT THE MOVIES — Publicist Transcript Request
Episode ${episode}: ${chapter}
${dateLineTxt}
REVIEW EXCERPT
"${excerpt}"

Listen to this review: ${deepLink}

Online attribution link to the review:
${deepLink}

TERMS OF USE
The transcript excerpt provided through this service is made available exclusively for promotional and editorial use in connection with the film reviewed. By requesting and using this transcript, you agree to the following conditions.

Attribution. Any use of the excerpt must be accompanied by a clear credit reading: Madeleine at the Movies, Golden Days Radio 95.7FM Melbourne along with a link to the review — either to the homepage at matm.com.au, or directly to the film review via the coded link provided in this email.

Accuracy. Transcripts are generated using an AI transcription tool and may contain errors, mishearings, or incorrect proper nouns. It is your responsibility to verify the text for accuracy before publication.

Permitted use. The excerpt may be used in press releases, media kits, promotional copy, and editorial coverage of the reviewed film. It may not be used out of context, edited to alter the meaning of the review, or presented in a way that misrepresents Madeleine Swain's opinion.

No endorsement. Receipt of a transcript does not constitute an endorsement of any product, campaign, or organisation beyond the review itself.

Enquiries. For permissions beyond the scope of these terms, or to request a correction, contact Madeleine@matm.com.au.

DISCLAIMER
This transcript was produced using an AI transcription tool.
Please check carefully for accuracy, spelling, and proper nouns before publication.

---
Madeleine at the Movies · Golden Days Radio 95.7FM Melbourne
1st floor 1236 Glen Huntly Road Glen Huntly VIC 3163
matm.com.au`;

  return { html, text };
}
