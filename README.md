# Humanise — Negotiation Training for Junior Lawyers

Humanise is an AI-powered negotiation simulator. Junior lawyers practise commercial negotiations (M&A, SPA, financing) against a realistic opposing counsel, then receive structured performance feedback. It runs as a web app or a command-line tool, with optional voice I/O.

---

## Prerequisites

- **Python 3.10 or later**
- **An Anthropic API key** (Claude) — or a Google Gemini API key
- **Windows** (run.bat is a Windows batch file; the Python code runs cross-platform)

---

## Setup

### 1. Create your config file

Copy `config.example.json` to `config.json` and fill in your API key:

```json
{
  "api_provider": "claude",
  "claude": {
    "api_key": "sk-ant-..."
  }
}
```

For Gemini, set `"api_provider": "gemini"` and add a `"gemini": { "api_key": "..." }` block instead.

### 2. Install packages

Run the batch file and choose **option 8** (Reinstall packages), or run directly:

```
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Key packages installed:

| Package | Purpose |
|---|---|
| `anthropic` / `google-generativeai` | LLM provider |
| `fastapi` + `uvicorn` | Web API server |
| `pypdf` | Contract PDF parsing |
| `faster-whisper` | Offline speech-to-text fallback |
| `edge-tts` | Offline text-to-speech fallback |
| `google-cloud-texttospeech` | High-quality TTS (optional, needs service account) |
| `pygame`, `sounddevice`, `numpy` | Voice I/O in CLI mode |

---

## Quickstart

Double-click **`run.bat`** to open the menu, then choose:

```
--- Humanise (practice) ---------------------------
  1.  Start session          (text mode)
  2.  Start session          (voice mode)
  3.  Start session + contract PDF

--- Web / Frontend --------------------------------
  16. Start API server       (opens browser automatically)
```

**Option 16 is the recommended starting point.** It launches the FastAPI server on `http://localhost:8000` and opens the web interface in your default browser.

### Web interface walkthrough

1. **Upload files** (optional) — drop in a scenario JSON, persona JSON, or a contract PDF. Defaults are pre-loaded for an M&A SPA negotiation.
2. **Parse contract** — if you uploaded a PDF, click "Parse contract" to extract key terms and update the contested points panel.
3. **Start negotiation** — opposing counsel opens. Respond by typing or using the mic button.
4. **🔇 Stop speech** — silences the AI audio mid-playback so you can start typing immediately.
5. **End session** — closes the session and generates a performance report with scores, strengths, and areas to improve.

---

## Input files

Default inputs live in the `inputs/` folder:

| File | Contents |
|---|---|
| `scenario.json` | Deal background, agreed points, contested issues |
| `persona.json` | Opposing counsel — name, role, tactics, redlines |
| `company_norms.json` | Your firm's negotiating policies and conduct rules |
| `playbook.json` | Scoring rubric used for the performance report |
| `past_learnings.json` | Weaknesses from previous sessions (auto-updated) |

You can swap any of these via the upload slots in the web UI without restarting the server.

---

## Voice (CLI)

Voice mode requires a working microphone. If you see `Error querying device`, run:

```
conda install -c conda-forge portaudio
```

Google Cloud TTS (higher quality) needs a service account key at `service_account_key.json`. Without it, Humanise falls back to Microsoft Edge TTS voices automatically.

---

## Security notes

- `config.json` and `service_account_key.json` are excluded from git (`.gitignore`).
- API keys are never logged or displayed.
- The `integrated/Legora/` folder is also excluded from version control.
