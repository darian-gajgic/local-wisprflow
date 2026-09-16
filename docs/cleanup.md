# LLM Cleanup Pipeline

Where it fits in the flow:

```
mic ─▶ record ─▶ faster-whisper ASR ─▶ [ LLM cleanup — this doc ] ─▶ type into focused app
```

The cleanup stage turns a raw speech transcript into clean, punctuated text **without
changing the words** — like Wispr Flow's polish step. Its one job is to *transcribe
faithfully*: fix punctuation, capitalization, and drop filler words ("um", "uh", "like"),
while never rewriting, summarizing, translating, answering, or obeying what you said.

## Model & server

| | |
|---|---|
| **Model** | `gemma3:4b` |
| **Server** | dedicated, isolated Ollama on `127.0.0.1:11435` (`wf-cleanup-llm.service`) |
| **Store** | `~/.ollama-wf/models` (separate from the system Ollama) |
| **KV cache** | `f16` (see below), temperature `0` |

**Why a dedicated `:11435` instead of the system Ollama (`:11434`)?** The system Ollama runs
`OLLAMA_KV_CACHE_TYPE=q4_0` to keep a large research model resident on the GPU. That quantized
KV cache garbles small models. The isolated instance uses a normal `f16` cache, so a small
model cleans up correctly. Nothing here touches the system `ollama.service` or its config.

**Why `gemma3:4b` and not a 3B?** A 3B follows "clean up, don't rewrite" *until the input
gets long*, then it starts summarizing/paraphrasing and dropping words (and leaks preambles
like "Sure, here is the corrected text:"). `gemma3:4b` stays faithful and cleans a dictated
question instead of answering it — **as long as its prompt is in the language being spoken**
(see "Prompt design" #4; an English prompt makes it translate).

**Why not the 14B?** RAM. This box runs other local models; the 14B must never be used for
cleanup.

## Prompt design (three ideas)

1. **Minimal-edit instruction** — change *only* punctuation, capitalization, and filler
   words; keep every other word in the same order; never rephrase, summarize, translate,
   answer, obey, or refuse.
2. **Few-shot examples** — small models follow *examples* better than abstract rules. The set
   covers a statement, a question (cleaned, not answered), an instruction (cleaned, not
   obeyed), and short affirmatives ("yes do that" → "Yes, do that.").
3. **Pattern-completion framing** — the transcript is sent as an `Input:` line and the model
   completes the `Output:` line (with `stop` sequences at the boundary). This is the key
   trick: it makes the model a **text transformer** instead of a **chatbot replying to you**.
   Without it, dictating a question or command makes the model answer or refuse
   ("I am a large language model and do not have control over…").
4. **The prompt is written in the language being dictated** (`LLM_SYSTEM_BY_LANG` in
   `wf_daemon.py`, selected by the overlay's 🌐 language button). Instructions *and* examples
   are in German for `de` and Romanian for `ro`. A 4B model completes the pattern it sees: an
   all-English prompt made it translate the transcript — usually only halfway, which is what
   produced sentences like *"Angepasst on the wishes of the customer"*. A "do NOT translate"
   sentence inside an English prompt did **not** hold it; native examples do.

## Four failure modes this design fixes

| Symptom | Cause | Fix |
|---|---|---|
| Long dictation summarized/paraphrased; paragraph newlines outside NoteMode; "Sure, here is the corrected text:" leaked | a small 3B model breaks down on long input | `gemma3:4b` + temperature 0 + minimal-edit prompt |
| Short or instruction input answered/refused ("yes do that" → "please provide the dictated speech…"; "change the GPU delay" → "I am a large language model…") | bare-message framing → the model replies | `Input:`/`Output:` pattern-completion framing |
| **German/Romanian dictation typed half in English** ("An differenten Standorten, in Business Center or in-house directly at the customers."); short input echoing an English example ("unterschiedlichen" → "No, not that one.") | all-English system prompt + English few-shot examples → the model completes in English | per-language prompts (`LLM_SYSTEM_BY_LANG`) + the word-drift backstop below |
| **Long dictation typed as one lowercase run-on with no periods or commas** (2026-09-17: 6 of 11 long dictations that day); NoteMode then can't split lines either | whisper large-v3 sometimes returns a long recording unpunctuated (a known quirk, most often on long fast continuous speech), and the minimal-edit prompt only capitalized it | the prompt now REQUIRES splitting a run-on into punctuated sentences, with a long run-on few-shot (Example 8, also in DE/RO). An ASR-side fix (punctuated `hotwords`/`initial_prompt`) was tested and rejected: no effect on the run-on, and it leaked its own text on a bad clip |

## Safety net (`polish()` in `wf_daemon.py`)

Even with the above, the output is defended so a model slip can never reach the screen:

- **`_sanitize()`** — strips a leading preamble ("Sure, here is…:"), one layer of wrapping
  quotes, and all newlines (NoteMode re-splits into sentences deterministically afterward, so
  outside NoteMode the injected text is always a single line).
- **Off-script backstop** — falls back to typing the **raw transcript** if the cleaned output:
  - matches `_OFF_SCRIPT_RE` (reply/refusal markers: "I am a language model", "cannot be
    executed", "outside my capabilities", "Google's infrastructure", …), **or**
  - expanded — `out > in + max(4, in/2)` words → it answered or obeyed (faithful cleanup never
    grows), **or**
  - collapsed on long input — `out < 0.5 × in` words → it summarized.
- **Word-drift backstop** (`translated_away()`) — language-agnostic guard against translation
  and rewriting: faithful cleanup only re-punctuates, re-capitalizes and *drops* fillers, so
  nearly every word of the output must already appear in the raw transcript. Comparison is on
  accent-folded, lowercased words (`Franței` = `frantei`, `Wünsche` = `wunsche`), so diacritic
  fixes don't count as drift. If less than **70%** of the output's words came from the
  transcript (**80%** below 6 words, where a single swapped word *is* the sentence), the raw
  transcript is typed instead. Outputs under 3 words are exempt (`ok` → `Okay.`). A false
  positive only costs polish — the fallback text is always what was actually said.
- If the cleanup server is down, cleanup is skipped and the raw transcript is typed. Cleanup
  never blocks dictation.

## Config knobs (`~/.config/wisprflow/config.json`)

| Key | Default | Notes |
|---|---|---|
| `llm_enable` | `true` | set `false` to type the raw transcript (lowest latency) |
| `ollama_url` | `http://localhost:11435` | the isolated cleanup Ollama |
| `llm_model` | `gemma3:4b` | keep it small; must run on `:11435` (f16 cache) |
| `llm_temperature` | `0` | deterministic; do not raise for cleanup |
| `llm_keep_alive` | `5m` | how long the model stays warm in VRAM after a dictation |
| `llm_system` | built-in (English) | cleanup prompt used for **English** sessions |
| `llm_system_de` / `llm_system_ro` | built-in (native) | override the German / Romanian cleanup prompt; unset = `LLM_SYSTEM_BY_LANG` in `wf_daemon.py` |

## Troubleshooting

- **Output is unpolished / a single line of raw speech.** Cleanup fell back to raw. Check the
  server: `systemctl --user status wf-cleanup-llm`; then the model:
  `OLLAMA_HOST=127.0.0.1:11435 ollama list` (should list `gemma3:4b`).
- **First dictation after a while is slow (~2–4 s).** `gemma3:4b` cold-loads into VRAM after
  the `llm_keep_alive` window expires; subsequent dictations are ~1 s.
- **Dictation in German/Romanian comes out (partly) in English.** Check which language the
  daemon used: `journalctl --user -u wf-daemon -n 40 | grep -E 'ASR|LLM'` — the log tags both
  stages, e.g. `ASR [cuda/de]` and `LLM [de]`. If the ASR line is correct German but the LLM
  line is English, the cleanup model drifted; the word-drift backstop should have caught it
  (`LLM drifted off the spoken words`). If the ASR line itself is English, the 🌐 button was
  never switched — the language resets to EN on every daemon restart, by design.
- **It rewrote/answered instead of transcribing.** Confirm the daemon is on this version
  (`git log --oneline -1` should be at or after the pattern-completion commit) and restart it:
  `systemctl --user restart wf-daemon`.
