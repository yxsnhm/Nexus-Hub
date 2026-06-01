# 🧠 Nexus Hub — A Multi-Agent Debate Platform for Code Generation

![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Status](https://img.shields.io/badge/status-active-brightgreen.svg)

Nexus Hub is an **open‑source multi‑agent collaboration system** where multiple AI models debate, vote, and audit each other before generating code. Think of it as a **“model parliament”** that improves reliability and safety through structured argumentation.

---

## 📖 Table of Contents

- [Why Nexus Hub?](#-why-nexus-hub)
- [How It Works](#-how-it-works)
- [System Architecture](#-system-architecture)
- [Project Structure](#-project-structure)
- [Quick Start](#-quick-start)
- [Execution Modes](#-execution-modes)
- [Bring Your Own Model](#-bring-your-own-model)
- [Current Parliament Members](#-current-parliament-members)
- [Roadmap](#-roadmap)
- [Contributing](#-contributing)
- [License](#-license)

---

## ❓ Why Nexus Hub?

Single‑model code generation often suffers from **hallucinations**, **bias**, and **poor self‑verification**.  
Nexus Hub forces different LLMs to **argue, vote, and audit** each other before any code is written.  
This structured debate leads to more reliable, secure, and well‑reasoned output.

---

## ⚙️ How It Works

1. **User submits a task** via the web interface.  
2. **Architect AI** proposes a technical solution.  
3. **Debater AIs** (from different providers) critique the plan.  
4. **Voting round** – the majority decides which plan is accepted.  
5. **Writer AI** generates the final code based on the winning plan.  
6. **Security & code auditors** review the code (optional, extensible).  
7. **The final code is saved locally** – ready to use!

The whole process is **real‑time visible** in the frontend.

---

## 🧱 System Architecture
┌─────────────┐ ┌────────────────┐ ┌────────────────┐
│ Web UI │────▶│ Daemon │────▶│ SOLO API │
│ (FastAPI) │ │ (nexus_daemon) │ │ (Flask) │
└─────────────┘ └────────────────┘ └────────────────┘
│ │
▼ ▼
.nexus_task.json AI Models
.nexus_result.json (Qwen, DeepSeek, ...)
output/ Debate & Vote

text

- **Web Server** (`web_server.py`): Frontend + REST API  
- **Daemon** (`nexus_daemon.py`): Task watcher & result writer  
- **SOLO API** (`solo_api.py`): Core debate engine with role management and fallback logic  

Communication is currently file‑based (`.nexus_task.json` / `.nexus_result.json`).  
A **WebSocket‑based gateway** for external models is under development.

---

## 📂 Project Structure
D:\智联枢纽
├── .env.example # Template for API keys (no real secrets!)
├── .gitignore
├── README.md
├── start_nexus_services.bat # One‑click launcher (Windows)
├── web_server.py # FastAPI web interface (port 9000)
├── nexus_daemon.py # Task monitoring daemon
├── solo_api.py # Core debate & code generation engine
├── desktop_automation.py # Desktop automation module (experimental)
├── bridge_base.py # Legacy base class for model bridges
├── deepseek_bridge.py # DeepSeek API wrapper
├── qwen_bridge.py # Qwen API wrapper
├── gemini_bridge.py # Gemini API wrapper (placeholder)
├── claude_bridge.py # Claude API wrapper (placeholder)
├── coze_bridge.py # Coze API wrapper (experimental)
├── llama_bridge.py # Llama API wrapper (placeholder)
├── mistral_bridge.py # Mistral API wrapper (placeholder)
└── output/ # Generated code files (*.py)

text

> **Note:** The bridge files are legacy wrappers. The core engine (`solo_api.py`) now calls the model APIs directly for better control and fallback handling.

---

## 🚀 Quick Start

### Prerequisites
- Windows OS (Linux support planned)
- Python 3.10+
- API keys for at least one of: [DeepSeek](https://platform.deepseek.com/), [Qwen](https://dashscope.aliyun.com/)

### Installation

```bash
git clone https://github.com/yxsnhm/Nexus-Hub.git
cd Nexus-Hub
pip install -r requirements.txt   # or manually: flask fastapi uvicorn requests python-dotenv
Create a .env file from the template and add your real API keys:

bash
cp .env.example .env
# Edit .env with your keys
Launch
One‑click (Windows):
Double‑click start_nexus_services.bat – three terminal windows will open.

Manual (any OS):
Open three terminals and run:

bash
python solo_api.py        # Port 8765
python nexus_daemon.py    # Task watcher
python web_server.py      # Port 9000
Then open http://127.0.0.1:9000 in your browser.

🎮 Execution Modes
Mode	Trigger Keyword	Description
Debate	(default)	Full multi‑agent debate → vote → audit → code
Direct	直接执行	Skip debate, generate code immediately
Desktop Automation	桌面自动化 / 键盘 / 粘贴	AI generates code to simulate desktop actions
🤖 Bring Your Own Model
We designed Nexus Hub to be model‑agnostic. You can plug in your own LLM with just a few lines of configuration.

Open solo_api.py and locate the models dictionary.

Add your model entry:

python
"my_model": (API_KEY, "https://your.api.endpoint/v1/chat/completions", "your-model-name"),
Add it to the debater pool inside debate_and_generate():

python
if checks.get("my_model") == "✓ 正常":
    debaters.append(("my_model", models["my_model"]))
Restart SOLO API – your model is now a citizen of the parliament!

We are also developing a lightweight gateway that will allow external models to auto‑register via WebSocket – no code changes required on your side.

🧪 Current Parliament Members
Model	Provider	Role	Status
Qwen‑Plus	Alibaba Cloud	Architect, Writer, Debater	✅ Active
DeepSeek‑V4‑Pro	DeepSeek	Fallback Architect & Debater	✅ Active
DeepSeek‑V4‑Flash	DeepSeek	Fallback Writer	✅ Active
Coze (扣子)	ByteDance	Backup Writer	❌ Auth failed
Claude 3 Haiku	Anthropic	Code Auditor (planned)	❌ 403 Forbidden
Gemini 2.0 Flash	Google	Security Auditor (planned)	❌ Timeout
Models marked ❌ are due to API key or network issues – the system automatically skips them and falls back to available models.

🗺️ Roadmap
WebSocket Gateway – external models connect without modifying core code

Sub‑room Creation – models can create private rooms and invite others

Tournament‑style Debates – multi‑round elimination for complex tasks

Emergent Behavior Dashboard – visualize alliances, roles, and influence

Academic Benchmark Suite – standardised tasks for multi‑agent evaluation

Linux / macOS Support – cross‑platform launcher and path handling

🤝 Contributing
We welcome contributions from researchers and developers interested in multi‑agent systems.
Ways to contribute:

Add your model to the parliament and share your observations

Improve the debate protocol (e.g., weighted voting, rebuttal rounds)

Extend desktop automation with more real‑world actions

Report bugs & suggest features via GitHub Issues

Please read CONTRIBUTING.md (coming soon) for guidelines.

📄 License
This project is licensed under the MIT License – see the LICENSE file for details.



