#!/usr/bin/env python3
"""
build_video.py — Marketing Auto (headless, GitHub Actions edition)

Input : text script + list of media (images and/or videos, URL or local path)
Voice : Microsoft edge-tts (free, no API key needed)
Output: single .mp4, auto-uploaded to Dropbox

Env vars expected (set by the workflow from `workflow_dispatch` inputs / secrets):
    MEDIA_URLS          comma or newline separated list of image/video URLs (or local paths under ./input_media)
    SCRIPT_TEXT          voiceover script text
    VOICE                edge-tts voice name, e.g. en-US-JennyNeural
    RATE                 edge-tts rate, e.g. -15%
    RESOLUTION           tiktok | youtube | original
    MOTION               kenburns | static   (only applies to images)
    OUTPUT_NAME          e.g. marketing_video.mp4
    DROPBOX_FOLDER       e.g. /Marketing
    CAPTION_ENABLE       "true" | "false"
    CAPTION_STYLE        poh_gold | poh_box_plum | classic_white
    CAPTION_POSITION     bottom | center | top
    CAPTION_WORDS        words grouped per caption card, e.g. "4"
    CAPTION_FONT_SIZE    e.g. "40"
    DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN   (repo secrets)

Caption timing is taken directly from edge-tts's own word-boundary events
(the exact timestamps Microsoft's TTS engine used to speak each word) —
no separate speech-to-text pass is needed, so captions are frame-accurate
and free.
"""

import os
import re
import sys
import json
import asyncio
import tempfile
import subprocess
from pathlib import Path

import requests

# ── constants ──────────────────────────────────────────────
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
FPS = 30
DBX_API = "https://api.dropboxapi.com/2"
DBX_CONTENT = "https://content.dropboxapi.com/2"

# ASS colours are &HAABBGGRR (alpha,blue,green,red — hex, alpha 00=opaque).
# Built from POH brand hex: ivory #FBF8F2, plum #6E4A5E, gold #B08D43.
CAPTION_PRESETS = {
    "poh_gold": {
        # Ivory text, plum outline — matches POH's editorial palette, no box.
        "primary_color": "&H00F2F8FB", "outline_color": "&H005E4A6E",
        "back_color": "&H00000000", "outline_width": 2, "shadow": 1,
        "bold": 0, "border_style": 1,
    },
    "poh_box_plum": {
        # Ivory text on a soft translucent plum box — reads well on busy photos.
        "primary_color": "&H00F2F8FB", "outline_color": "&H00000000",
        "back_color": "&H905E4A6E", "outline_width": 0, "shadow": 0,
        "bold": 1, "border_style": 3,
    },
    "classic_white": {
        # Safe fallback: white text, black outline, no box.
        "primary_color": "&H00FFFFFF", "outline_color": "&H00000000",
        "back_color": "&H00000000", "outline_width": 2, "shadow": 1,
        "bold": 0, "border_style": 1,
    },
}


def log(msg):
    print(f"[build_video] {msg}", flush=True)


def fail(msg):
    log(f"ERROR: {msg}")
    sys.exit(1)


# ── shell helpers ──────────────────────────────────────────
def run_ff(cmd, timeout=900):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        return False, r.stderr.decode(errors="ignore")[-1500:]
    return True, ""


def ffprobe_json(path):
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", path],
        capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {}


def get_duration(path):
    data = ffprobe_json(path)
    try:
        return float(data.get("format", {}).get("duration", 0) or 0)
    except Exception:
        return 0.0


def has_audio_stream(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    return "audio" in (r.stdout or "")


def get_dims(path):
    data = ffprobe_json(path)
    w = h = 0
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            w, h = s.get("width", 0), s.get("height", 0)
            break
    if not w or not h:
        w, h = 1280, 720
    w -= w % 2
    h -= h % 2
    return w, h


# ── media download ─────────────────────────────────────────
def is_url(s):
    return s.startswith("http://") or s.startswith("https://")


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


def download(url, dest):
    r = requests.get(url, headers=HEADERS, stream=True, timeout=120)
    r.raise_for_status()
    size = 0
    with open(dest, "wb") as f:
        for chunk in r.iter_content(512 * 1024):
            f.write(chunk)
            size += len(chunk)
    if size < 512:
        raise ValueError(f"Downloaded file too small ({size} bytes)")


def resolve_media_list(raw_list, tmp_dir):
    """Download URLs / copy local paths, classify each as image or video."""
    items = []
    for i, entry in enumerate(raw_list):
        entry = entry.strip()
        if not entry:
            continue
        ext = Path(entry.split("?")[0]).suffix.lower()
        if not ext:
            log(f"Skip (no extension): {entry}")
            continue
        kind = "image" if ext in IMAGE_EXTS else ("video" if ext in VIDEO_EXTS else None)
        if kind is None:
            log(f"Skip (unsupported extension {ext}): {entry}")
            continue

        dest = os.path.join(tmp_dir, f"media_{i:03d}{ext}")
        if is_url(entry):
            log(f"Downloading {kind}: {entry}")
            download(entry, dest)
        else:
            src = Path(entry)
            if not src.exists():
                fail(f"Local media not found: {entry}")
            dest = str(src)
        items.append({"path": dest, "kind": kind})
    if not items:
        fail("No valid media (images/videos) resolved from MEDIA_URLS.")
    return items


# ── TTS (edge-tts, free Microsoft voices) ─────────────────
def normalize_rate(rate):
    """edge-tts requires an explicit +/- sign, e.g. '+0%' not '0%'."""
    rate = (rate or "+0%").strip()
    if not re.match(r"^[+-]\d+%$", rate):
        sign = "-" if rate.strip().startswith("-") else "+"
        digits = re.sub(r"[^\d]", "", rate) or "0"
        rate = f"{sign}{digits}%"
    return rate


def synth_voice_with_captions(text, voice, rate, audio_out, srt_out):
    """
    Generate the voiceover AND a word-level SRT from edge-tts's own
    WordBoundary events (the exact timing it used to speak the text) —
    no separate speech-to-text pass needed, so captions are frame-accurate.
    """
    import edge_tts

    rate = normalize_rate(rate)

    async def _run():
        communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
        submaker = edge_tts.SubMaker()
        with open(audio_out, "wb") as f:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    # Support both the modern (feed) and older (create_sub) SubMaker APIs.
                    if hasattr(submaker, "feed"):
                        submaker.feed(chunk)
                    else:
                        submaker.create_sub(
                            (chunk["offset"], chunk["duration"]), chunk["text"])
        if hasattr(submaker, "get_srt"):
            srt_text = submaker.get_srt()
        else:
            srt_text = submaker.generate_subs()
        with open(srt_out, "w", encoding="utf-8") as f:
            f.write(srt_text)

    asyncio.run(_run())
    if not os.path.exists(audio_out) or os.path.getsize(audio_out) < 1024:
        fail("edge-tts did not produce a usable audio file (check network egress / voice name).")
    if not os.path.exists(srt_out) or os.path.getsize(srt_out) < 10:
        log("WARNING: no word-boundary captions were captured — captions will be skipped.")


# ── resolution / clip building ─────────────────────────────
def target_dims(resolution, media_items):
    if resolution == "tiktok":
        return 1080, 1920
    if resolution == "youtube":
        return 1920, 1080
    # "original" -> use first media item's own dimensions
    return get_dims(media_items[0]["path"])


def scale_pad_filter(w, h):
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")


def build_image_clip(img_path, duration, w, h, motion, out_path):
    vf_scale = scale_pad_filter(w, h)
    if motion == "kenburns":
        # Upscale first so the zoompan crop has room to move, then pad to target.
        up_w, up_h = w * 2, h * 2
        vf = (f"scale={up_w}:{up_h}:force_original_aspect_ratio=increase,"
              f"crop={up_w}:{up_h},"
              f"zoompan=z='min(zoom+0.0006,1.16)':d={int(FPS*duration)}:s={w}x{h}:fps={FPS}")
    else:
        vf = f"{vf_scale},fps={FPS}"
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", img_path, "-t", str(duration),
           "-vf", vf, "-c:v", "libx264", "-preset", "fast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-an", out_path]
    ok, err = run_ff(cmd)
    if not ok:
        fail(f"Failed to build image clip [{os.path.basename(img_path)}]: {err}")


def build_video_clip(vid_path, duration, w, h, out_path):
    src_dur = get_duration(vid_path) or duration
    vf = f"{scale_pad_filter(w, h)},fps={FPS}"
    if src_dur < duration - 0.05:
        cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", vid_path, "-t", str(duration),
               "-vf", vf, "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               "-pix_fmt", "yuv420p", "-an", out_path]
    else:
        cmd = ["ffmpeg", "-y", "-i", vid_path, "-t", str(duration),
               "-vf", vf, "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               "-pix_fmt", "yuv420p", "-an", out_path]
    ok, err = run_ff(cmd)
    if not ok:
        fail(f"Failed to build video clip [{os.path.basename(vid_path)}]: {err}")


def build_final_video(media_items, voice_path, resolution, motion, output_path, tmp_dir):
    voice_dur = get_duration(voice_path)
    if not voice_dur or voice_dur <= 0:
        fail("Could not read voiceover duration.")
    log(f"Voiceover duration: {voice_dur:.1f}s across {len(media_items)} media item(s)")

    w, h = target_dims(resolution, media_items)
    per_item = max(0.8, voice_dur / len(media_items))

    clips = []
    for i, item in enumerate(media_items):
        clip_path = os.path.join(tmp_dir, f"clip_{i:03d}.mp4")
        if item["kind"] == "image":
            build_image_clip(item["path"], per_item, w, h, motion, clip_path)
        else:
            build_video_clip(item["path"], per_item, w, h, clip_path)
        clips.append(clip_path)
        log(f"Built clip {i+1}/{len(media_items)} ({item['kind']}, {per_item:.1f}s)")

    concat_txt = os.path.join(tmp_dir, "concat.txt")
    with open(concat_txt, "w") as f:
        f.write("\n".join(f"file '{c}'" for c in clips))
    silent_video = os.path.join(tmp_dir, "silent.mp4")
    ok, err = run_ff(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt,
                      "-c", "copy", silent_video])
    if not ok:
        fail(f"Concat failed: {err}")

    voice_aac = os.path.join(tmp_dir, "voice.aac")
    ok, err = run_ff(["ffmpeg", "-y", "-i", voice_path, "-vn", "-c:a", "aac",
                      "-b:a", "192k", "-ar", "44100", "-ac", "2", voice_aac])
    if not ok:
        fail(f"Voice re-encode failed: {err}")

    ok, err = run_ff(["ffmpeg", "-y", "-i", silent_video, "-i", voice_aac,
                      "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                      "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                      "-movflags", "+faststart", output_path])
    if not ok:
        fail(f"Final mux failed: {err}")
    log(f"Final video ready: {output_path}")
    return w, h


# ── Captions: word-SRT → grouped SRT → ASS → burn-in ──────
def _srt_ts_to_seconds(ts):
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _seconds_to_srt_ts(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt(path):
    raw = open(path, encoding="utf-8", errors="ignore").read().strip()
    entries = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        m = re.match(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", lines[1])
        if not m:
            continue
        text = " ".join(lines[2:]).strip()
        if text:
            entries.append((m.group(1), m.group(2), text))
    return entries


def merge_srt_words(entries, words_per_caption):
    words_per_caption = max(1, int(words_per_caption))
    merged = []
    for i in range(0, len(entries), words_per_caption):
        group = entries[i:i + words_per_caption]
        start = group[0][0]
        end = group[-1][1]
        text = " ".join(g[2] for g in group)
        merged.append((start, end, text))
    return merged


def write_srt(entries, path):
    lines = []
    for idx, (start, end, text) in enumerate(entries, 1):
        lines += [str(idx), f"{start} --> {end}", text, ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def build_caption_srt(raw_srt_path, words_per_caption, grouped_srt_path):
    entries = parse_srt(raw_srt_path)
    if not entries:
        return False
    merged = merge_srt_words(entries, words_per_caption)
    write_srt(merged, grouped_srt_path)
    return True


def srt_to_ass(srt_path, ass_path, style, position, font_size, width, height):
    alignment_map = {"bottom": 2, "center": 5, "top": 8}
    margin_v = max(20, int(height * 0.06))
    alignment = alignment_map.get(position, 2)
    if position == "center":
        margin_v = 0

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{font_size},{style['primary_color']},&H000000FF,{style['outline_color']},{style['back_color']},{style['bold']},0,0,0,100,100,0,0,{style['border_style']},{style['outline_width']},{style['shadow']},{alignment},40,40,{margin_v},1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def srt_ts_to_ass(ts):
        secs = _srt_ts_to_seconds(ts)
        h = int(secs // 3600)
        m = int((secs % 3600) // 60)
        s = secs % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    events = []
    for start, end, text in parse_srt(srt_path):
        text = re.sub(r"<[^>]+>", "", text).replace("\n", "\\N")
        events.append(f"Dialogue: 0,{srt_ts_to_ass(start)},{srt_ts_to_ass(end)},Default,,0,0,0,,{text}")

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events))


def burn_captions(video_path, ass_path, output_path):
    ass_esc = ass_path.replace("\\", "/")
    cmd = ["ffmpeg", "-y", "-i", video_path, "-vf", f"ass={ass_esc}",
           "-c:v", "libx264", "-preset", "fast", "-crf", "22",
           "-c:a", "copy", "-movflags", "+faststart", output_path]
    return run_ff(cmd)


# ── Dropbox upload (refresh-token OAuth) ──────────────────
def dbx_get_access_token(app_key, app_secret, refresh_token):
    r = requests.post(
        "https://api.dropbox.com/oauth2/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token,
              "client_id": app_key, "client_secret": app_secret},
        timeout=20)
    data = r.json()
    token = data.get("access_token")
    if not token:
        fail(f"Dropbox auth failed: {data.get('error_description', data.get('error'))}")
    return token


def dbx_list_names(token, folder):
    try:
        r = requests.post(f"{DBX_API}/files/list_folder",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"path": folder, "recursive": False}, timeout=20)
        return {e["name"] for e in r.json().get("entries", [])} if r.status_code == 200 else set()
    except Exception:
        return set()


def unique_name(filename, existing):
    if filename not in existing:
        return filename
    stem, ext = Path(filename).stem, Path(filename).suffix
    i = 1
    while f"{stem}_{i}{ext}" in existing:
        i += 1
    return f"{stem}_{i}{ext}"


def dbx_upload(token, file_path, folder, filename):
    existing = dbx_list_names(token, folder)
    final_name = unique_name(filename, existing)
    dest = (folder.rstrip("/") + "/" + final_name)
    size = os.path.getsize(file_path)
    CHUNK = 148 * 1024 * 1024

    if size <= CHUNK:
        with open(file_path, "rb") as f:
            r = requests.post(f"{DBX_CONTENT}/files/upload",
                headers={"Authorization": f"Bearer {token}",
                         "Dropbox-API-Arg": json.dumps({"path": dest, "mode": "add", "autorename": False}),
                         "Content-Type": "application/octet-stream"},
                data=f, timeout=900)
        if r.status_code != 200:
            fail(f"Dropbox upload error {r.status_code}: {r.text[:300]}")
    else:
        with open(file_path, "rb") as f:
            chunk = f.read(CHUNK)
            r = requests.post(f"{DBX_CONTENT}/files/upload_session/start",
                headers={"Authorization": f"Bearer {token}", "Dropbox-API-Arg": '{"close":false}',
                         "Content-Type": "application/octet-stream"},
                data=chunk, timeout=900)
            if r.status_code != 200:
                fail(f"Dropbox session start error: {r.text[:300]}")
            sid = r.json()["session_id"]
            offset = len(chunk)
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                requests.post(f"{DBX_CONTENT}/files/upload_session/append_v2",
                    headers={"Authorization": f"Bearer {token}",
                             "Dropbox-API-Arg": json.dumps({"cursor": {"session_id": sid, "offset": offset}, "close": False}),
                             "Content-Type": "application/octet-stream"},
                    data=chunk, timeout=900)
                offset += len(chunk)
            r = requests.post(f"{DBX_CONTENT}/files/upload_session/finish",
                headers={"Authorization": f"Bearer {token}",
                         "Dropbox-API-Arg": json.dumps({"cursor": {"session_id": sid, "offset": offset},
                                                        "commit": {"path": dest, "mode": "add"}}),
                         "Content-Type": "application/octet-stream"},
                data=b"", timeout=900)
            if r.status_code != 200:
                fail(f"Dropbox session finish error: {r.text[:300]}")

    r2 = requests.post(f"{DBX_API}/sharing/create_shared_link_with_settings",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"path": dest, "settings": {"requested_visibility": "public"}}, timeout=20)
    url = None
    if r2.status_code == 200:
        url = r2.json().get("url", "")
    elif r2.status_code == 409:
        url = (r2.json().get("error", {}).get("shared_link_already_exists", {})
               .get("metadata", {}).get("url", ""))
    if url:
        url = url.replace("www.dropbox.com", "dl.dropboxusercontent.com").replace("?dl=0", "?dl=1")
    return final_name, url


# ── main ───────────────────────────────────────────────────
def main():
    media_raw = os.environ.get("MEDIA_URLS", "")
    script_text = os.environ.get("SCRIPT_TEXT", "").strip()
    voice = os.environ.get("VOICE", "en-US-JennyNeural")
    rate = os.environ.get("RATE", "-15%")
    resolution = os.environ.get("RESOLUTION", "tiktok")
    motion = os.environ.get("MOTION", "kenburns")
    output_name = os.environ.get("OUTPUT_NAME", "marketing_video.mp4")
    dropbox_folder = os.environ.get("DROPBOX_FOLDER", "/Marketing")

    caption_enable = os.environ.get("CAPTION_ENABLE", "true").strip().lower() == "true"
    caption_style_key = os.environ.get("CAPTION_STYLE", "poh_gold")
    caption_position = os.environ.get("CAPTION_POSITION", "bottom")
    caption_words = os.environ.get("CAPTION_WORDS", "4")
    caption_font_size = os.environ.get("CAPTION_FONT_SIZE", "40")

    app_key = os.environ.get("DROPBOX_APP_KEY", "")
    app_secret = os.environ.get("DROPBOX_APP_SECRET", "")
    refresh_token = os.environ.get("DROPBOX_REFRESH_TOKEN", "")

    if not script_text:
        fail("SCRIPT_TEXT is empty.")
    if not (app_key and app_secret and refresh_token):
        fail("Missing Dropbox credentials (DROPBOX_APP_KEY / DROPBOX_APP_SECRET / DROPBOX_REFRESH_TOKEN).")

    raw_list = [x.strip() for x in re.split(r"[\n,]+", media_raw) if x.strip()]
    if not raw_list:
        fail("MEDIA_URLS is empty — provide at least one image or video URL.")

    with tempfile.TemporaryDirectory() as tmp_dir:
        log("Resolving media...")
        media_items = resolve_media_list(raw_list, tmp_dir)

        log(f"Generating voiceover with edge-tts voice={voice} rate={rate}...")
        voice_path = os.path.join(tmp_dir, "voice_raw.mp3")
        raw_srt_path = os.path.join(tmp_dir, "words_raw.srt")
        synth_voice_with_captions(script_text, voice, rate, voice_path, raw_srt_path)
        if not has_audio_stream(voice_path):
            fail("Generated voice file has no audio stream.")

        output_path = os.path.join(tmp_dir, output_name)
        out_w, out_h = build_final_video(media_items, voice_path, resolution, motion, output_path, tmp_dir)

        upload_path = output_path
        if caption_enable:
            log(f"Building captions (style={caption_style_key}, {caption_words} words/card)...")
            grouped_srt = os.path.join(tmp_dir, "captions.srt")
            if build_caption_srt(raw_srt_path, caption_words, grouped_srt):
                style = CAPTION_PRESETS.get(caption_style_key, CAPTION_PRESETS["poh_gold"])
                ass_path = os.path.join(tmp_dir, "captions.ass")
                srt_to_ass(grouped_srt, ass_path, style, caption_position,
                          caption_font_size, out_w, out_h)
                captioned_path = os.path.join(tmp_dir, f"captioned_{output_name}")
                ok, err = burn_captions(output_path, ass_path, captioned_path)
                if ok:
                    upload_path = captioned_path
                    log("Captions burned in successfully.")
                else:
                    log(f"WARNING: burning captions failed, uploading without captions: {err}")
            else:
                log("WARNING: no caption timing available, uploading without captions.")

        log("Authenticating with Dropbox...")
        token = dbx_get_access_token(app_key, app_secret, refresh_token)

        log(f"Uploading to Dropbox: {dropbox_folder}")
        final_name, share_url = dbx_upload(token, upload_path, dropbox_folder, output_name)

        log(f"Done. Uploaded as '{final_name}'.")
        if share_url:
            log(f"Shared link: {share_url}")
        else:
            log("Uploaded, but could not create a shared link (check app permissions).")

        # Surface key results in the GitHub Actions run summary.
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a") as f:
                f.write("## 🎬 Marketing Video Result\n\n")
                f.write(f"- **File:** `{final_name}`\n")
                f.write(f"- **Folder:** `{dropbox_folder}`\n")
                if share_url:
                    f.write(f"- **Link:** {share_url}\n")


if __name__ == "__main__":
    main()
