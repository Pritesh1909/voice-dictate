# Parakeet Dictate

Offline push-to-talk dictation for Windows. It uses the speech models Orca already downloaded
(**Parakeet TDT v2** by default; v3 and any newer model Orca adds show up in the tray menu automatically),
runs them with the same engine Orca uses (transcribe.cpp, on the graphics card when there is one), removes filler words ("uh", "um", "ahh", …) and types the text into whatever window has focus.
Your voice never leaves the laptop (only the optional *Learning in the background* can send short text, never audio, to Claude, and you can turn that off), and no admin rights are needed.

## Use

| Action | How |
|---|---|
| Dictate (tap mode) | Press **Ctrl+Q**, speak, press **Ctrl+Q** again |
| Dictate (hold mode) | Hold **Ctrl+Q** while speaking, then let go |
| Cancel a recording | **Esc** (nothing is typed) |
| Retype the last result | Tray icon → *Copy last dictation*, then paste — or set a shortcut for it (Settings → Shortcuts) |
| Fix a word for next time | Tray icon → *Correct last dictation…* (or its shortcut): fix the text, tick the spellings to learn, *Save* |
| Find an earlier dictation | Tray icon → *History…* (or its shortcut): search, then *Copy* or *Copy and close* |
| New line / empty line | Say **"new line"** or **"new paragraph"** on its own, between two pauses |
| Take back the last phrase | Say **"scratch that"** (or "delete that") on its own |
| Stop without the hotkey | Say **"stop listening"** on its own |
| Recognition failed | Tray icon → *Retry last recording* (or *Discard last recording*). The audio for a retry is kept in memory only; recordings kept on disk for learning are a separate, optional thing (see *Learning in the background*) |
| Send it straight away | Settings → General → Typing → *Press Enter after typing* |
| Change settings | Tray icon → *Settings…* (applied as soon as the window closes) |

The tray menu only holds what is used day to day: *Copy last dictation*, *Correct last dictation…*,
*History…* (hidden when the history is off), *Settings…* and *Quit* (with the shortcut shown next to an item that has one). *Retry* / *Discard last
recording*, *Done moving the pill* and *Download Parakeet v2 model* appear only when they apply.
Everything else is in the Settings window.

A small pill at the bottom of the screen you're working on shows **● Listening** (with a live mic
meter and timer), then **Transcribing…**, then **✓ Typed** (or *Cancelled* / *No speech heard*). It stays on
top of all windows, never takes focus and lets clicks pass through. Turn it off or move it to the top with
`[overlay]` in the settings. When the microphone clips it turns red and reads **Too loud**.

**Pill or button:** Settings → General → On-screen indicator → *Style*. *Pill* is the long one above.
*Button* is a small round mic: dark and ready, a red halo that grows with your voice while listening,
three dots while typing, a blue spinner while processing and a white tick when the text is in. Click it
to start dictating and again to stop; it never takes the focus, so the text goes where your cursor is.
*Show the button*: **Always** (it stays on screen, ready to click) or **Only while dictating**.
*Position*: top or bottom centre, or any corner, plus a distance from the edge; *Move…* overrides it.
The pill and the button each remember their own dragged spot.

**Put it anywhere:** Settings → General → On-screen indicator → *Move…* shows it with "Drag me · Enter to save · Esc to cancel".
Drag it with the left mouse button to any spot on any monitor, then press **Enter**, double-click it, or pick
*Done moving the pill* in the tray menu (Esc cancels). Only while you move it does the pill take the mouse
and keyboard; afterwards it is click-through and never-focused again, and the window you were in gets the
focus back. No dictation starts meanwhile: the hotkey just saves the position and ends moving. The spot is
kept in `%LOCALAPPDATA%\ParakeetDictate\pill_position.json` (the monitor's name and where on it, as a share
of the screen, so a resolution or scaling change keeps it in place) and the pill always stays fully on that
screen. With a saved spot the pill **stays on that monitor** instead of following the window you type into;
when that monitor isn't connected it goes back to its usual place. *Reset* next to it forgets it.

In tap mode a dictation stops by itself (and types what you said) after a minute without speech
(`[audio] silence_stop_seconds`, 0 = off). If the microphone is unplugged mid-dictation it stops the same way;
if the microphone named in `[audio] input_device` isn't there, the Windows default is used and a notification says so.

**Music pauses while you dictate.** Music or a video that is playing (Spotify, Amazon Music, a YouTube tab,
Media Player: anything the Windows volume flyout shows) is paused when a recording starts, so it isn't picked up
by the microphone, and played again as soon as the recording stops or is cancelled. Music you had paused yourself
stays paused. Off: Settings → General → Microphone → *Pause music while dictating* (`[audio] pause_media`).

**Transcribes while you talk.** Each time you pause for 0.8 s, the part before the pause is transcribed in
the background, so when you stop only the last bit is left and the text appears almost right away. Tested:
the split result is word-for-word the same as transcribing everything at once. Tune with
`[model] pause_seconds`, or turn it off with `process_while_speaking = false`.

The tray icon colour shows the state: **grey** idle (model not loaded) · **blue** idle (model loaded) ·
**red** listening · **amber** loading/transcribing · **dark red with !** problem (see the notification or *Open log*).

If the text can't be typed (no window has focus, or the window runs as administrator), it is put on the
clipboard instead and a notification says so; with typing while you talk, what was already typed stays.

Ctrl+Q is taken over system-wide while the app runs, so other apps no longer receive it
(for example "quit" in some apps, or XON in terminals).

### Settings window

Tray menu → **Settings…** (or double-click the tray icon) opens a Windows 11 style window (light or dark,
following Windows) with five pages, each made of titled groups:

| Page | Groups |
|---|---|
| **General** | Start-up (start with Windows, sounds) · Microphone (microphone, stop after silence, pause music) · Speech model (model, your model files and folders, runs on, language, *Find models*) · Typing · Pill (show, edge, distance, *Move the pill…*, *Reset*) |
| **Shortcuts** | Dictate · Copy last dictation · Correct last dictation · History · hold time · Esc cancels |
| **Improvements** | Dictionary (names and terms, "heard as", learned words) · Clean-up (fillers, repeated words, numbers, ticket prefixes) · Context words · Learning in the background (learn from my dictations, ask Claude, the automatic fixes with *Undo* and *Reset all*) · Recognition (voice detector, audio clean-up, preferred words) |
| **Extras** | Performance (memory, threads, pauses, longest dictation, *Free now*) · Spoken commands · Details · Privacy and files (log dictated text, keep a history, history length, keep recordings for learning and for how many days, *Delete all* / *Open folder* for the recordings, *Open log*, *Open settings file*) |
| **About** | Parakeet Dictate (version, speech engine, Python) · Files (data folder with *Open folder* / *Open log*) · Credits |

- **Change…** next to a shortcut listens for the next key combination: Ctrl/Alt/Shift/Win plus one key
  (F-keys may be used alone). Esc cancels; Backspace goes back to Ctrl+Q for Dictate and removes the
  shortcut for the others. Combinations Windows keeps for itself (Alt+Tab, Win+L, ...), everyday ones
  (Ctrl+C, Ctrl+V, ...), one already used by another of these shortcuts, and one another program holds
  ("in use by another app") are refused. In the file: `[hotkey] keys`, `copy_last`, `correct_last`, `history`.
- **Save** (enabled once something changed) writes only the settings you changed into `config.toml`;
  every comment and every other line stays as it was. The result is checked first: if it isn't valid, the
  problem is shown and nothing is written. The tray app picks the new settings up as soon as the window
  closes and says "Settings saved."
- Buttons that need the tray app (*Move the pill…*, *Reset*, *Find models*, *Free now*) are carried out
  when the window closes (*Move the pill…* saves and closes it straight away).
- Per-program settings (`[apps."program.exe"]`, `[dictionary.apps."program.exe"]`) and exact replacements
  stay in the file: Extras → *Open settings file*.

The window is its own short-lived process (`ParakeetDictate.exe --settings`, or
`python -m parakeet_dictate --settings` from source), like the correction window.

### Spoken commands

A command only counts when the whole phrase (what you say between two pauses) is just the command, so
"I added a new line to the file" is typed as said. Capitals and punctuation don't matter. Change, add or
remove phrases in `[commands.phrases]` (what each does: `newline`, `paragraph`, `undo`, `stop`), or turn
them off with `[commands] enabled = false`.

- **"new line"** types a line break, **"new paragraph"** two. The space typed after the previous phrase is
  taken back first so lines don't end in a space (only when typing, not pasting).
- **"scratch that"** removes the phrase typed just before it, with Backspace. The app knows exactly what it
  typed, so it presses Backspace exactly that many times, but only with `method = "type"` and only if the
  window you started dictating into is still in front; when pasting, or in another window, nothing is
  removed. Not yet typed (Ctrl still held, or typing everything at the end) means it is simply dropped.
  **Limit:** editors that add characters by themselves while you type (auto-closing brackets or quotes,
  auto-indent, auto-complete) change what is on screen, so the count can be off there.
- **"stop listening"** ends the dictation like the hotkey; it isn't typed.

### History

Tray → *History…* (or its shortcut, Settings → Shortcuts) lists recent dictations, newest first: time,
program it was typed into and the first line. Type in the search box to filter as you type; the selected
dictation's full text is shown below (select part of it and Ctrl+C, or use *Copy* / *Copy and close*,
Enter or a double-click). *Delete* (or the Delete key) removes one, *Clear all…* asks first and removes all.

Dictation can be sensitive, so where the history lives is a choice (Settings → Extras → Privacy and files,
or `[history] keep`):

| `keep` | What is kept |
|---|---|
| `"off"` | Nothing. The menu item is hidden. |
| `"session"` (default) | The last `max_items` dictations in the tray app's memory only; gone when it quits. The History window gets them in a temporary file in `%LOCALAPPDATA%\ParakeetDictate` that it deletes as soon as it has read it. |
| `"disk"` | Also `%LOCALAPPDATA%\ParakeetDictate\history.jsonl`, so it survives restarts. Trimming and deleting rewrite it in one step. Switching away from `"disk"` deletes the file. |

`[history] max_items = 200` (10–5000): the oldest are dropped first. Only finished dictations are kept
(also a retried recording); cancelled, empty and failed ones are not. What the window deletes goes back to
the tray as a list of ids (no text) when it closes. The window is its own process
(`ParakeetDictate.exe --history <file>`), like the correction window.

### Learn from corrections

Tray → *Correct last dictation…* opens a small window with the last dictated text. Fix the words the
model got wrong and the window proposes dictionary entries, each with a checkbox: "key cloak" → **Keycloak**
needs only the spelling (spacing and capitals are matched anyway); "mail sey" → **Mailzzy** is saved as
"heard as". Punctuation, a capital at the start of a sentence, added or removed words, rewrites of more
than four words and swaps of one ordinary word for another ("their" → "there") are not proposed.
*Save* writes the ticked ones to `%APPDATA%\ParakeetDictate\learned.toml` (same layout as `[dictionary]`;
your `config.toml` is never rewritten). They apply from the next dictation, after your own `[dictionary]`.
The window runs as its own small process, so it can't disturb the tray app.

### Learning in the background

The model sometimes writes a name the same wrong way again and again ("MailT" for **Mailzzy**). The app
can find these by itself: it keeps your recent dictations, and while the PC is otherwise idle a bigger
speech model (Whisper Large v3 Turbo, "the teacher") listens to them again. Where the two models
disagree, a fix is worked out; once the same fix has shown up in **2 different recordings** it is used from
the next dictation on. It is on by default, never shows a pop-up and never gets in the way of dictating.

- **What is saved, and where.** These files contain what you said. Each finished dictation (typed, or put
  on the clipboard) is kept as a 16 kHz audio file plus a `.json` record next to it with the text as heard
  and as typed, in `%LOCALAPPDATA%\ParakeetDictate\recordings\*.json` (and `.wav`). They are deleted after
  **14 days** (`keep_days`), and if the folder grows past **1.5 GB** (`max_recordings_mb`) the oldest go
  first. On a PC where learning can never run (no discrete graphics card, or one that dictation has crashed)
  no recordings are kept. What the checker has looked at is in
  `%LOCALAPPDATA%\ParakeetDictate\learning\checked.jsonl`: the text of the last **5000** checked dictations
  (what Parakeet heard, what Whisper heard and the judged text), with no time limit. *Delete all* and turning
  learning off leave it alone; to remove it, delete that file.
- **Your voice never leaves the PC.** The audio is heard only by the two models on this PC. The only thing
  that can go out is **text**: when two transcripts disagree, both are sent to Claude (`claude -p`, the Claude
  Code you are already signed in to, no API key) to say which one is right. That is a few cents a day at most:
  one call settles up to 30 dictations, and there are at most **20 calls a day** (`claude_calls_per_day`).
  If Claude isn't installed (or *Ask Claude* is off), only fixes to names you already have (your dictionary,
  your learned words and names from your notes) are learned, and the other disagreements from that run are
  not looked at again. If Claude runs but can't answer (not signed in, a usage limit, offline, too slow), or
  the day's calls are used up, the dictations still waiting for Claude are left for later: the next day, or
  the next time the app starts.
- **When it runs.** Only when all of these are true: learning is on and Whisper is ready; no dictation is going
  on; there has been no keyboard or mouse input for **3 minutes** (`idle_minutes`); the PC is on mains power
  (not on battery); and the PC has a discrete graphics card (Whisper runs on it, never on the CPU, so
  dictation is never slowed down). The moment you press the dictation shortcut the check stops and Whisper
  is shut down, so dictation starts at once. Touching the mouse or keyboard, or unplugging the laptop,
  stops it within about a second too, even in the middle of a recording. If Whisper ever crashes, learning pauses until the app is
  restarted; it never switches dictation to the CPU.
- **Whisper.** It is one download of 886 MB, started the first time the app runs with learning on, and only on a
  PC with a discrete graphics card that dictation hasn't crashed. Turning learning off stops a download in
  progress; it carries on where it stopped when learning is turned on again. It is stored in `%LOCALAPPDATA%\ParakeetDictate\teacher\whisper-large-v3-turbo\`
  (checked against its SHA-256 before use) and never appears in the Model menu. If Orca has already
  downloaded the same Whisper build, that copy is reused instead and nothing is downloaded. Uninstalling
  removes ours.
- **What gets learned.** Only spelling fixes for names and terms ("MailT" → **Mailzzy**), the same kind as in
  *Learn from corrections*; punctuation, rewording and swaps of ordinary words are ignored, and so is a
  recording where Whisper only wrote nonsense on noise. The fixes are in
  `%APPDATA%\ParakeetDictate\learned_auto.toml`, which also remembers the ones you undid, so after a
  reinstall they are still listed and undone ones stay undone. Your `config.toml` and `learned.toml` are
  never touched, and when one of your entries and an automatic fix clash, yours wins.
- **See and undo them.** Settings → Improvements → **Learning in the background** shows a status line (for example
  "Waits until the PC is idle", "Checked 12 dictations, learned 1 fix", or "Up to date" when every saved
  dictation has been checked) and every automatic fix: what was
  heard, what it becomes, the day it was learned and how often it was seen. **Undo** removes one at once, and a
  fix you undo is **never learned again**. **Reset all** (it asks first) removes them all, and none of them is learned again either.
- **Turn it off.** Settings → Improvements → Learning in the background → *Learn from my dictations* off
  (`[learning] enabled = false`): nothing is checked, downloaded or saved from then on. To keep learning but
  never ask Claude, turn *Ask Claude to judge disagreements* off. To keep no audio at all, turn off Settings →
  Extras → Privacy and files → *Keep recordings for learning*; *Delete all* in the same group erases the
  ones already saved. Neither removes the checked text in `...\learning\checked.jsonl`: delete that file.

All the settings are in `[learning]`:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Check kept recordings in the background and learn fixes from them |
| `keep_recordings` | `true` | Keep each dictation's audio and record (needed for learning; `false` = no audio is kept) |
| `keep_days` | `14` | Delete kept recordings older than this (1 to 365) |
| `max_recordings_mb` | `1500` | Keep at most this much, the oldest deleted first (100 to 20000) |
| `idle_minutes` | `3` | Minutes without keyboard or mouse input before a check starts (1 to 120) |
| `use_claude` | `true` | Let Claude judge disagreements (text only, never audio); `false` = only fixes to names you already have (dictionary, learned words, names from your notes) are learned |
| `claude_calls_per_day` | `20` | At most this many Claude calls a day (0 to 200, 0 = never) |

### Context words

`[context]` spells code names the way you write them, from the window you dictate into (its title, such as
`McpAuthFilter.java - ss-core-service`, and its program name) and from your notes (`folders`, by default
`D:\Obsidian\Knowledgebase`; add more, e.g. `folders = ["D:\\Obsidian\\Knowledgebase", "D:\\work"]`, only
`.md` files are read). Notes are scanned in the background (about 1 s for 140 notes, then only changed notes
are read again, at most every `refresh_minutes`), never while you dictate. The found names are kept in
`%LOCALAPPDATA%\ParakeetDictate\context_terms.json`; no note text is copied.

Only the spelling, spacing and capitals of words you actually said change ("mcp auth filter" →
**McpAuthFilter**, "ss core service" → **ss-core-service**), and ordinary speech is protected:

- Only names that take two or more spoken words are picked up (CamelCase, joined with `-` `_` `.`, letters
  with digits like `V2`) and ALL-CAPS acronyms. Single plain words ("Docker") never are: put those in `[dictionary]`.
- An acronym that reads as ordinary words is dropped: `IAM` would turn "I am going" into "IAM going".
- A name made only of ordinary words (`userId`, `displayName`, lower-case compounds like `customer-facing`)
  is used only in the programs listed in `[context] code_apps` (IntelliJ, VS Code, Windows Terminal, Orca,
  DataGrip, PyCharm, Cursor), so "the user id field" stays as you said it in an email.
- A name with an unusual part (`ss-core-service`, `McpAuthFilter`, `SHA-256`) is used everywhere.

Your own `[dictionary]` comes first, then learned words, then window-title words, then note words.
`[dictionary.apps]` terms for the program in front win over all of them. Models that support word
boosting also get the note words that are safe everywhere (Parakeet doesn't support boosting).

### Settings per program

`[apps."<program>.exe"]` changes output settings for one program (the window in front when you start
dictating): `method`, `add_trailing_space`, `type_while_speaking`, `press_enter` and `commands`. Program
names match like `[dictionary.apps]` (capitals don't matter, ".exe" may be left out). For example:

```toml
[apps."windowsterminal.exe"]
commands = false
[apps."slack.exe"]
press_enter = true
```

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
- **Words stream in while you speak** (`[model] stream_while_speaking = true`): Parakeet reads a whole phrase
  at a time, so the phrase still being spoken is re-read about twice a second (≈0.1 s per read on the
  graphics card; read less often on the CPU) and each word is typed once two reads agree on it — about half
  a second to a second behind your voice. The newest words show in grey on the (wider) pill until they settle.
  A word that might still become part of a dictionary term ("key" before "cloak") waits for the next one, and a
  phrase that could be a spoken command ("scratch…") isn't streamed. At the pause the phrase is read once more
  in full; if that differs, the end is corrected with Backspace (type mode, same window only — otherwise the
  typed words stay and the rest is added). Measured on TTS speech: words landed every ~0.5 s and the final text
  was identical to phrase-at-a-time typing, with no corrections needed.
- **Real streaming with models that support it** (Parakeet Unified EN, which Orca downloads as
  "parakeet-unified-en-0.6b"; pick it in Settings → Speech model): on a graphics card the phrase being spoken
  is fed to the model in quarter-second pieces instead of being re-read, and the words it is sure of (or that
  two updates in a row agree on) are typed. The pause, the final full read, the corrections and the spoken
  commands work exactly as above, so the text you end up with is the same. Nothing to switch on: it is used
  whenever the chosen model can stream and runs on a graphics card. Parakeet v2 and v3 can't stream and keep
  re-reading; so does any model on the CPU, where streaming falls behind the voice (measured on an i7-1260P:
  32 s of speech took 34 s). If streaming fails or falls behind during a dictation (more than 0.6 s of model
  time per second of speech), that dictation goes back to re-reading without losing a word.
  Measured on an RTX 2050: words land ~0.45 s after they are said (steps of 0.56 s, 0.16 s look-ahead;
  the model's own setting is ~1.5 s), about as soon as re-reading with v2, for a fixed cost per piece
  instead of re-reading a phrase that keeps growing. Integrated graphics (Iris Xe) uses the model's own
  setting, which it keeps up with.
- **Audio clean-up** before recognition: DC offset removed, silence trimmed, quiet speech raised (max 8×),
  and padding at the end so the last word isn't cut.
- **Background-noise reduction** (`[accuracy] noise_reduction`, default `"auto"`): steady noise such as a fan,
  mains hum or air conditioning is turned down before each phrase is read (`parakeet_dictate/denoise.py`: a
  Wiener-style filter on the spectrum, noise estimated from the phrase itself, never more than 10 dB of
  suppression, 50 Hz hum and rumble under ~80 Hz turned down; ~16 ms per 5 s of audio on an i7-1260P).
  `"auto"` only cleans a phrase whose background is within 20 dB of the voice, so a normal microphone in a quiet
  room is never touched. Measured on 20 Windows-TTS sentences of developer talk mixed with synthetic noise
  (word error rate, lower is better):

  | Noise | SNR | Off | Auto |
  |---|---|---|---|
  | none | – | 0.8% | 0.8% |
  | fan + hum | 20 / 10 / 5 / 0 dB | 0.8 / 2.0 / 4.8 / **100%** | 0.8 / 1.6 / 2.0 / 1.6% |
  | keyboard clicks | 20 / 10 / 5 / 0 dB | 0.8 / 0.8 / 0.8 / 0.8% | 0.8 / 0.8 / 0.8 / 0.8% |
  | other voices (babble) | 20 / 10 / 5 / 0 dB | 1.2 / 0.0 / 4.8 / **98%** | 1.2 / 1.6 / 3.2 / 13% |

  Parakeet itself copes well with steady noise; what failed was the silence trimming, which under loud noise
  cut off the last words (5 dB) or threw the whole phrase away as "silence" (0 dB). Cleaning first fixes that.
  Keyboard clicks don't bother the model or the voice detector, and are left alone. Stronger settings
  (-15/-20 dB, over-subtraction) read worse, which is why the suppression is mild. The Silero voice detector
  ignored fan noise and clicks completely (no phrase started early, none was missed), so its audio is not
  cleaned; nearby voices are speech to it, with or without the clean-up.
- **Deterministic text clean-up** with no AI rewriting: fillers removed as whole words (stretched spellings
  too, but "uh-huh", "umbrella" and a lone "M" are left alone), stutters like "the the" collapsed, then your exact
  replacements and the dictionary applied. The filler sounds follow the dictation language (Orca's lists for
  English, German, Spanish, French, Italian, Portuguese, Dutch, Russian, Japanese, Korean and Chinese); your
  `[cleanup] fillers` are removed on top.
- **Numbers as digits** (`[cleanup] numbers`, English only). Measured with Windows text-to-speech through
  Parakeet v2: it already writes most numbers as digits ("25%", "version 2.5", "port 6040", "512MB", "SEC-12"
  from "SEC dash twelve"), but leaves some as words or writes them oddly. Only those clearly numeric cases
  are changed:

  | Said | Parakeet v2 wrote | Typed |
  |---|---|---|
  | three replicas / two instances | three replicas / two instances | 3 replicas / 2 instances |
  | five minutes / three hours | five minutes / three hours | 5 minutes / 3 hours |
  | Java twenty one | Java Twenty-One | Java 21 |
  | at ten thirty / at three fifteen PM | at 10.30 / at 3.15 p.m. | at 10:30 / at 3:15 p.m. |
  | minus five / negative ten | minus 5 / negative 10 | -5 / -10 |
  | SEC twelve / NET zero three | SEC12 / Net 03 | SEC-12 / NET-03 (`[cleanup] id_prefixes`) |
  | D one eight four | D one eight four | D-184 |

  The same rules cover the words when the model leaves them ("port six oh three zero", "two point five",
  "twenty five percent"). Prose stays: "one of the", "a couple", "for the four of us", "no one", "once",
  "one-to-one", "Two things…", "one second", "three times". While streaming, a word that may still become
  part of a number ("twenty" before "five", "SEC" before "twelve") waits for the next word.
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
- Learned words: `%APPDATA%\ParakeetDictate\learned.toml`; names found in your notes:
  `%LOCALAPPDATA%\ParakeetDictate\context_terms.json` (names, file times and one-way word hashes; no note text)
- Log, sounds, icon, the chosen model (`model_path.txt`), where you put the pill (`pill_position.json`):
  `%LOCALAPPDATA%\ParakeetDictate\`. Recognised text is **not** logged unless `[output] log_transcripts = true`.
- Dictation history: in memory only by default; `%LOCALAPPDATA%\ParakeetDictate\history.jsonl` only with
  `[history] keep = "disk"`.
- Learning in the background (see above): automatic fixes in `%APPDATA%\ParakeetDictate\learned_auto.toml`;
  kept recordings (audio, and the text as heard and typed) in `%LOCALAPPDATA%\ParakeetDictate\recordings\`;
  the text of the last 5000 checked dictations in `...\learning\checked.jsonl`;
  Whisper in `...\teacher\whisper-large-v3-turbo\`.

## Install

Run `build\installer\ParakeetDictate-Setup-<version>.exe`. It installs for the current user only (no admin)
into `%LOCALAPPDATA%\Programs\Parakeet Dictate`, adds a Start menu entry, can start with Windows, and has an
uninstaller in *Settings → Apps*. The app bundles its own Python, so the target PC needs nothing else.
Uninstalling keeps your settings (`%APPDATA%\ParakeetDictate`, including the automatic fixes) and removes logs, saved recordings and any copied or downloaded model (Whisper too).

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

**Your own models:** `[model] folders` lists more `.gguf` files or folders (searched two folder levels deep,
environment variables allowed). Their models are listed under *Model* as "your folder (…)" next to the ones found
automatically, also when `dir` is a fixed path (then `dir` is the one used at start, and a model picked in the menu
is used until the app restarts). A folder that is gone (a USB drive, say) is simply not listed. Tray →
*Add a model file…* opens the Windows file dialog: the file must be a speech-to-text model (a voice detector or
an LLM `.gguf` is refused with a notification); it is added to `folders` in your `config.toml` (only that line
changes, comments stay) and used right away. Your pick is remembered in `model_path.txt` like any other.

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
