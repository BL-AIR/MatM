#!/usr/bin/env python3
"""
matm_publish.py — End-to-end episode publisher for Madeleine at the Movies.

USAGE
-----
  cd ~/Library/Mobile\ Documents/com~apple~CloudDocs/MatM/MatM
  python3 matm_publish.py 0725 "The Richest Woman in the World" "Finding Emily"

  Film titles must be in the order Madeleine reviews them in the episode —
  they are assigned to chapters sequentially as each sign-off is detected.

WHAT IT DOES (in order)
-----------------------
  1. Runs Whisper on the episode MP3 → SRT
  2. Converts MP3 to webm (Opus) and m4a (AAC) for streaming
  3. Converts SRT to WebVTT captions
  4. Corrects proper nouns in the VTT using the matching Word script (if found)
  5. Generates chapter markers from the VTT and supplied film titles
  6. Uploads webm, m4a, vtt, chapters.vtt to Cloudflare R2

WHAT STAYS MANUAL
-----------------
  - Renaming and placing the MP3 in the MatM source folder
  - Updating index.html with the new episode (ask Claude in Cowork)
  - Pushing to GitHub

DEPENDENCIES
-----------
  Required:
    ffmpeg      brew install ffmpeg
    whisper     pip install openai-whisper --break-system-packages
    boto3       pip install boto3 --break-system-packages

  Optional (enables VTT proper-noun correction):
    anthropic   pip install anthropic --break-system-packages

CREDENTIALS
-----------
  Place a file at:
    ~/Library/Mobile Documents/com~apple~CloudDocs/MatM/.r2_credentials

  Containing:
    R2_ACCESS_KEY_ID=your_key_id
    R2_SECRET_ACCESS_KEY=your_secret_key
    R2_ENDPOINT_URL=https://d2e2816d4dd8c6826c06eabb9d6606b3.r2.cloudflarestorage.com
    R2_BUCKET=matm-audio
    ANTHROPIC_API_KEY=sk-ant-...   (optional — enables Step 4 VTT correction)
"""

import os
import re
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


# ── Paths ─────────────────────────────────────────────────────────────────────

HERE         = Path(__file__).parent                        # MatM/MatM/
SOURCE       = HERE.parent                                  # MatM/
SCRIPTS      = SOURCE / "Scripts"                           # MatM/Scripts/
WORD_DIR     = SOURCE / "MatM Script Word Files"            # MatM/MatM Script Word Files/
STREAMING    = HERE / "streaming"                           # MatM/MatM/streaming/
CREDS_PATHS  = [SOURCE / ".r2_credentials", HERE / ".r2_credentials"]

SIGN_OFF_BUFFER = 1.5   # seconds added after sign-off before next chapter starts


# ── Credentials ───────────────────────────────────────────────────────────────

def load_credentials() -> dict:
    for path in CREDS_PATHS:
        if path.exists():
            creds = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
            return creds
    return {}


# ── Dependency check ──────────────────────────────────────────────────────────

def require(cmd: str, hint: str) -> None:
    if subprocess.run(["which", cmd], capture_output=True).returncode != 0:
        print(f"\n  ERROR: '{cmd}' not found.")
        print(f"  Install it with: {hint}\n")
        sys.exit(1)


# ── Time helpers ──────────────────────────────────────────────────────────────

def vtt_to_secs(t: str) -> float:
    parts = t.strip().split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def secs_to_vtt(s: float) -> str:
    h   = int(s // 3600)
    m   = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}"


# ── VTT parser ────────────────────────────────────────────────────────────────

def parse_vtt(path: Path) -> list[dict]:
    cues = []
    for block in re.split(r"\n\n+", path.read_text(encoding="utf-8").strip()):
        lines = block.strip().splitlines()
        tl = next((l for l in lines if "-->" in l), None)
        if not tl:
            continue
        left, right = tl.split("-->")
        txt = " ".join(
            l for l in lines
            if "-->" not in l and not re.match(r"^\d+$", l.strip())
        ).strip()
        cues.append({"start": vtt_to_secs(left), "end": vtt_to_secs(right), "text": txt})
    return cues


def find_sign_off_indices(cues: list[dict]) -> list[int]:
    """Find cues that end a film review (sign-off pattern: 'runs for X minutes')."""
    indices = []
    i = 0
    while i < len(cues):
        window = " ".join(c["text"] for c in cues[max(0, i - 1): i + 1])
        if re.search(r"runs\s+for\b.{0,40}?\bminutes\b", window, re.IGNORECASE):
            indices.append(i)
            i += 2
        else:
            i += 1
    return indices


# ── Step 1: Whisper transcription ─────────────────────────────────────────────

def step_whisper(mp3_path: Path, srt_path: Path) -> None:
    print("\n  Step 1 — Whisper transcription")

    if srt_path.exists():
        print(f"     — SRT already exists, skipping  ({srt_path.name})")
        return

    SCRIPTS.mkdir(exist_ok=True)
    print(f"     Running Whisper on {mp3_path.name} …  (this takes a few minutes)")

    result = subprocess.run(
        ["whisper", str(mp3_path),
         "--output_format", "srt",
         "--language", "en",
         "--output_dir", str(SCRIPTS)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"     ✗  Whisper failed:\n{result.stderr}")
        sys.exit(1)

    # Whisper names output after the input stem; rename to canonical name if needed
    whisper_out = SCRIPTS / (mp3_path.stem + ".srt")
    if whisper_out.exists() and whisper_out != srt_path:
        whisper_out.rename(srt_path)
    elif not srt_path.exists():
        print(f"     ✗  Expected SRT not found at {srt_path}")
        sys.exit(1)

    print(f"     ✓  SRT → {srt_path.name}")


# ── Step 2: Audio conversion ──────────────────────────────────────────────────

def step_audio(mp3_path: Path, ep_tag: str) -> None:
    print("\n  Step 2 — Audio conversion")
    STREAMING.mkdir(exist_ok=True)

    webm = STREAMING / f"{ep_tag}.webm"
    if not webm.exists():
        subprocess.run(
            ["ffmpeg", "-i", str(mp3_path), "-c:a", "libopus", "-b:a", "48k",
             "-vn", "-y", str(webm), "-loglevel", "error"],
            check=True,
        )
        print(f"     ✓  Opus  → {webm.name}")
    else:
        print(f"     — Opus  already exists, skipping")

    m4a = STREAMING / f"{ep_tag}.m4a"
    if not m4a.exists():
        subprocess.run(
            ["ffmpeg", "-i", str(mp3_path), "-c:a", "aac", "-b:a", "64k",
             "-vn", "-y", str(m4a), "-loglevel", "error"],
            check=True,
        )
        print(f"     ✓  AAC   → {m4a.name}")
    else:
        print(f"     — AAC   already exists, skipping")


# ── Step 3: SRT → VTT ────────────────────────────────────────────────────────

def step_srt_to_vtt(srt_path: Path, ep_tag: str) -> Path:
    print("\n  Step 3 — SRT → VTT")

    vtt_path = STREAMING / f"{ep_tag}.vtt"
    srt_text = srt_path.read_text(encoding="utf-8")
    vtt_text = "WEBVTT\n\n" + re.sub(
        r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", srt_text
    )
    vtt_path.write_text(vtt_text, encoding="utf-8")
    print(f"     ✓  VTT   → {vtt_path.name}")
    return vtt_path


# ── Step 4: VTT correction ────────────────────────────────────────────────────

def find_word_script(ep_num: int) -> "Path | None":
    """Find the matching Word script, tolerating case and spacing quirks."""
    for pattern in [f"MATM_{ep_num:04d}.docx", f"MATM_{ep_num:04d} .docx",
                    f"MatM_{ep_num:04d}.docx"]:
        p = WORD_DIR / pattern
        if p.exists():
            return p
    matches = list(WORD_DIR.glob(f"*{ep_num:04d}*.docx"))
    return matches[0] if matches else None


def extract_script_text(docx_path: Path) -> str:
    """Extract clean script text from docx, stripping trailing release-date notes."""
    with zipfile.ZipFile(docx_path) as z:
        with z.open("word/document.xml") as f:
            tree = ET.parse(f)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    lines = []
    for p in tree.findall(".//w:p", ns):
        line = "".join(t.text or "" for t in p.findall(".//w:t", ns)).strip()
        lines.append(line)

    # Trim everything after the sign-off line (release notes live below it)
    cutoff = len(lines)
    for i, line in enumerate(lines):
        if re.search(r"and that'?s all from me", line, re.IGNORECASE):
            cutoff = min(i + 3, len(lines))
            break

    return "\n".join(lines[:cutoff]).strip()


def step_correct_vtt(ep_num: int, ep_tag: str, api_key: "str | None") -> None:
    print("\n  Step 4 — VTT correction")

    if not api_key:
        print("     — No Anthropic API key found, skipping")
        print("       Add ANTHROPIC_API_KEY to your .r2_credentials file to enable this")
        return

    word_path = find_word_script(ep_num)
    if not word_path:
        print(f"     — No Word script found for {ep_tag}, skipping")
        return

    print(f"     Script : {word_path.name}")

    try:
        import anthropic
    except ImportError:
        print("     — 'anthropic' not installed, skipping")
        print("       pip install anthropic --break-system-packages")
        return

    vtt_path    = STREAMING / f"{ep_tag}.vtt"
    script_text = extract_script_text(word_path)
    vtt_text    = vtt_path.read_text(encoding="utf-8")

    print("     Calling Claude …")
    client   = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8096,
        system=(
            "You are a transcript editor. You will be given a film critic's script "
            "and a Whisper-generated WebVTT transcript of the same recording. "
            "Correct proper nouns in the VTT — film titles, director names, actor names, "
            "place names, award names — using the script as the authoritative spelling reference. "
            "Rules: "
            "(1) Preserve every timestamp exactly as-is. "
            "(2) Do not rewrite or restructure sentences — only fix misspelled proper nouns. "
            "(3) Only correct words that are clearly wrong versions of names or titles "
            "found in the script. "
            "(4) Return only the corrected VTT content, with no commentary."
        ),
        messages=[{
            "role": "user",
            "content": f"SCRIPT:\n{script_text}\n\nVTT:\n{vtt_text}",
        }],
    )

    corrected = response.content[0].text.strip()
    if not corrected.startswith("WEBVTT"):
        print("     ⚠  Unexpected response from Claude — correction skipped, VTT unchanged")
        return

    vtt_path.write_text(corrected, encoding="utf-8")
    print(f"     ✓  Corrected → {vtt_path.name}")


# ── Step 5: Chapter detection ─────────────────────────────────────────────────

def step_chapters(ep_tag: str, titles: list[str]) -> None:
    print("\n  Step 5 — Chapter detection")

    vtt_path     = STREAMING / f"{ep_tag}.vtt"
    chapters_out = STREAMING / f"{ep_tag}.chapters.vtt"

    cues = parse_vtt(vtt_path)
    if not cues:
        print("     ✗  VTT is empty")
        return

    sign_off_idxs = find_sign_off_indices(cues)
    if not sign_off_idxs:
        print("     ✗  Sign-off pattern not found — add chapters manually")
        return

    if len(sign_off_idxs) != len(titles):
        print(f"     ⚠  Detected {len(sign_off_idxs)} review(s) but "
              f"{len(titles)} title(s) supplied — verify {chapters_out.name}")

    total    = cues[-1]["end"]
    bounds   = [cues[i]["end"] + SIGN_OFF_BUFFER for i in sign_off_idxs]
    starts   = [0.0] + bounds[:-1]
    ends     = bounds[:-1] + [total]

    lines = ["WEBVTT", ""]
    for i, (start, end) in enumerate(zip(starts, ends)):
        title = titles[i] if i < len(titles) else f"Review {i + 1}"
        lines += [f"{secs_to_vtt(start)} --> {secs_to_vtt(end)}", title, ""]
        print(f"     [{secs_to_vtt(start)[3:]}]  {title}")

    chapters_out.write_text("\n".join(lines), encoding="utf-8")
    print(f"     ✓  Written → {chapters_out.name}")


# ── Step 6: R2 upload ─────────────────────────────────────────────────────────

def step_upload(ep_tag: str, creds: dict) -> None:
    print("\n  Step 6 — R2 upload")

    try:
        import boto3
    except ImportError:
        print("     ✗  boto3 not installed — skipping upload")
        print("       pip install boto3 --break-system-packages")
        return

    client = boto3.client(
        "s3",
        endpoint_url=creds["R2_ENDPOINT_URL"],
        aws_access_key_id=creds["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=creds["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    bucket = creds["R2_BUCKET"]

    uploads = [
        (STREAMING / f"{ep_tag}.webm",         "audio/webm"),
        (STREAMING / f"{ep_tag}.m4a",           "audio/mp4"),
        (STREAMING / f"{ep_tag}.vtt",           "text/vtt"),
        (STREAMING / f"{ep_tag}.chapters.vtt",  "text/vtt"),
    ]

    for path, content_type in uploads:
        if not path.exists():
            print(f"     — {path.name}  not found, skipping")
            continue
        try:
            client.upload_file(
                str(path), bucket, path.name,
                ExtraArgs={"ContentType": content_type},
            )
            print(f"     ↑  {path.name}")
        except Exception as e:
            print(f"     ✗  {path.name}: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    # Episode number
    m = re.search(r"\d+", args[0])
    if not m:
        print(f"\n  ERROR: Could not parse episode number from '{args[0]}'\n")
        sys.exit(1)
    ep_num = int(m.group())
    ep_tag = f"MatM_{ep_num:04d}"

    # Film titles
    titles = args[1:]
    if not titles:
        print(f"\n  ERROR: Please supply at least one film title.")
        print(f'  Usage: python3 matm_publish.py {ep_num} "Film One" "Film Two"\n')
        sys.exit(1)

    # Credentials
    creds   = load_credentials()
    api_key = creds.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    has_r2  = "R2_ACCESS_KEY_ID" in creds

    # Dependencies
    require("ffmpeg",  "brew install ffmpeg")
    require("whisper", "pip install openai-whisper --break-system-packages")

    # Locate MP3
    mp3_path = SOURCE / f"{ep_tag}.mp3"
    if not mp3_path.exists():
        candidates = list(SOURCE.glob(f"*{ep_num:04d}*.mp3"))
        if not candidates:
            print(f"\n  ERROR: MP3 not found. Expected: {mp3_path}\n")
            sys.exit(1)
        mp3_path = candidates[0]

    srt_path = SCRIPTS / f"{ep_tag}.srt"

    # Header
    print()
    print(f"  MatM Publisher — {ep_tag}")
    print(f"  ────────────────────────────────────────────────────")
    print(f"  MP3      : {mp3_path.name}")
    print(f"  Films    : {', '.join(titles)}")
    print(f"  Correct  : {'yes (Claude Haiku)' if api_key else 'no  — add ANTHROPIC_API_KEY to .r2_credentials'}")
    print(f"  Upload   : {'yes' if has_r2 else 'no  — add R2 credentials to .r2_credentials'}")
    print(f"  ────────────────────────────────────────────────────")

    # Run all steps
    step_whisper(mp3_path, srt_path)
    step_audio(mp3_path, ep_tag)
    step_srt_to_vtt(srt_path, ep_tag)
    step_correct_vtt(ep_num, ep_tag, api_key)
    step_chapters(ep_tag, titles)

    if has_r2:
        step_upload(ep_tag, creds)
    else:
        print("\n  Step 6 — R2 upload")
        print("     — No R2 credentials found, skipping")

    # Done
    print()
    print(f"  ────────────────────────────────────────────────────")
    print(f"  Done.")
    print(f"  Next: ask Claude in Cowork to add {ep_tag} to the website,")
    print(f"  then push to GitHub.")
    print(f"  ────────────────────────────────────────────────────")
    print()


if __name__ == "__main__":
    main()
