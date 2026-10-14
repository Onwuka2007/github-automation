"""
Automated AI README Generator
-----------------------------
Scans a target project directory, respects .gitignore rules, builds a file tree,
gathers source context under a size budget, extracts verifiable facts in code
(Python version, console commands, license file), and asks a Gemini model to
write a README.md with a Mermaid architecture diagram.

Facts that can be computed deterministically (the tree, versions, commands,
license presence) are computed here and handed to the model as ground truth
instead of being left for it to infer.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time

import pathspec
from dotenv import load_dotenv
from google import genai
from google.genai import errors

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10 and earlier
    tomllib = None

# Load local .env variables if present
load_dotenv(".env.local")
load_dotenv()

# Sentinel written at the top of generated READMEs. A README without it is
# treated as hand-written and is not overwritten unless --force is passed.
SENTINEL_MARKER = "<!-- AUTO-GENERATED-AI-README -->"

# Model chain: first entry is tried first, the rest are fallbacks.
MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
]

MAX_ATTEMPTS_PER_MODEL = 3
BASE_BACKOFF = 2  # seconds; wait = BASE_BACKOFF ** attempt + jitter

MAX_FILE_CHARS = 3000        # per-file truncation
MAX_CONTEXT_CHARS = 60_000   # total budget for source context sent to the API

# Default patterns ignored in addition to the target's .gitignore.
IGNORE_PATTERNS = [
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "env",
    "dist",
    "build",
    ".next",
    "__pycache__",
    ".pytest_cache",
    ".DS_Store",
    "package-lock.json",
    "yarn.lock",
    "*.egg-info",
    "*.pyc",
    # Secrets: never send these to an external API.
    ".env",
    ".env.*",
    "!.env.example",
    "*.pem",
    "*.key",
]

# Manifest files placed first in the context so the budget cuts source, not config.
PRIORITY_FILES = {
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "Dockerfile",
    "README.md",
    ".env.example"
}
LICENSE_PREFIXES = ("LICENSE", "LICENCE", "COPYING")
SOURCE_EXTENSIONS = (
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".toml",
)


# Ignore handling and scanning

def load_gitignore(root_dir: str) -> pathspec.PathSpec:
    """Combine default ignore patterns with the project's .gitignore rules."""
    ignore_list = list(IGNORE_PATTERNS)
    gitignore_path = os.path.join(root_dir, ".gitignore")
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8", errors="ignore") as f:
            ignore_list.extend(f.read().splitlines())
    return pathspec.PathSpec.from_lines("gitwildmatch", ignore_list)


def is_ignored(spec: pathspec.PathSpec, rel_path: str, is_dir: bool) -> bool:
    """Match a path against the spec. Directories get a trailing slash so that
    gitignore rules like 'build/' apply to them."""
    return spec.match_file(rel_path + "/" if is_dir else rel_path)


def visible_entries(path: str, rel: str, spec: pathspec.PathSpec):
    """Return non-ignored entries of `path` as (DirEntry, rel_path, is_dir),
    directories first, then alphabetical."""
    try:
        entries = list(os.scandir(path))
    except OSError:
        return []
    entries.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    result = []
    for entry in entries:
        rel_path = f"{rel}/{entry.name}" if rel else entry.name
        is_dir = entry.is_dir(follow_symlinks=False)
        if is_ignored(spec, rel_path, is_dir):
            continue
        result.append((entry, rel_path, is_dir))
    return result


def generate_tree(start_dir: str, spec: pathspec.PathSpec) -> str:
    """ASCII tree of the project, using the same ignore logic as the context scan."""
    lines: list[str] = []

    def walk(path: str, prefix: str, rel: str) -> None:
        entries = visible_entries(path, rel, spec)
        for i, (entry, rel_path, is_dir) in enumerate(entries):
            last = i == len(entries) - 1
            connector = "└── " if last else "├── "
            lines.append(f"{prefix}{connector}{entry.name}{'/' if is_dir else ''}")
            if is_dir:
                walk(entry.path, prefix + ("    " if last else "│   "), rel_path)

    walk(start_dir, "", "")
    return "\n".join(lines)


def _is_priority(name: str) -> bool:
    return name in PRIORITY_FILES or name.upper().startswith(LICENSE_PREFIXES)


def collect_files(root_dir: str, spec: pathspec.PathSpec) -> list[tuple[str, str]]:
    """Return (rel_path, abs_path) for files worth sending, ordered so manifests
    come first, then shallow files before deep ones."""
    found: list[tuple[int, int, str, str]] = []
    stack = [(root_dir, "")]
    while stack:
        path, rel = stack.pop()
        for entry, rel_path, is_dir in visible_entries(path, rel, spec):
            if is_dir:
                stack.append((entry.path, rel_path))
            elif _is_priority(entry.name) or entry.name.endswith(SOURCE_EXTENSIONS):
                priority = 0 if _is_priority(entry.name) else 1
                found.append((priority, rel_path.count("/"), rel_path.lower(), rel_path))
    found.sort()
    return [(rel_path, os.path.join(root_dir, *rel_path.split("/"))) for *_, rel_path in found]


def gather_codebase_context(root_dir: str, spec: pathspec.PathSpec) -> tuple[str, list[str]]:
    """Read files under a total character budget. Returns (context, included_paths)."""
    chunks: list[str] = []
    included: list[str] = []
    total = 0
    omitted = 0

    for rel_path, abs_path in collect_files(root_dir, spec):
        try:
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError:
            continue

        # Don't feed a previous AI-generated README back in as "source".
        if os.path.basename(rel_path) == "README.md" and SENTINEL_MARKER in content[:500]:
            continue

        if len(content) > MAX_FILE_CHARS:
            content = content[:MAX_FILE_CHARS] + "\n... [truncated]"
        chunk = f"--- FILE: {rel_path} ---\n{content}\n"

        if total + len(chunk) > MAX_CONTEXT_CHARS:
            omitted += 1
            continue
        chunks.append(chunk)
        included.append(rel_path)
        total += len(chunk)

    if omitted:
        chunks.append(f"[Context budget reached: {omitted} file(s) omitted]\n")
    return "\n".join(chunks), included


# Deterministic facts

def detect_facts(root_dir: str) -> dict[str, str]:
    """Extract facts from manifests so the model doesn't have to guess them."""
    facts: dict[str, str] = {}

    pyproject = os.path.join(root_dir, "pyproject.toml")
    if os.path.isfile(pyproject):
        try:
            with open(pyproject, "r", encoding="utf-8") as f:
                text = f.read()
            if tomllib:
                project = tomllib.loads(text).get("project", {})
                if project.get("name"):
                    facts["Package name"] = str(project["name"])
                if project.get("requires-python"):
                    facts["Python requirement"] = str(project["requires-python"])
                if project.get("scripts"):
                    facts["Console commands"] = ", ".join(sorted(project["scripts"]))
                if project.get("dependencies"):
                    facts["Declared dependencies"] = ", ".join(project["dependencies"])
            else:
                m = re.search(r'requires-python\s*=\s*"([^"]+)"', text)
                if m:
                    facts["Python requirement"] = m.group(1)
                m = re.search(r'^\s*name\s*=\s*"([^"]+)"', text, re.MULTILINE)
                if m:
                    facts["Package name"] = m.group(1)
        except (OSError, ValueError):
            pass

    package_json = os.path.join(root_dir, "package.json")
    if os.path.isfile(package_json):
        try:
            with open(package_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("name"):
                facts.setdefault("Package name", str(data["name"]))
            bin_field = data.get("bin")
            if isinstance(bin_field, dict) and bin_field:
                facts.setdefault("Console commands", ", ".join(sorted(bin_field)))
            node = (data.get("engines") or {}).get("node")
            if node:
                facts["Node requirement"] = str(node)
        except (OSError, ValueError, AttributeError):
            pass

    try:
        licenses = sorted(
            n for n in os.listdir(root_dir)
            if n.upper().startswith(LICENSE_PREFIXES) and os.path.isfile(os.path.join(root_dir, n))
        )
    except OSError:
        licenses = []
    facts["License file"] = ", ".join(licenses) if licenses else "none"
    return facts

# Prompt and post-processing

def build_prompt(project_tree: str, codebase_context: str, facts: dict[str, str]) -> str:
    facts_block = "\n".join(f"- {k}: {v}" for k, v in facts.items())
    return f"""
You are a technical writer and software engineer. Analyze the project below and write an objective, accurate README.md.

### Verified facts (computed from project files; use exactly as given)
{facts_block}

### Project folder structure (computed from disk)
```text
{project_tree}
```

### Codebase context
{codebase_context}

### Rules
1. **Tone**: Direct, objective technical language. Avoid marketing filler and subjective adjectives (for example "intelligent", "comprehensive", "powerful", "robust", "seamless", "professional").
2. **Accuracy**: Document only behavior visible in the code. Do not invent flags, options, versions, commands, or files. Use version requirements and command names only from the verified facts or from the code itself. If something is unknown, omit it.
3. **Folder Structure section**: Copy the provided folder structure verbatim into a text code block. Do not modify, reorder, or annotate the tree lines. You may add a short bullet list below it describing key directories and files.
4. **Mermaid diagram**: Include one flowchart in a ```mermaid block. Every node label must be enclosed in double quotes, for example `F["User (--force flag)"]`. Edge labels must use the form `A -->|"label"| B`. Never use unquoted parentheses or special characters in labels.
5. **License**: Include a License section only if "License file" in the verified facts is not "none". Name the license type only if the license text is visible in the context.
6. **Privacy / data handling**: Include a short section only if the code shows that the project sends data to an external service. Describe exactly what the code sends and what the ignore rules exclude. State that secrets in files not matched by ignore rules would be sent. Never describe the project as local, offline, or private unless the code shows no data leaves the machine.
7. **Sections**: Project Overview, Features, System Architecture, Folder Structure, Tech Stack, Installation, Usage, plus the conditional sections above. For Usage, document the options and fallback behavior that appear in the code.

Output ONLY the markdown content of README.md, with no surrounding code fence and no conversational text.
"""


_MERMAID_BLOCK = re.compile(r"(```mermaid\s*\n)(.*?)(```)", re.DOTALL)
# NodeId[label] where label isn't already quoted and isn't a special shape like [(db)] or [[sub]]
_NODE_LABEL = re.compile(r'(\b[A-Za-z_][\w-]*)\[(?![\"\[\(/\\])([^\]\n]*)\]')
_EDGE_LABEL = re.compile(r'\|(?!")([^|\n"]+)\|')


def fix_mermaid(markdown: str) -> str:
    """Quote unquoted node and edge labels inside mermaid blocks. Unquoted
    parentheses in labels break rendering on GitHub."""
    def quote_node(m: re.Match) -> str:
        return f'{m.group(1)}["{m.group(2).strip()}"]'

    def quote_edge(m: re.Match) -> str:
        return f'|"{m.group(1).strip()}"|'

    def fix_block(m: re.Match) -> str:
        body = _NODE_LABEL.sub(quote_node, m.group(2))
        body = _EDGE_LABEL.sub(quote_edge, body)
        return f"{m.group(1)}{body}{m.group(3)}"

    return _MERMAID_BLOCK.sub(fix_block, markdown)


def postprocess(text: str) -> str:
    """Strip an outer ```markdown fence if the model added one, then fix mermaid."""
    text = text.strip()
    m = re.match(r"^```(?:markdown|md)\s*\n(.*)\n```\s*$", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    return fix_mermaid(text)


# Generation with retry and fallback

def generate_readme(client: genai.Client, prompt: str, models: list[str]) -> tuple[str, str]:
    """Try each model in order. Server errors and 429s are retried with backoff;
    other errors move on to the next model. Returns (text, model_used)."""
    for model in models:
        for attempt in range(1, MAX_ATTEMPTS_PER_MODEL + 1):
            print(f"Trying '{model}' (attempt {attempt}/{MAX_ATTEMPTS_PER_MODEL})...")
            try:
                response = client.models.generate_content(model=model, contents=prompt)
            except errors.APIError as e:
                code = getattr(e, "code", None)
                print(f"  API error ({code}): {e}")
                if code in (401, 403):
                    raise RuntimeError("API key was rejected. Check GEMINI_API_KEY.") from e
                retryable = isinstance(e, errors.ServerError) or code == 429
                if retryable and attempt < MAX_ATTEMPTS_PER_MODEL:
                    wait = BASE_BACKOFF ** attempt + random.uniform(0, 1)
                    print(f"  Retrying in {wait:.1f}s...")
                    time.sleep(wait)
                    continue
                break  # give up on this model
            except Exception as e:  # network errors, etc.
                print(f"  Unexpected error: {e}")
                break

            if response.text:
                return response.text, model
            print("  Empty response.")
            break

    raise RuntimeError("All models failed to generate a README.")


# CLI

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gen-readme",
        description="Generate a README.md for a project using Gemini.",
    )
    parser.add_argument("path", nargs="?", default=".", help="Project directory (default: current directory)")
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing README.md even if it lacks the auto-generated marker",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List the files that would be sent and exit without calling the API or writing anything",
    )
    parser.add_argument(
        "--model", help="Primary model to try first; the default chain is used as fallback",
    )
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    root_dir = os.path.abspath(args.path)
    if not os.path.isdir(root_dir):
        raise ValueError(f"Not a directory: {args.path}")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key and not args.dry_run:
        raise ValueError("GEMINI_API_KEY environment variable is missing!")

    readme_path = os.path.join(root_dir, "README.md")
    if os.path.exists(readme_path):
        with open(readme_path, "r", encoding="utf-8", errors="ignore") as f:
            has_marker = SENTINEL_MARKER in f.read(500)
        if not has_marker:
            if not args.force:
                print("Existing README.md found without AI sentinel marker.")
                print("Skipping generation to protect manual documentation.")
                print("Tip: Run 'gen-readme --force' to override.")
                return
            print("--force flag detected. Overwriting existing README...")

    spec = load_gitignore(root_dir)

    print("Generating project directory tree...")
    project_name = os.path.basename(root_dir) or root_dir
    project_tree = f"{project_name}/\n{generate_tree(root_dir, spec)}"

    print("Gathering project source context...")
    codebase_context, included = gather_codebase_context(root_dir, spec)
    facts = detect_facts(root_dir)

    models = list(MODELS)
    if args.model:
        models = [args.model] + [m for m in MODELS if m != args.model]

    print(f"Sending {len(included)} file(s), {len(codebase_context):,} characters of context to the Gemini API.")
    if args.dry_run:
        for rel_path in included:
            print(f"  {rel_path}")
        print("\nDetected facts:")
        for key, value in facts.items():
            print(f"  {key}: {value}")
        print("\nDry run: nothing sent, nothing written.")
        return

    prompt = build_prompt(project_tree, codebase_context, facts)
    client = genai.Client(api_key=api_key)
    text, model_used = generate_readme(client, prompt, models)

    final_content = f"{SENTINEL_MARKER}\n\n{postprocess(text)}\n"
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(final_content)

    print(f"README.md successfully updated (model: {model_used}).")


def cli() -> None:
    """Entry point for the global gen-readme command."""
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"\n[Error] gen-readme failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()