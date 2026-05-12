// ─────────────────────────────────────────────────────────────────────────────
// Cloudflare Worker: matm-publicist
// Handles publicist transcript excerpt requests for matm.com.au
//
// Environment variables required (set in Cloudflare dashboard → Worker → Settings → Variables):
//   RESEND_API_KEY   — from resend.com
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
    const origin     = request.headers.get('Origin') || '';
    const corsOrigin = ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0];

    const corsHeaders = {
      'Access-Control-Allow-Origin':  corsOrigin,
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    };

    // CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    if (request.method !== 'POST') {
      return json({ error: 'Method not allowed' }, 405, corsHeaders);
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
      excerpt = fullText.replace(/\s+/g, ' ').trim();
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
  <p style="font-size:13px;line-height:1.7;color:#444;">[Terms and conditions &mdash; to be inserted]</p>

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
[Terms and conditions — to be inserted]

DISCLAIMER
This transcript was produced using an AI transcription tool.
Please check carefully for accuracy, spelling, and proper nouns before publication.

---
Madeleine at the Movies · Golden Days Radio 95.7FM Melbourne
1st floor 1236 Glen Huntly Road Glen Huntly VIC 3163
matm.com.au`;

  return { html, text };
}
