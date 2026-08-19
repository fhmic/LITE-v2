##LITE AI

A real-time voice AI that can hear, see, understand, and control your computer — on any OS. Supports Windows, macOS, and Linux. Built on the Gemini Live API for native audio streaming, delivering zero subscriptions and total digital autonomy.

---

## 🚀 Capabilities

| Feature | Description |
|---|---|
| 🎙️ Real-time Voice | Ultra-low latency conversation in any language via Gemini Live API |
| 🖥️ System Control | Launch apps, adjust volume/brightness, WiFi, shortcuts, power — all by voice |
| 🧩 Autonomous Tasks | High-level planning for complex multi-step goals via agent mode |
| 👁️ Visual Awareness | Real-time screen capture and webcam vision piped into your main Gemini session |
| 🧠 Persistent Memory | Deeply remembers projects, preferences, and personal context across sessions |
| ⌨️ Hybrid Input | Seamlessly switch between keyboard typing and voice commands |
| 🌅 Morning Briefing | On first boot: greets you, reads the time, recaps yesterday, and fetches live news |
| 🔔 Proactive 2.0 | Time-aware, context-aware check-ins — knows the time of day, your projects, and what you've been discussing |
| 🗓️ Session Memory | Summarises each conversation and mentions it naturally next morning — consumed after use, never repeats |
| 👁️‍🗨️ Background Monitoring | User-configured topic watching — checks for new headlines once a day and alerts naturally |
| 📊 Hardware Monitoring | Continuous CPU, RAM, GPU and temperature telemetry with localized voice alerts |
| 🌤️ Weather Report | Live weather data for your city, personalized from memory |
| 🗺️ Dynamic Content Panel | Scrollable display layer beneath the HUD that renders web results, news, and search data |
| 🔍 Multi-Mode Web Search | `news` / `research` / `price` / `compare` / `search` — Gemini Grounded first, DDG fallback |
| ⏰ Smart Reminders | OS-native scheduled notifications (Windows Task Scheduler / macOS LaunchAgent / Linux systemd) |
| ✈️ Flight Finder | Live flight price and availability lookup |
| 🎮 Game Updater | Checks and triggers game updates on Steam and Epic Games on demand |
| 📂 File Processor | Read, summarize, and answer questions about local files |
| 💻 Code Helper | Inline code review, debugging, and generation |
| 🌐 Browser Control | Open URLs, navigate tabs, and interact with the browser by voice |
| 📨 Send Message | Compose and send messages through WhatsApp, Telegram, and more |
| 🎬 YouTube Control | Search, play, and control YouTube playback by voice |
| 🖱️ Desktop Control | Taskbar, window management, and desktop-level operations |
| 🧑‍💻 Silent Language Memory | Detects spoken language on first use — all future sessions adapt automatically |
| 📱 Remote Dashboard | Control the assistant from your phone via QR code pairing |
| ⚡ Auto-Start on Boot | Registers with the OS startup system (registry / LaunchAgent / .desktop) |
| 📋 Clipboard Intelligence | Copy any text → floating panel with Translate / Summarise / Explain / Fix |
| 🎨 Assistant Customization | Change the assistant name and your name from the UI — takes effect immediately |

---


🗓️ Session Memory

LITE saves a 1-2 sentence summary at the end of each session and references it once in the next briefing. After use, it's deleted, preventing memory clutter.

👁️‍🗨️ Background Monitoring

Ask LITE to track a topic and it checks daily for new headlines. Updates are reported only when something changes. Fully opt-in, with crypto, finance, and trading topics blocked. Duplicate headlines are never repeated.

🔔 Proactive System 2.0

LITE now adapts to:

Time of day
Your active projects
Monitored topics
Recent conversation context (last 8 turns)

It rotates between focus areas to avoid repetition and uses a 20-minute cooldown for fewer, more relevant check-ins.

👁️ Instant Vision Acknowledgment

When asked to view your screen or camera, LITE immediately confirms it's processing before delivering the analysis.

📰 Parallel News Search

Gemini Grounded Search and DuckDuckGo run simultaneously. The first valid result is used, eliminating delays from Gemini outages.

---

## 📋 Requirements

| Requirement | Details |
| --- | --- |
| **OS** | Windows 10/11, macOS, or Linux |
| **Python** | 3.11 or 3.12 |
| **Microphone** | Required for voice interaction |
| **API Key** | None required to get started — everything except live voice works with zero configuration (DuckDuckGo search/news, text tools). Add a Gemini key for voice; optionally add a Claude key, a Groq key, and/or a custom/local endpoint (`config/api_keys.json`, see `api_keys.example.json`) as automatic fallbacks if Gemini's quota runs out. |

---

## 🗂️ Project Structure

```

├── main.py                   # Core loop — Gemini Live session, audio I/O, tool dispatch
├── ui.py                     # PyQt6 HUD — waveform, log panel, interrupt button, camera feed
├── setup.py                  # First-run configuration wizard
├── actions/
│   ├── web_search.py         # DDG (no key needed) + optional Gemini/Claude/Groq/custom synthesis (news, research, price, compare)
│   ├── screen_processor.py   # Screen capture & webcam vision via Gemini Live
│   ├── background_monitor.py # User-configured topic watching — daily DDG check, no crypto
│   ├── proactive.py          # Proactive 2.0 — time/context/rotation-aware check-ins
│   ├── reminder.py           # OS-native scheduled notifications
│   ├── system_monitor.py     # CPU / RAM / GPU / temperature telemetry
│   ├── computer_settings.py  # Volume, brightness, WiFi, power
│   ├── computer_control.py   # Keyboard shortcuts, mouse, window management
│   ├── open_app.py           # Application launcher
│   ├── browser_control.py    # Web browser control
│   ├── file_controller.py    # File system operations
│   ├── file_processor.py     # Document reading and summarization
│   ├── send_message.py       # Messaging integration
│   ├── weather_report.py     # Live weather data
│   ├── flight_finder.py      # Flight search
│   ├── youtube_video.py      # YouTube playback control
│   ├── game_updater.py       # Game update management (Steam / Epic)
│   ├── code_helper.py        # Code review and generation
│   ├── dev_agent.py          # Developer task agent
│   ├── desktop.py            # Desktop and taskbar control
│   ├── self_maintain.py      # Scans/fixes bugs AND builds new features into LITE's own code (backed up + auto-rollback)
│   └── self_update.py        # Checks/pulls/clones LITE's own GitHub repo
├── memory/
│   ├── memory_manager.py     # Load/save long_term.json — sessions, monitors, identity
│   └── long_term.json        # Persistent store: identity, preferences, projects, sessions, monitors
├── core/
│   ├── ai_client.py          # Shared text-generation client — Gemini → Claude → Groq → custom/local fallback
│   ├── fallback_voice.py     # Degraded voice loop (offline STT + Claude/Groq/custom + TTS) when Gemini Live is down
│   ├── system_integration.py # Desktop shortcut, auto-start, .ico generation (OS-level, used by Settings)
│   ├── global_hotkey.py      # System-wide mute hotkey (works even when LITE isn't focused)
│   ├── stt.py                # Offline speech-to-text — Whisper / Vosk
│   ├── tts.py                # Text-to-speech — EdgeTTS / Kokoro (offline) / ElevenLabs
│   └── prompt.txt            # Assistant personality and tool-routing rules
├── hologram/                 # The 3D holographic HUD (Three.js), replacing the old QPainter UI
│   ├── index.html            # The whole HUD — layout, styling, Three.js scene, QWebChannel bridge JS
│   └── vendor/three/         # Three.js + addons, vendored locally (not loaded from a CDN) — see note below
└── config/
    ├── api_keys.json         # Your real keys/settings — gitignored, created locally, never committed
    └── api_keys.example.json # Schema/template — safe to commit, no real secrets
```

**About `ui.py` and the hologram HUD:** the app's window is a `QWebEngineView` rendering `hologram/index.html`, bridged to Python via `QWebChannel` (`ui.py`). It's served over a local HTTP server (`ui.py`'s `_HologramHTTPServer`, bound to `127.0.0.1` on a random free port) rather than loaded as a bare `file://` page — Chromium (which powers QtWebEngine) blocks ES module `import` statements when the page origin is `file://`, which silently breaks the entire scene. Three.js itself is vendored locally under `hologram/vendor/three/` rather than pulled from a CDN, so the HUD renders with zero external network dependency.
