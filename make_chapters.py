#!/usr/bin/env python3
"""
make_chapters.py — Generate chapter VTT files for MatM episodes.

Reads film titles from the EPISODES array in index.html, then finds each
review's sign-off in the captions VTT and writes a chapters VTT that the
player uses for its skip-to-next-review button.

USAGE
-----
  # Batch — process every VTT that has 2+ films in the database:
  python3 make_chapters.py

  # Single episode — titles from database:
  python3 make_chapters.py MatM_0721

  # Single episode — explicit title overrides:
  python3 make_chapters.py MatM_0387 "Amazing Grace" "Dragged Across Concrete"

  # Re-generate (overwrite existing chapters files):
  python3 make_chapters.py --force

Run from the MatM/MatM/ folder (same location as convert.sh).

SKIPPED AUTOMATICALLY
---------------------
  - Episodes with 0 or 1 film titles (festivals, year reviews, "Best of" specials)
  - Episodes with no VTT file in streaming/
  - Episodes that already have a .chapters.vtt (unless --force)

OUTPUT
------
  streaming/MatM_XXXX.chapters.vtt
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

HERE       = Path(__file__).parent
STREAMING  = HERE / "streaming"
INDEX_HTML = HERE / "index.html"

# Public R2 base URL — used to fetch VTTs that aren't stored locally.
R2_BASE = "https://pub-fca72aca0d2a44489ca717888abac149.r2.dev"

# Seconds of buffer added after the sign-off cue ends before starting the
# next chapter — lands in the brief silence after the ratings/runtime line.
SIGN_OFF_BUFFER = 1.5


# ── Episode database ───────────────────────────────────────────────────────────

def load_episode_db() -> dict[int, list[str]]:
    """
    Parse the EPISODES array from index.html and return a dict of
    episode_number → [film titles].
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r"const EPISODES\s*=\s*(\[.*?\]);", html, re.DOTALL)
    if not m:
        raise RuntimeError("Could not find EPISODES array in index.html")
    episodes = json.loads(m.group(1))
    return {e["ep"]: e.get("films", []) for e in episodes}


# ── Time helpers ───────────────────────────────────────────────────────────────

def vtt_to_secs(t: str) -> float:
    t = t.strip()
    parts = t.split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def secs_to_vtt(s: float) -> str:
    h   = int(s // 3600)
    m   = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}"


# ── VTT parser ─────────────────────────────────────────────────────────────────

def parse_vtt(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    cues = []
    for block in re.split(r"\n\n+", text.strip()):
        lines = block.strip().splitlines()
        tl = next((l for l in lines if "-->" in l), None)
        if not tl:
            continue
        left, right = tl.split("-->")
        txt = " ".join(
            l for l in lines
            if "-->" not in l and not re.match(r"^\d+$", l.strip())
        ).strip()
        cues.append({
            "start": vtt_to_secs(left),
            "end":   vtt_to_secs(right),
            "text":  txt,
        })
    return cues


# ── Sign-off detection ─────────────────────────────────────────────────────────

def find_sign_off_indices(cues: list[dict]) -> list[int]:
    """
    Return the index of each cue that ends a film review.

    Detection: look for "runs for ... minutes" which appears exclusively in
    Madeleine's sign-off ("... runs for 90 minutes, and is screening in ...").
    This works regardless of whether she says "screening in", "in general
    release", "now showing", or any other release phrase.

    We check the current cue and the one before it together, in case the
    phrase spans a cue boundary. After a match we skip the next cue to
    prevent it double-triggering from the same lookback window.
    """
    indices = []
    i = 0
    while i < len(cues):
        window = " ".join(c["text"] for c in cues[max(0, i - 1): i + 1])
        if re.search(r"runs\s+for\b.{0,40}?\bminutes\b", window, re.IGNORECASE):
            indices.append(i)
            i += 2  # skip next cue — it would match the same window
        else:
            i += 1
    return indices


# ── Chapter builder ────────────────────────────────────────────────────────────

def build_chapters(
    cues:           list[dict],
    sign_off_idxs:  list[int],
    titles:         list[str],
) -> list[dict]:
    """
    Build chapter list from detected sign-off positions and supplied titles.

    Chapter N starts where chapter N-1's sign-off ended (plus buffer).
    Chapter 1 always starts at 0:00. The last chapter runs to end of episode.
    """
    n              = len(sign_off_idxs)
    total_duration = cues[-1]["end"] if cues else 0.0
    boundaries     = [cues[i]["end"] + SIGN_OFF_BUFFER for i in sign_off_idxs]

    chapter_starts = [0.0] + boundaries[:-1]
    chapter_ends   = boundaries[:-1] + [total_duration]

    chapters = []
    for i in range(n):
        chapters.append({
            "start": chapter_starts[i],
            "end":   chapter_ends[i],
            "title": titles[i] if i < len(titles) else f"Review {i + 1}",
        })
    return chapters


# ── VTT writer ─────────────────────────────────────────────────────────────────

def write_chapters_vtt(path: Path, chapters: list[dict]) -> None:
    lines = ["WEBVTT", ""]
    for ch in chapters:
        lines.append(f"{secs_to_vtt(ch['start'])} --> {secs_to_vtt(ch['end'])}")
        lines.append(ch["title"])
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# ── Per-episode processing ─────────────────────────────────────────────────────

def process_episode(
    ep_num:   int,
    titles:   list[str],
    force:    bool = False,
    verbose:  bool = True,
) -> str:
    """
    Process one episode. Returns 'ok', 'skip', or 'fail'.
    """
    tag      = f"MatM_{ep_num:04d}"
    vtt_path = STREAMING / f"{tag}.vtt"
    out_path = STREAMING / f"{tag}.chapters.vtt"

    def log(msg):
        if verbose:
            print(msg)

    if not vtt_path.exists():
        log(f"  — {tag}  no VTT file, skipping")
        return "skip"

    if out_path.exists() and not force:
        log(f"  — {tag}  already has chapters, skipping  (--force to overwrite)")
        return "skip"

    log(f"  ▶  {tag}")

    cues = parse_vtt(vtt_path)
    if not cues:
        log(f"     ✗  VTT is empty")
        return "fail"

    sign_off_idxs = find_sign_off_indices(cues)

    if not sign_off_idxs:
        log(f"     ✗  Sign-off pattern not found — add chapters manually to {out_path.name}")
        return "fail"

    # Validate: number of detected sign-offs should match number of titles.
    # Warn if mismatched but continue — the chapter file is still useful.
    if len(sign_off_idxs) != len(titles):
        log(f"     ⚠  Detected {len(sign_off_idxs)} sign-off(s) but database has "
            f"{len(titles)} title(s) — please verify {out_path.name}")

    chapters = build_chapters(cues, sign_off_idxs, titles)
    write_chapters_vtt(out_path, chapters)

    for ch in chapters:
        log(f"     [{secs_to_vtt(ch['start'])[3:]}]  {ch['title']}")

    log(f"     ✓  Written → {out_path.name}")
    return "ok"


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    args  = sys.argv[1:]
    force = "--force" in args
    args  = [a for a in args if a != "--force"]

    print()
    print("  MatM Chapter Generator")
    print("  ──────────────────────────────────────────────────")

    # Load episode database from index.html
    try:
        db = load_episode_db()
    except Exception as e:
        print(f"  ERROR loading episode database: {e}")
        sys.exit(1)

    # ── Batch mode ────────────────────────────────────────────────────────────
    if not args:
        # Find all episodes that have a VTT and 2+ film titles in the database.
        vtts = sorted(STREAMING.glob("MatM_*.vtt"))
        vtts = [v for v in vtts if ".chapters" not in v.name]

        if not vtts:
            print(f"  No VTT files found in {STREAMING}")
            print()
            sys.exit(0)

        # Determine which episodes are worth processing.
        candidates = []
        skipped_no_titles = []
        for vtt in vtts:
            m = re.search(r"MatM_(\d+)", vtt.name)
            if not m:
                continue
            ep_num = int(m.group(1))
            titles = db.get(ep_num, [])
            if len(titles) < 2:
                skipped_no_titles.append((ep_num, titles))
            else:
                candidates.append((ep_num, titles))

        print(f"  Batch mode — {len(vtts)} VTT file(s) found")
        if skipped_no_titles:
            print(f"  Skipping {len(skipped_no_titles)} episode(s) with fewer than 2 titles "
                  f"(festivals, specials, year reviews)")
        print()

        ok = skip = fail = 0
        for ep_num, titles in candidates:
            result = process_episode(ep_num, titles, force=force)
            if result == "ok":
                ok += 1
            elif result == "skip":
                skip += 1
            else:
                fail += 1
            print()

        print("  ──────────────────────────────────────────────────")
        print(f"  Done.  Created: {ok}   Skipped: {skip}   Failed: {fail}")
        print()
        sys.exit(0)

    # ── Single episode mode ───────────────────────────────────────────────────
    raw = args[0]
    if not raw.startswith("MatM_"):
        raw = "MatM_" + raw.zfill(4)
    m = re.search(r"(\d+)", raw)
    if not m:
        print(f"  ERROR: Could not parse episode number from '{args[0]}'")
        sys.exit(1)
    ep_num = int(m.group(1))

    # Titles: use command-line args if given, otherwise fall back to database.
    if len(args) > 1:
        titles = args[1:]
    else:
        titles = db.get(ep_num, [])
        if len(titles) < 2:
            print(f"  Episode {ep_num} has {len(titles)} title(s) in the database "
                  f"— no chapters needed.")
            print()
            sys.exit(0)

    result = process_episode(ep_num, titles, force=force)
    print()
    sys.exit(0 if result in ("ok", "skip") else 1)


if __name__ == "__main__":
    main()
