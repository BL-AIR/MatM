#!/usr/bin/env python3
"""
Build the MatM film index data files from the EPISODES array in index.html.

Reads   : index.html  (the EPISODES array)
          data/overrides.json   (hand-maintained corrections — never written by this script)
Writes  : data/films.json
          data/people.json

Overrides always win over a TMDB lookup, so re-running this can never silently
revert a correction. Run it from the repo root, or pass --repo.

    python3 tools/enrich.py                 # uses the key found in index.html
    TMDB_KEY=xxxx python3 tools/enrich.py   # or supply one (GitHub Actions does this)
"""

import argparse
import difflib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://api.themoviedb.org/3"
R2 = "https://pub-fca72aca0d2a44489ca717888abac149.r2.dev"
CAST_DEPTH = 3          # top-billed cast kept per film
DIRECTOR_DEPTH = 1      # directors kept per film
SIM_THRESHOLD = 0.92    # below this, a match is flagged for human review
YEAR_TOLERANCE = 1

# Titles that are not films at all. Mirrors the SKIP lists in index.html.
SKIP_EXACT = {
    "best of 2025", "best of 2024", "best of 2023", "best of 2022",
    "2022 in review", "2023 in review", "2024 in review", "2025 in review",
    "top 10 of all time", "mqff", "cat video fest",
    "interview with noni hazelhurst", "one mind, one heart",
}
SKIP_CONTAINS = [
    "film festival", "best of 20", " in review",
    "national theatre live", "oscar short", "catvideofest",
]


# ---------------------------------------------------------------- helpers

def norm(s):
    """Fold accents, case and punctuation so titles compare sensibly."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_title(t):
    return re.sub(r"[.!?,;:]+$", "", t).strip()


def is_non_film(title):
    n = title.lower().strip()
    return n in SKIP_EXACT or any(p in n for p in SKIP_CONTAINS)


def api_get(path, **params):
    params["api_key"] = KEY
    url = API + path + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                return json.load(r)
        except Exception as exc:          # noqa: BLE001 - retry anything transient
            last = exc
            time.sleep(0.6 * (attempt + 1))
    raise last


# ---------------------------------------------------------------- inputs

def read_episodes(index_html):
    src = index_html.read_text(encoding="utf-8")
    m = re.search(r"const EPISODES = (\[.*?\]);", src, re.S)
    if not m:
        sys.exit("Could not find the EPISODES array in index.html")
    key = None
    k = re.search(r"TMDB_KEY\s*=\s*'([^']+)'", src)
    if k:
        key = k.group(1)
    return json.loads(m.group(1)), key


class BlockedR2(Exception):
    """Raised when R2 is unreachable from this network, rather than absent."""


def fetch_chapters(ep_num):
    """
    Chapter names as they actually exist in the VTT on R2.

    The player matches ?chapter= against this text exactly, and the VTT was
    written at publish time from the titles as Blair typed them then. So the
    VTT — not the EPISODES array — is the authority for the deep link, and a
    later typo fix in EPISODES cannot break it.
    """
    url = "%s/MatM_%04d.chapters.vtt" % (R2, ep_num)
    # r2.dev sits behind Cloudflare's bot filtering and refuses a default
    # urllib user-agent with a 403, which reads exactly like "no access".
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/128.0 Safari/537.36"),
        "Accept": "text/vtt,text/plain,*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status != 200:
                return []
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 403 means the network this is running on cannot reach R2 (a sandboxed
        # Cowork container does not). 404 means the episode genuinely has no
        # chapters. Only the first is worth shouting about.
        if exc.code in (401, 403, 407):
            raise BlockedR2(exc)
        return []
    except Exception:                            # noqa: BLE001 - old episodes have none
        return []
    names = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line == "WEBVTT" or "-->" in line or line.isdigit():
            continue
        if line.upper().startswith(("NOTE", "STYLE", "REGION")):
            continue
        names.append(line)
    return names


def resolve_chapter(title, chapter_names):
    """Exact normalised match first, then a close fuzzy match, else nothing."""
    if not chapter_names:
        return None
    target = norm(title)
    for name in chapter_names:
        if norm(name) == target:
            return name
    best, score = None, 0.0
    for name in chapter_names:
        s = difflib.SequenceMatcher(None, target, norm(name)).ratio()
        if s > score:
            best, score = name, s
    return best if score >= 0.8 else None


def unique_titles(episodes):
    """One entry per unique title, carrying the episode it first appeared in."""
    out, seen = [], set()
    for ep in episodes:
        year = int(ep["date"].split()[-1])
        for film in ep["films"]:
            title = film.strip()
            k = norm(title)
            if not k or k in seen:
                continue
            seen.add(k)
            out.append({"title": title, "year": year, "ep": ep["ep"]})
    return out


# ---------------------------------------------------------------- lookup

def lookup(item, overrides):
    """Resolve one title. Overrides are consulted first and always win."""
    title, year = item["title"], item["year"]
    rec = {
        "t": title,          # title as listed in EPISODES
        "n": title,          # display title, corrected where an override says so
        "y": year,
        "e": item["ep"],
        "s": "ok",
        "i": None, "g": [], "d": None, "c": [],
    }

    ov = overrides.get(norm(title))
    if ov:
        if ov.get("kind") in ("tv", "nonfilm", "exclude"):
            rec["s"] = ov["kind"]
            if ov.get("note"):
                rec["note"] = ov["note"]
            return rec
        if ov.get("title"):
            rec["n"] = ov["title"]
        if ov.get("tmdb"):
            try:
                det = api_get("/movie/%d" % ov["tmdb"], append_to_response="credits")
                fill_from_detail(rec, det)
                rec["s"] = "ok"
                rec["pinned"] = True
                return rec
            except Exception:                    # noqa: BLE001
                rec["s"] = "error"
                return rec

    if is_non_film(title):
        rec["s"] = "nonfilm"
        return rec

    query = clean_title(rec["n"])
    single_word = " " not in query

    try:
        results = (api_get("/search/movie", query=query, language="en-US", page=1)
                   .get("results") or [])
    except Exception:                            # noqa: BLE001
        rec["s"] = "error"
        return rec

    candidates = ([m for m in results if norm(m["title"]) == norm(query)]
                  if single_word else results)
    match = next(
        (m for m in candidates
         if m.get("release_date")
         and abs(int(m["release_date"][:4]) - year) <= YEAR_TOLERANCE),
        None,
    )
    if not match:
        rec["s"] = "nomatch"
        return rec

    try:
        det = api_get("/movie/%d" % match["id"], append_to_response="credits")
    except Exception:                            # noqa: BLE001
        rec["s"] = "error"
        return rec

    fill_from_detail(rec, det)
    sim = difflib.SequenceMatcher(None, norm(query), norm(det.get("title") or "")).ratio()
    rec["sim"] = round(sim, 3)
    if sim < SIM_THRESHOLD:
        rec["s"] = "review"
    return rec


def fill_from_detail(rec, det):
    credits = det.get("credits") or {}
    directors = [c for c in (credits.get("crew") or []) if c.get("job") == "Director"]
    cast = (credits.get("cast") or [])[:CAST_DEPTH]
    rec["i"] = det["id"]
    rec["g"] = [g["id"] for g in det.get("genres", [])]
    rec["d"] = directors[0]["id"] if directors else None
    rec["c"] = [c["id"] for c in cast]
    rec["p"] = det.get("poster_path") or None
    rec["_people"] = {}
    for person in directors[:DIRECTOR_DEPTH] + cast:
        rec["_people"][str(person["id"])] = person["name"]
    if det.get("title"):
        rec["_tmdb_title"] = det["title"]


# ---------------------------------------------------------------- output

def verify(films, people, episodes):
    """Fail loudly rather than shipping a broken index."""
    problems = []
    known_eps = {e["ep"] for e in episodes}
    for f in films:
        if f["e"] not in known_eps:
            problems.append("film %r points at unknown episode %s" % (f["n"], f["e"]))
        for pid in ([f["d"]] if f["d"] else []) + f["c"]:
            if str(pid) not in people:
                problems.append("film %r references person %s missing from people.json" % (f["n"], pid))
    if problems:
        for p in problems[:20]:
            print("  FAIL:", p, file=sys.stderr)
        sys.exit("verification failed: %d problem(s)" % len(problems))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", help="repo root containing index.html")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    data = repo / "data"
    data.mkdir(exist_ok=True)

    episodes, key_from_page = read_episodes(repo / "index.html")

    global KEY
    KEY = os.environ.get("TMDB_KEY") or key_from_page
    if not KEY:
        sys.exit("No TMDB key: set TMDB_KEY, or leave TMDB_KEY in index.html")

    ov_path = data / "overrides.json"
    raw_overrides = json.loads(ov_path.read_text(encoding="utf-8")) if ov_path.exists() else {}
    overrides = {norm(k): v for k, v in raw_overrides.items()}

    titles = unique_titles(episodes)
    print("%d unique titles from %d episodes" % (len(titles), len(episodes)))

    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        records = list(pool.map(lambda t: lookup(t, overrides), titles))
    print("lookup took %.0fs" % (time.time() - started))

    # Chapter names, read from the VTTs themselves where the network allows it.
    wanted = sorted({r["e"] for r in records if r["s"] == "ok"})
    started = time.time()
    chapters_verified = True
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            chapter_map = dict(zip(wanted, pool.map(fetch_chapters, wanted)))
        with_chapters = sum(1 for v in chapter_map.values() if v)
        print("chapter VTTs: %d of %d episodes reachable (%.0fs)"
              % (with_chapters, len(wanted), time.time() - started))
    except BlockedR2:
        chapters_verified = False
        chapter_map = {}
        print("chapter VTTs: R2 unreachable from this network — falling back to\n"
              "  the titles as listed, which is correct until a title is edited.\n"
              "  Re-run where R2 is reachable (the GitHub Action is) to verify them.",
              file=sys.stderr)

    for rec in records:
        if rec["s"] != "ok":
            continue
        ch = resolve_chapter(rec["t"], chapter_map.get(rec["e"], []))
        # Falling back to the listed title is right whenever the title has not
        # been edited since publication — the VTT was written from it.
        rec["ch"] = ch or rec["t"]

    people = {}
    for rec in records:
        people.update(rec.pop("_people", {}))
        rec.pop("_tmdb_title", None)

    # Films the page will render, plus the ones held back, with the reason.
    films = [r for r in records if r["s"] == "ok"]
    held = [r for r in records if r["s"] != "ok"]

    for f in films:
        if f["n"] == f["t"]:
            del f["n"]          # only carry a display title when it differs
        if f.get("ch") == f["t"]:
            del f["ch"]         # the chapter key defaults to the listed title
        f.pop("sim", None)

    try:
        genres = {str(g["id"]): g["name"]
                  for g in api_get("/genre/movie/list", language="en-US").get("genres", [])}
    except Exception:                            # noqa: BLE001
        genres = {}

    used_eps = sorted({f["e"] for f in films})
    ep_dates = {str(e["ep"]): e["date"] for e in episodes if e["ep"] in set(used_eps)}

    payload = {
        "generated": time.strftime("%Y-%m-%d"),
        "genres": genres,
        "episodes": ep_dates,
        "counts": {
            "total": len(records),
            "indexed": len(films),
            "held": len(held),
            "byReason": {k: sum(1 for r in held if r["s"] == k)
                         for k in sorted({r["s"] for r in held})},
            "chaptersVerified": chapters_verified,
        },
        "films": films,
        "held": [{"t": r["t"], "y": r["y"], "e": r["e"], "s": r["s"]} for r in held],
    }

    used = set()
    for f in films:
        if f["d"]:
            used.add(str(f["d"]))
        used.update(str(c) for c in f["c"])
    people = {k: v for k, v in people.items() if k in used}

    verify(films, people, episodes)

    (data / "films.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (data / "people.json").write_text(
        json.dumps(people, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print("chapters verified against the VTTs on R2: %s"
          % ("yes" if chapters_verified else "NO - falling back to titles as listed"))
    print("indexed %d films, held %d (%s)" % (
        len(films), len(held),
        ", ".join("%s %d" % (k, v) for k, v in payload["counts"]["byReason"].items())))
    print("films.json  %5.1f KB" % ((data / "films.json").stat().st_size / 1024))
    print("people.json %5.1f KB (%d names)" % (
        (data / "people.json").stat().st_size / 1024, len(people)))


if __name__ == "__main__":
    main()
