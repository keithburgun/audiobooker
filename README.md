# Audiobooker

Turn long PDFs (books) into audiobooks on your own Windows PC, using text-to-speech models that run locally on your NVIDIA GPU. Nothing is uploaded anywhere.

* **Engines**
  * **Kokoro-82M**: the default. It is very fast (a novel takes minutes on a GPU) and has 23 English voices (American and British).
  * **Chatterbox**: Resemble AI's 0.5B model. It is the most natural and expressive of the three, has sliders for expressiveness and pacing, and can clone a voice from a 10–20 s clip.
  * **Chatterbox Turbo**: a smaller, faster Chatterbox with the same cloning.
* **Output:** one MP3 per chapter (plus an .m3u playlist), a single MP3 with ID3 chapter markers, or an **M4B audiobook** with chapters and cover art.
* **PDF handling**
  * Chapters come from the PDF's bookmarks, or failing that from "Chapter …" headings.
  * Running headers and footers, page numbers, footnotes and footnote markers are stripped.
  * Words hyphenated across lines are re-joined.
  * **Scanned PDFs** are read with OCR (RapidOCR, which needs no extra install).
* **Resumable:** every finished chapter is cached. If you cancel or crash halfway through a 12-hour book, run it again and it picks up where it stopped. Exporting the same narration in a different format is instant.
* **Downloads everything itself:** Python, PyTorch with CUDA, and the voice models.

## Installing

Run `Audiobooker-Setup-x.y.z.exe`. It installs for your user only and doesn't need admin rights.

The first launch opens a setup window and downloads about 3–4 GB into `%LOCALAPPDATA%\Audiobooker`:

* a private Python 3.11
* PyTorch built for your GPU
* the libraries
* the Kokoro voice model

This only happens once. Chatterbox (≈3 GB) and Chatterbox Turbo (≈1.5 GB) download the first time you pick them.

If something goes wrong, use the Start-menu entry **Audiobooker (repair setup)**.

## Using it

1. **Open PDF…** (or drag a PDF onto the window).
2. Check the chapter list. Tick or untick chapters, double-click to rename them, and right-click to merge chapters.
3. Pick an engine and voice, then press **▶ Preview** to hear it read the selected chapter.
4. Choose the output format and press **Create audiobook**.

To use your own narrator voice, select a Chatterbox engine and press **Add your own voice clip…**. Use 10–20 seconds of clean speech with no music. Only clone voices you have permission to use.

## Building the installer

You need a Windows machine, or GitHub Actions (see below).

```powershell
# needs Python 3.11+ and Inno Setup 6
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# -> dist\Audiobooker-Setup-0.1.0.exe
```

Alternatively, push this folder to a GitHub repo and run the **Build Windows installer** workflow (Actions tab), or push a tag like `v0.1.0`. The workflow runs in two stages:

* It first runs the real first-time setup on a Windows runner. Then it narrates the test books with Kokoro and synthesizes a sentence with each Chatterbox engine. CI runners have no GPU, so this runs on the CPU.
* Only if that passes does it build the installer and attach it to the release.

## Running from source (for development)

```powershell
cd app
uv venv --python 3.11 .venv
uv pip install --python .venv torch torchaudio --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv -r requirements.txt
uv pip install --python .venv --no-deps chatterbox-tts==0.1.7
.venv\Scripts\python -m audiobooker                 # GUI
.venv\Scripts\python -m audiobooker cli book.pdf --engine kokoro --voice bm_george --format m4b
```

Set `AUDIOBOOKER_DEV=1` to add a "test tones" engine that exercises the whole pipeline without any model.

## Layout

```
app/audiobooker/   the application (GUI, PDF extraction, engines, encoding)
app/requirements.txt
packaging/         launcher (first-run setup), Inno Setup script, icon, build script
tests/             test-PDF generator and engine smoke test
```

## Notes

* Chatterbox adds Resemble AI's inaudible "Perth" watermark to its output, as the library does by default.
* Chatterbox has no native speed control, so speed changes for it are applied by time-stretching.
* Licenses:
  * Kokoro: Apache-2.0
  * Chatterbox: MIT
  * PyMuPDF: AGPL-3.0 (fine for personal use)
  * RapidOCR: Apache-2.0
