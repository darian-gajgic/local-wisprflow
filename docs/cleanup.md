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
like "Sure, here is the corrected text:"). `gemma3:4b` stays faithful, preserves the spoken
language (it won't translate German → English), and cleans a dictated question instead of
answering it.

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

## Two failure modes this design fixes

| Symptom | Cause | Fix |
|---|---|---|
| Long dictation summarized/paraphrased; paragraph newlines outside NoteMode; "Sure, here is the corrected text:" leaked | a small 3B model breaks down on long input | `gemma3:4b` + temperature 0 + minimal-edit prompt |
| Short or instruction input answered/refused ("yes do that" → "please provide the dictated speech…"; "change the GPU delay" → "I am a large language model…") | bare-message framing → the model replies | `Input:`/`Output:` pattern-completion framing |

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

## Troubleshooting

- **Output is unpolished / a single line of raw speech.** Cleanup fell back to raw. Check the
  server: `systemctl --user status wf-cleanup-llm`; then the model:
  `OLLAMA_HOST=127.0.0.1:11435 ollama list` (should list `gemma3:4b`).
- **First dictation after a while is slow (~2–4 s).** `gemma3:4b` cold-loads into VRAM after
  the `llm_keep_alive` window expires; subsequent dictations are ~1 s.
- **It rewrote/answered instead of transcribing.** Confirm the daemon is on this version
  (`git log --oneline -1` should be at or after the pattern-completion commit) and restart it:
  `systemctl --user restart wf-daemon`.
