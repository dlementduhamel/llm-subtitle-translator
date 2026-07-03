# Local Subtitle Translator

A fully offline Python script that translates video subtitle tracks using a local LLM server via an OpenAI-compatible API (e.g. [LM Studio](https://lmstudio.ai/)). It extracts the source subtitle stream, translates it in chunks, converts it to ASS for better compatibility, and remuxes the result back into the original video file.

## Features

- **Fully local**: no data leaves your machine. The LLM runs on your own hardware.
- **OpenAI-compatible API**: works with LM Studio, Ollama, llama.cpp server, vLLM, etc.
- **Multiple containers**: supports `MKV`, `MP4` and `M4V`.
- **Configurable language pair**: translate from any source language to any target language by editing a few variables.
- **Target-language detection**: skips files that already contain a track in the target language.
- **Safe output handling**: either write files to a dedicated output directory or add a prefix next to the inputs.
- **Robust remuxing**: appends the translated subtitle track after video/audio, drops font attachments that can confuse VLC, and uses `ASS` for MKV and `mov_text` for MP4.

## Requirements

- Python 3.8+
- [ffmpeg](https://ffmpeg.org/download.html) and [ffprobe](https://ffmpeg.org/download.html) on your PATH
- `requests` Python package (`pip install requests`)
- A running local LLM server with an OpenAI-compatible `/v1/chat/completions` endpoint

## Quick start

1. Clone or download this repository.
2. Install the Python dependency:

   ```bash
   pip install requests
   ```

3. Start your local LLM server (for example with LM Studio) on `http://localhost:1234`.
4. Edit `subtitle_translator.py` and set the configuration variables at the top:

   ```python
   BASE_DIR = "/path/to/your/videos"
   TARGET_LANG = "fra"
   TARGET_TITLE = "Français"
   SOURCE_LANGS = ("eng", "en")
   ```

5. Run the script:

   ```bash
   python3 subtitle_translator.py
   ```

## Configuration

All user-configurable settings are at the top of `subtitle_translator.py`:

| Variable | Description | Example |
|----------|-------------|---------|
| `LM_STUDIO_URL` | URL of your local LLM server's chat completions endpoint. | `"http://localhost:1234/v1/chat/completions"` |
| `API_TOKEN` | Bearer token if your server requires authentication. Leave empty otherwise. | `""` or `"sk-..."` |
| `BASE_DIR` | Directory containing the source videos. | `"/home/user/Videos"` |
| `OUTPUT_DIR` | Optional output directory. If set, files keep their original names. | `"/home/user/Videos/French"` |
| `TEST_FILE` | Process a single file for testing. Leave empty for batch mode. | `""` or `"Episode 01.mkv"` |
| `CHUNK_ENTRIES` | Number of subtitle entries sent per LLM request. | `30` |
| `MAX_RETRIES` | Retries per chunk if the LLM returns malformed JSON. | `3` |
| `TARGET_LANG` | ISO 639-2/639-1 language code for the generated subtitle track. | `"fra"` |
| `TARGET_TITLE` | Human-readable title used in the prompt and metadata. | `"Français"` |
| `TARGET_PREFIX` | Prefix added to the output filename when `OUTPUT_DIR` is empty. | `"FR"` |
| `SOURCE_LANGS` | Tuple of source language codes to look for. | `("eng", "en")` |
| `SUBTITLE_OUTPUT_CODEC` | Codec for the generated subtitle track. MKV: `ass`; MP4: `mov_text`. | `"ass"` |

## Output modes

The script has two mutually exclusive ways to avoid overwriting your source files:

1. **Output directory (recommended)**

   ```python
   OUTPUT_DIR = "/path/to/output"
   TARGET_PREFIX = ""  # optional, ignored
   ```

   Output files keep their original names inside `OUTPUT_DIR`.

2. **Prefix next to source files**

   ```python
   OUTPUT_DIR = ""
   TARGET_PREFIX = "FR"
   ```

   Output files are written next to the inputs with the given prefix: `FR_Episode 01.mkv`.

If both `OUTPUT_DIR` and `TARGET_PREFIX` are empty, the script exits immediately to prevent overwriting the inputs.

## How it works

1. **Probe** the input file with `ffprobe` to detect subtitle streams.
2. **Skip** the file if a target-language subtitle already exists.
3. **Select** the first source-language subtitle track (fallback: any non-target track).
4. **Extract** that subtitle track to a temporary SRT file.
5. **Translate** the SRT in chunks using the local LLM, asking for a strict JSON array.
6. **Convert** the translated SRT to ASS for robust playback in VLC (MKV only).
7. **Remux** the original video/audio with the translated subtitle track appended at the end.

## Language pairs

You can translate any supported language pair. For example, to translate German videos to English:

```python
TARGET_LANG = "eng"
TARGET_TITLE = "English"
TARGET_PREFIX = "ENG"
SOURCE_LANGS = ("deu", "de", "ger")
```

For Japanese to French:

```python
TARGET_LANG = "fra"
TARGET_TITLE = "Français"
TARGET_PREFIX = "FR"
SOURCE_LANGS = ("jpn", "ja")
```

## Notes

- The script uses per-process temporary directories and cleans them up automatically.
- The LLM prompt is generated dynamically from `TARGET_TITLE`, so the same script works for any target language.
- The script assumes the source subtitle track can be extracted to SRT by `ffmpeg`. Most text-based subtitle formats (SRT, ASS, PGS via OCR) work; hardcoded bitmap subtitles are not supported.

## License

This project is released under the MIT License. You are free to use, modify and distribute it.

## Troubleshooting

**Error: "No source subtitle track found"**
- The input file has no subtitle stream matching `SOURCE_LANGS`. Check with `ffprobe -i file.mkv`.

**Error: "API error" or connection refused**
- Your LLM server is not running or `LM_STUDIO_URL` is wrong.

**Output has no subtitle in VLC**
- For MKV, the script uses ASS by default. If you still see issues, try converting the output manually or check that VLC is set to use the new subtitle track.

**VLC stops showing subtitles after an accented entry**
- This is exactly why the script converts the translated SRT to ASS before remuxing. If you changed `SUBTITLE_OUTPUT_CODEC` to `srt`, switch it back to `ass` for MKV.

## Author

Created by [Clément Duhamel](https://github.com/clement-duhamel) for local, offline subtitle translation workflows.
