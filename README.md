# Parakeet Dictate

Offline push-to-talk dictation for Windows. It uses the speech models Orca already downloaded
(**Parakeet TDT v2** by default; v3 and any newer model Orca adds show up in the tray menu automatically),
runs them with the same engine Orca uses (transcribe.cpp, on the graphics card when there is one), removes filler words ("uh", "um", "ahh", …) and types the text into whatever window has focus.
Nothing leaves the laptop, and no admin rights are needed.

## Use

| Action | How |
|---|---|
| Dictate (tap mode) | Press **Ctrl+Q**, speak, press **Ctrl+Q** again |
| Dictate (hold mode) | Hold **Ctrl+Q** while speaking, then let go |
| Cancel a recording | **Esc** (nothing is typed) |
| Retype the last result | Tray icon → *Copy last dictation*, then paste |
| Change settings | Tray icon → *Settings…* (applied on the next dictation) |

A small pill at the bottom of the screen you're working on shows **● Listening** (with a live mic
meter and timer), then **Transcribing…**, then **✓ Typed** (or *Cancelled* / *No speech heard*). It stays on
top of all windows, never takes focus and lets clicks pass through. Turn it off or move it to the top with
`[overlay]` in the settings.

**Transcribes while you talk.** Each time you pause for 0.8 s, the part before the pause is transcribed in
the background, so when you stop only the last bit is left and the text appears almost right away. Tested:
the split result is word-for-word the same as transcribing everything at once. Tune with
`[model] pause_seconds`, or turn it off with `process_while_speaking = false`.

The tray icon colour shows the state: **grey** idle (model not loaded) · **blue** idle (model loaded) ·
**red** listening · **amber** loading/transcribing · **dark red with !** problem (see the notification or *Open log*).

Ctrl+Q is taken over system-wide while the app runs, so other apps no longer receive it
(for example "quit" in some apps, or XON in terminals).

## What it does for accuracy

- **Same model and engine as Orca** (`%APPDATA%\orca\speech-models\…\*.gguf`, transcribe.cpp 0.4.1), read-only.
  Orca's folder is never modified.
- **Product names** come out split or misspelled ("key cloak", "KeyCloak", "Soft Sages", "Mailsey"). transcribe.cpp's
  Parakeet has no word boosting, so the `[dictionary]` fixes them (ported from Orca 1.4.256): each term also fixes
  the spacing, hyphens and capitals it is heard with ("Keycloak" fixes "key cloak" and "Key-Cloak",
  "useAudioCapture" fixes "use audio capture"), and `[dictionary.heard_as]` covers real mishearings
  ("mail sey" → **Mailzzy**). `[accuracy] hotwords` is passed to models that do support boosting.
  `[dictionary.apps."code.exe"]` adds terms for one program only (the window in front when you start
  dictating); they win over your own.
- **Phrases found by a voice detector**: Silero VAD (the 1 MB file Orca downloads, run on the CPU in the engine
  process) decides where each phrase starts and ends, keeping 0.4 s before it so first words aren't clipped.
  Same thresholds as Orca. Without it (`[model] vad = "off"`, not found, or it fails) a loudness detector is used.
- **Typed while you talk**: each phrase is typed as soon as it is transcribed; an amber dot on the pill means a
  phrase is still being written. Nothing is typed while Ctrl/Alt/Shift/Win is held (hold-to-talk), so letters
  never become shortcuts. `[output] type_while_speaking = false` types it all at the end instead.
- **Audio clean-up** before recognition: DC offset removed, silence trimmed, quiet speech raised (max 8×),
  and padding at the end so the last word isn't cut.
- **Deterministic text clean-up** with no AI rewriting: fillers removed as whole words (stretched spellings
  too, but "uh-huh", "umbrella" and a lone "M" are left alone), stutters like "the the" collapsed, then your exact
  replacements and the dictionary applied. The filler sounds follow the dictation language (Orca's lists for
  English, German, Spanish, French, Italian, Portuguese, Dutch, Russian, Japanese, Korean and Chinese); your
  `[cleanup] fillers` are removed on top.
- **Dictation language**: `[model] language` ("auto" or a code like "de") is passed to models that can be told
  the language, and picks the filler words.

## Resource use (measured on this laptop)

| State | Memory | CPU |
|---|---|---|
| Idle in the tray | ~2 MB working set, ~27 MB committed | 0 (waits on a Windows message) |
| While dictating | + ~700 MB in a separate model process (in graphics memory when on the GPU) | GPU: 16 s of speech in 0.3 s (RTX 2050). CPU: 4 threads, ~0.3 s per second of speech |
| 30 seconds after a dictation ends (typed or cancelled) | the model process exits, so all of that memory goes back to Windows | 0 |

`[model] device = "auto"` uses the NVIDIA card, then the built-in Intel graphics, then the CPU; whichever
fails falls back to the CPU. If the graphics card ever crashes the engine, `auto` remembers it
(`%LOCALAPPDATA%\ParakeetDictate\gpu-failed.json`) and uses the CPU from then on; delete that file, or set
`device = "gpu"`, to try the card again. Loading and each transcription give up after a minute. The very first GPU run on a PC compiles shaders once (~20 s); after that the
driver's cache makes it ~2 s to load. The model starts loading the moment you press the hotkey, so the load overlaps your speaking.
A new dictation within the 30 s reuses the loaded model and starts instantly. Change it with
`[model] unload_after_seconds` (0 = free right after each dictation, -1 = keep loaded).

## Files

- Settings: `%APPDATA%\ParakeetDictate\config.toml` (created from `parakeet_dictate\config.default.toml`)
- Log, sounds, icon, the chosen model (`model_path.txt`): `%LOCALAPPDATA%\ParakeetDictate\`. Recognised text is **not**
  logged unless `[output] log_transcripts = true`.

## Install

Run `build\installer\ParakeetDictate-Setup-<version>.exe`. It installs for the current user only (no admin)
into `%LOCALAPPDATA%\Programs\Parakeet Dictate`, adds a Start menu entry, can start with Windows, and has an
uninstaller in *Settings → Apps*. The app bundles its own Python, so the target PC needs nothing else.
Uninstalling keeps your settings (`%APPDATA%\ParakeetDictate`) and removes logs and any copied or downloaded model.

## Where the model comes from (`[model] dir = "auto"`)

Models are `.gguf` files. The app reads each file's header (architecture, name, languages) and lists every
speech-to-text model it finds under tray → *Model*, so models Orca adds later (Parakeet v3, Whisper,
SenseVoice, …) appear with no update. Voice-activity, diarization, language-ID and LLM `.gguf` files are skipped.
The same model found in two places is listed once. Your pick is remembered; with nothing picked yet, an
English-only model (Parakeet v2) is preferred. It looks in:

1. **Windows:** Orca (`%APPDATA%\orca\speech-models\…`), other apps' `models` / `speech-models` folders in
   AppData, and your user folder, Downloads, `models` and `.cache` (two folder levels deep).
2. **WSL:** every installed distribution (Docker/Rancher/Podman helper distros are skipped). Inside each one it checks
   `/home/*` and `/root`: Linux Orca (`~/.config/orca/speech-models/…`), `~/.local/share/*/models`, `~/.cache`,
   `~/models`, `~/Downloads`. A model found there is **copied to this PC once**, so dictation never has to start WSL
   or load ~700 MB over the network share.
3. **Nothing found:** tray menu → *Download Parakeet v2 model*. It fetches the same file Orca uses from
   Hugging Face (`handy-computer/parakeet-tdt-0.6b-v2-gguf`, ~730 MB) and checks its SHA-256.

When run on Linux itself (or inside WSL), the same finder searches the Linux home folder and the Windows user
folders under `/mnt/c/Users`. The dictation app itself (hotkey, typing, tray, indicator) is Windows-only.

Tray menu → *Find models again* re-runs the search. To force a model, set `[model] dir` to a `.gguf` file or its folder.

## Build the installer

```powershell
cd D:\Tools\parakeet-dictate
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
.\packaging\build.ps1        # needs Inno Setup 6; output: build\installer\ParakeetDictate-Setup-<version>.exe
```

Check a build without a microphone (finds the model, transcribes a WAV, writes JSON):
`ParakeetDictate.exe --selftest in.wav out.json`

Run from source instead: `.\.venv\Scripts\pythonw.exe -m parakeet_dictate`.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q                 # all, including the real model (~20 s)
.\.venv\Scripts\python.exe -m pytest -q -m "not slow"   # clean-up/config/audio only
```

The slow tests generate speech with Windows text-to-speech, run it through the real Parakeet v2 model
(on the GPU and on the CPU) and check that the fillers are gone and the product names are spelled correctly.
