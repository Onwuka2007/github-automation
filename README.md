# Automated README Generator

An automated CI/CD documentation pipeline that leverages Google's Gemini AI to dynamically generate and maintain comprehensive, professional `README.md` documentation for your repositories.

---

## 🚀 Features

- **Automated Documentation**: Uses GitHub Actions workflows to build or update your README upon repository triggers.
- **AI-Powered Insights**: Analyzes your project structure, source code, and configuration files via the `google-genai` SDK to draft detailed technical overviews.
- **Smart Filtering**: Integrates with `.gitignore` and predefined ignore patterns to filter out irrelevant files (`node_modules`, `venv`, logs, build outputs, etc.) and focus on core source logic.
- **Priority File Analysis**: Prioritizes crucial setup files (`package.json`, `requirements.txt`, `Dockerfile`, etc.) to accurately infer the project tech stack and installation commands.

---

## 📂 Folder Structure

```text
.github/
├── scripts/
│   └── generate_readme.py     # Core Python script for directory tree traversal and AI prompt generation
└── workflows/
    ├── auto-readme.yml        # Main automation pipeline workflow
    └── generate-readme-reusable.yml # Reusable workflow for flexible integration
```

- **`.github/scripts/generate_readme.py`**: Crawls the target repository, parses files with path specifications, aggregates codebase snippets, and communicates with the Gemini API.
- **`.github/workflows/`**: Contains GitHub Actions configurations that orchestrate the execution of the README generation script.

---

## 🧰 Tech Stack

- **Language:** Python
- **Automation:** GitHub Actions (CI/CD)
- **AI/ML:** Google GenAI SDK (`google-genai`)
- **Utilities:** `pathspec` for gitwildmatch pattern parsing

---

## ⚙️ Getting Started & Installation

### Prerequisites

- Python 3.x
- A Google Gemini API Key configured in your GitHub repository secrets (e.g., `GEMINI_API_KEY`).

### Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/your-username/your-repo.git
   cd your-repo
   ```

2. Install the required dependencies:
   ```bash
   pip install google-genai pathspec
   ```

3. Run the generator script locally (ensure your API key is set in your environment):
   ```bash
   export GEMINI_API_KEY="your-api-key-here"
   python .github/scripts/generate_readme.py
   ```

---

## 📄 License & Usage

This project is open-source software provided under the standard MIT License. Feel free to adopt and configure the GitHub Actions workflows to automate documentation across your development lifecycle.