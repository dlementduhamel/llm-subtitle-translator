#!/usr/bin/env python3
"""
Local subtitle translator using LM Studio (OpenAI-compatible API).
Extracts a source subtitle track, translates it chunk by chunk via a local LLM,
and remuxes the video with the translated subtitle track added.

Supports MKV, MP4 and M4V containers. Works fully offline with LM Studio.
"""

import os
import re
import json
import time
import tempfile
import subprocess
import requests
import shutil

# ==================== CONFIGURATION ====================
LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"
# Replace with your real API token if LM Studio requires authentication.
# Leave empty ("") if the server does not require a token.
API_TOKEN = ""
# ASE_DIR = "/path/to/your/videos"
BASE_DIR = "/path/to/your/videos"
# Optional output directory. Leave empty to write files next to the inputs in BASE_DIR.
# If set, files keep their original names (no prefix needed). Otherwise TARGET_PREFIX is required.
OUTPUT_DIR = ""
# Leave empty to process all video files in BASE_DIR (batch mode).
# Set to a single filename to process only that file (test mode).
TEST_FILE = ""

# Number of subtitle entries translated per LLM call.
CHUNK_ENTRIES = 30

# Number of retries when the API returns a malformed response.
MAX_RETRIES = 3

# Target language for the generated subtitle track (ISO 639-2 / 639-1 code).
# Examples: "fra" or "fre" (French), "eng" (English), "ita" (Italian), "deu" (German),
#           "spa" (Spanish), "por" (Portuguese), "pol" (Polish), "jpn" (Japanese).
TARGET_LANG = "fra"
# Additional language codes that should be considered equivalent to the target language
# (e.g. "fre" is ISO 639-2/B French). The first matched track is treated as a target track.
TARGET_LANG_ALIASES = ("fre",)
TARGET_TITLE = "Français"

# Prefix used for the output filename. Falls back to TARGET_LANG if not provided.
# Examples: "FR", "FRA", "ENG", "DE", "VF", etc.
TARGET_PREFIX = ""

# Source language(s) to look for in the input file (ISO 639-2 / 639-1 codes).
# The first matching subtitle track will be translated.
SOURCE_LANGS = ("eng", "en")

# Output subtitle codec depends on the container.
# MKV  : "ass" works best with VLC for accented characters.
# MP4  : "mov_text" is the only subtitle codec supported by MP4.
SUBTITLE_OUTPUT_CODEC = "ass"

# Set to False to drop the source subtitle track from the output file.
# Default is True to preserve existing subtitle tracks.
KEEP_ORIGINAL_SUBTITLES = True

# ==================== UTILITIES ====================

def run(cmd, check=True, capture=True, timeout=300):
    """Run a shell command and return the completed process."""
    kwargs = {
        "check": check,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    return subprocess.run(cmd, **kwargs)


def get_stream_info(input_file):
    """Return ffprobe JSON info for a media file."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", input_file,
    ]
    try:
        result = run(cmd, check=True)
        return json.loads(result.stdout)
    except Exception as e:
        print(f"[X] ffprobe error on {input_file}: {e}")
        return None


def normalize_path(path):
    """Return an absolute path."""
    return os.path.abspath(path)


def is_target_language(lang):
    """Return True if lang matches the target language or one of its aliases."""
    lang = lang.lower()
    return lang == TARGET_LANG or lang in TARGET_LANG_ALIASES


def has_target_language_subtitle(info):
    """Check if the file already contains a subtitle track in the target language."""
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "subtitle":
            tags = stream.get("tags", {})
            lang = tags.get("language", "").lower()
            if is_target_language(lang):
                return True
    return False


def find_source_subtitle_index(info):
    """Return the index of the first subtitle track in a source language."""
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "subtitle":
            tags = stream.get("tags", {})
            lang = tags.get("language", "").lower()
            if lang in SOURCE_LANGS:
                return stream["index"]
    # Fallback: first subtitle track that is not already in the target language
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "subtitle":
            tags = stream.get("tags", {})
            lang = tags.get("language", "").lower()
            if not is_target_language(lang):
                return stream["index"]
    return None


# ==================== SRT PARSING ====================

SRT_BLOCK_RE = re.compile(
    r"(\d+)\s+"
    r"(\d{1,2}):(\d{2}):(\d{2})[,\.](\d{3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,\.](\d{3})\s*\n"
    r"((?:.*\n)+?)(?=\n*\d+\s+\d{1,2}:\d{2}:\d{2}|\Z)",
    re.MULTILINE,
)


def clean_tags(text):
    """Remove HTML/ASS style tags such as <font>, <b>, <i>, <u>, {\\...}."""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\{[^}]*\}", "", text)
    return text


def parse_srt(text):
    """Parse an SRT file into a list of dicts {index, start, end, text}."""
    entries = []
    for m in SRT_BLOCK_RE.finditer(text + "\n\n"):
        idx = int(m.group(1))
        start = f"{m.group(2)}:{m.group(3)}:{m.group(4)},{m.group(5)}"
        end = f"{m.group(6)}:{m.group(7)}:{m.group(8)},{m.group(9)}"
        body = clean_tags(m.group(10).strip("\n"))
        entries.append({
            "index": idx,
            "start": start,
            "end": end,
            "text": body,
        })
    return entries


def format_srt(entries):
    """Rebuild an SRT file from a list of entries."""
    lines = []
    for i, e in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{e['start']} --> {e['end']}")
        lines.append(e["text"])
        lines.append("")
    return "\n".join(lines)


# ==================== TRANSLATION VIA LM STUDIO ====================

NEWLINE_PLACEHOLDER = " <<NEWLINE>> "
NEWLINE_PLACEHOLDER_RE = re.compile(r"\s*\<\<NEWLINE\>\>\s*")


def translate_chunk(entries):
    """
    Translate a chunk of subtitle entries using the local LLM.
    Returns a new list of entries, or None on failure.
    """
    # Replace real newlines with a placeholder so the JSON stays valid
    input_payload = json.dumps([
        {"index": e["index"], "text": e["text"].replace("\n", NEWLINE_PLACEHOLDER)}
        for e in entries
    ], ensure_ascii=False, indent=2)

    system_prompt = (
        "You are a professional subtitle translator. "
        f"You translate subtitles into {TARGET_TITLE}. "
        "Preserve meaning, tone, and natural dialogue. "
        "Return ONLY a valid JSON array. Do not include markdown, explanations, or code blocks. "
        "Each object must contain exactly two keys: 'index' (integer) and 'text' (translated string). "
        "Preserve the order of entries. Do not merge or split entries. "
        f"If a subtitle has multiple lines, keep them on a single line and use the placeholder '{NEWLINE_PLACEHOLDER}' to separate them. "
        "Do not write real newline characters inside the JSON string values."
    )

    user_prompt = (
        f"Translate the following subtitle entries into {TARGET_TITLE}. "
        "Return a JSON array with the same number of entries and matching 'index' values.\n\n"
        f"{input_payload}"
    )

    headers = {}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"

    payload = {
        "model": "local-model",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 8192,
    }

    try:
        resp = requests.post(LM_STUDIO_URL, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[!] API error: {e}")
        return None

    parsed = extract_json_array(content)
    if parsed is None:
        print(f"[!] Non-JSON response: {content[:300]}...")
        return None

    if len(parsed) != len(entries):
        print(f"[!] Entry count mismatch: expected {len(entries)}, got {len(parsed)}")
        return None

    for orig, trans in zip(entries, parsed):
        if not isinstance(trans, dict) or "text" not in trans:
            print(f"[!] Malformed entry: {trans}")
            return None
        if trans.get("index") != orig["index"]:
            print(f"[!] Index mismatch: {trans.get('index')} vs {orig['index']}")
            return None

    return [
        {
            "index": e["index"],
            "start": e["start"],
            "end": e["end"],
            "text": NEWLINE_PLACEHOLDER_RE.sub("\n", str(t.get("text", "")).strip()),
        }
        for e, t in zip(entries, parsed)
    ]


def extract_json_array(text):
    """Extract the first JSON array from a string."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end+1])
    except json.JSONDecodeError as e:
        print(f"[!] JSON parse error: {e}")
        return None


# ==================== REMUXING ====================

def get_container_extension(path):
    """Return the file extension in lowercase, without the leading dot."""
    return os.path.splitext(path)[1].lower().lstrip(".")


def remap_subtitles(info, new_sub_index=1, container_ext="mkv"):
    """
    Build the ffmpeg command to remux the video with the translated subtitle
    track appended at the end (after video/audio), which is more robust for VLC.
    new_sub_index = index of the translated subtitle file in ffmpeg inputs.
    container_ext = target container extension (mkv, mp4, etc.).
    """
    is_mp4 = container_ext in ("mp4", "m4v")
    # Font attachments from the original file can confuse VLC when a new subtitle
    # track is added. We drop them.
    keep_attachments = False

    cmd = ["ffmpeg", "-y", "-i", "__INPUT__", "-i", "__SUB__"]

    # 1. Identify the source subtitle track to drop (source language or,
    #    in fallback, the first track that is not the target language)
    src_idx = -1
    for s in info.get("streams", []):
        if s.get("codec_type") == "subtitle":
            tags = s.get("tags", {})
            lang = tags.get("language", "").lower()
            if lang in SOURCE_LANGS:
                src_idx = s["index"]
                break
    if src_idx == -1:
        for s in info.get("streams", []):
            if s.get("codec_type") == "subtitle":
                tags = s.get("tags", {})
                lang = tags.get("language", "").lower()
                if lang != TARGET_LANG:
                    src_idx = s["index"]
                    break

    # 2. Map original tracks (video, audio, subtitles, attachments) excluding
    #    the source track and any existing target-language track.
    for s in info.get("streams", []):
        idx = s["index"]
        if idx == src_idx and not KEEP_ORIGINAL_SUBTITLES:
            continue
        tags = s.get("tags", {})
        if s.get("codec_type") == "subtitle" and is_target_language(tags.get("language", "")):
            continue
        codec_type = s.get("codec_type")
        if codec_type in ("video", "audio", "subtitle"):
            cmd += ["-map", f"0:{idx}"]
        elif codec_type == "attachment" and keep_attachments:
            cmd += ["-map", f"0:{idx}"]

    # 3. Append the translated subtitle track at the end
    cmd += ["-map", f"{new_sub_index}:0"]

    # 4. Copy all streams by default
    cmd += ["-c", "copy"]

    # 5. The translated track is the last subtitle; compute its index among
    #    subtitle tracks that will actually be mapped (exclude source track if
    #    KEEP_ORIGINAL_SUBTITLES is False and skip any existing target track).
    num_subs = 0
    for s in info.get("streams", []):
        if s.get("codec_type") != "subtitle":
            continue
        idx = s["index"]
        if idx == src_idx and not KEEP_ORIGINAL_SUBTITLES:
            continue
        tags = s.get("tags", {})
        if is_target_language(tags.get("language", "")):
            continue
        num_subs += 1
    target_sub_idx = num_subs

    # 6. Metadata for the translated subtitle track
    cmd += [f"-metadata:s:s:{target_sub_idx}", f"language={TARGET_LANG}", f"-metadata:s:s:{target_sub_idx}", f"title={TARGET_TITLE}"]
    cmd += [f"-disposition:s:{target_sub_idx}", "default"]

    # 7. Subtitle codec depends on the container
    if is_mp4:
        cmd += [f"-c:s:{target_sub_idx}", "mov_text"]
    else:
        cmd += [f"-c:s:{target_sub_idx}", SUBTITLE_OUTPUT_CODEC]

    # 8. Output filename placeholder (replaced by caller)
    cmd += ["__OUTPUT__"]

    return cmd


# ==================== SINGLE FILE ====================

def process_file(input_path, output_path):
    print(f"\n>>> PROCESSING: {os.path.basename(input_path)}")

    if os.path.exists(output_path):
        print(f"[SKIP] Output file already exists: {output_path}")
        return False

    info = get_stream_info(input_path)
    if not info:
        return False

    if has_target_language_subtitle(info):
        print("[COPY] Target-language subtitle track already present.")
        shutil.copy2(input_path, output_path)
        print(f"[OK] Copied to {output_path}")
        return True

    src_index = find_source_subtitle_index(info)
    if src_index is None:
        print("[X] No source subtitle track found.")
        return False
    print(f"[*] Source subtitle track found: index {src_index}")

    # Use per-process temporary files to avoid conflicts with parallel runs
    tmpdir = tempfile.mkdtemp(prefix="subtrans_")
    temp_src = os.path.join(tmpdir, "source.srt")
    temp_translated = os.path.join(tmpdir, "translated.srt")
    temp_translated_ass = os.path.join(tmpdir, "translated.ass")

    try:
        # 1. Extract source subtitle to SRT
        print("[*] Extracting source subtitle to SRT...")
        try:
            run(["ffmpeg", "-y", "-i", input_path, "-map", f"0:{src_index}", "-c:s", "srt", temp_src], check=True)
        except Exception as e:
            print(f"[X] Extraction failed: {e}")
            return False

        with open(temp_src, "r", encoding="utf-8") as f:
            srt_text = f.read()

        entries = parse_srt(srt_text)
        if not entries:
            print("[X] No parseable SRT entries found.")
            return False
        print(f"[*] {len(entries)} SRT entries detected.")

        # 2. Translate by chunks
        translated_entries = []
        for i in range(0, len(entries), CHUNK_ENTRIES):
            chunk = entries[i:i+CHUNK_ENTRIES]
            print(f"    > Translating chunk {i//CHUNK_ENTRIES + 1}/{(len(entries)-1)//CHUNK_ENTRIES + 1} ({len(chunk)} entries)...")
            success = False
            for attempt in range(MAX_RETRIES + 1):
                res = translate_chunk(chunk)
                if res is not None:
                    translated_entries.extend(res)
                    success = True
                    break
                print(f"        [!] Retry {attempt+1}/{MAX_RETRIES}")
                time.sleep(2)
            if not success:
                print("[X] Translation failed, skipping file.")
                return False

        # 3. Write translated SRT
        with open(temp_translated, "w", encoding="utf-8") as f:
            f.write(format_srt(translated_entries))
        print(f"[*] Translated SRT generated: {len(translated_entries)} entries.")

        # 3b. Convert translated SRT to ASS for better VLC compatibility in MKV
        try:
            run(["ffmpeg", "-y", "-i", temp_translated, "-c:s", "ass", temp_translated_ass], check=True)
        except Exception as e:
            print(f"[X] SRT to ASS conversion failed: {e}")
            return False

        # 4. Remux to a temporary local file first, then copy to the final destination.
        # This avoids SMB/network I/O errors during ffmpeg muxing.
        container_ext = get_container_extension(output_path)
        print(f"[*] Remuxing ({container_ext})...")
        cmd = remap_subtitles(info, 1, container_ext)  # 1 = translated subtitle file (second ffmpeg input)
        cmd = [input_path if token == "__INPUT__" else token for token in cmd]
        cmd = [temp_translated_ass if token == "__SUB__" else token for token in cmd]

        temp_output = os.path.join(tmpdir, f"output.{container_ext}")
        cmd = [temp_output if token == "__OUTPUT__" else token for token in cmd]

        # Ensure no placeholders remain
        if any(isinstance(x, str) and x.startswith("__") for x in cmd):
            print(f"[X] Residual placeholder in ffmpeg command: {cmd}")
            return False

        try:
            run(cmd, check=True, timeout=600)
        except subprocess.CalledProcessError as e:
            print(f"[X] Remuxing failed: {e}")
            if e.stderr:
                print("--- ffmpeg STDERR ---")
                print(e.stderr[-1000:])
            return False
        except Exception as e:
            print(f"[X] Remuxing failed: {e}")
            return False

        # Copy the completed file to the final destination with retries.
        print(f"[*] Copying to {output_path}...")
        copied = False
        for attempt in range(3):
            try:
                shutil.copy2(temp_output, output_path)
                copied = True
                break
            except Exception as e:
                print(f"    [!] Copy attempt {attempt + 1}/3 failed: {e}")
                time.sleep(2)
        if not copied:
            print(f"[X] Failed to copy output to {output_path}")
            return False

        print(f"[SUCCESS] File created: {output_path}")
        return True

    finally:
        # Cleanup temporary files
        for tmp_path in (temp_src, temp_translated, temp_translated_ass):
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
        try:
            os.rmdir(tmpdir)
        except Exception:
            pass


# ==================== MAIN ====================

def build_output_path(input_path, filename):
    """Return the destination path based on OUTPUT_DIR and TARGET_PREFIX."""
    if OUTPUT_DIR:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        return os.path.join(OUTPUT_DIR, filename)
    prefix = (TARGET_PREFIX or TARGET_LANG).strip()
    if not prefix.endswith("_"):
        prefix += "_"
    return os.path.join(BASE_DIR, f"{prefix}{filename}")


def main():
    if not OUTPUT_DIR and not (TARGET_PREFIX or TARGET_LANG).strip():
        print("[X] Configuration error: set OUTPUT_DIR or TARGET_PREFIX to avoid overwriting input files.")
        return

    if OUTPUT_DIR:
        os.makedirs(OUTPUT_DIR, exist_ok=True)

    if TEST_FILE:
        input_path = os.path.join(BASE_DIR, TEST_FILE)
        output_path = build_output_path(input_path, TEST_FILE)
        if not os.path.exists(input_path):
            print(f"[X] Test file not found: {input_path}")
            return
        process_file(input_path, output_path)
        return

    # Batch mode: all MKV/MP4/M4V files not already processed
    files = [f for f in os.listdir(BASE_DIR) if f.lower().endswith((".mkv", ".mp4", ".m4v"))]
    files.sort()
    for filename in files:
        input_path = os.path.join(BASE_DIR, filename)
        output_path = build_output_path(input_path, filename)
        if os.path.exists(output_path):
            print(f"[SKIP] {filename} (already processed)")
            continue
        process_file(input_path, output_path)

    print("\n[DONE]")


if __name__ == "__main__":
    main()
