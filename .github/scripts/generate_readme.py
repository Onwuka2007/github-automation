import os
import time
import random
import pathspec
from google import genai
from google.genai import errors

IGNORE_PATTERNS = [
    ".git",
    "node_modules",
    "venv",
    "env",
    "dist",
    "build",
    ".next",
    "__pycache__",
    ".DS_Store",
    "package-lock.json",
    "yarn.lock",
]

def load_gitignore(root_dir):
    ignore_list = list(IGNORE_PATTERNS)
    gitignore_path = os.path.join(root_dir, ".gitignore")
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r") as f:
            ignore_list.extend(f.read().splitlines())
    return pathspec.PathSpec.from_lines("gitwildmatch", ignore_list)

def generate_tree(start_dir, spec):
    tree_lines = []
    for root, dirs, files in os.walk(start_dir):
        rel_root = os.path.relpath(root, start_dir)
        if rel_root == ".":
            rel_root = ""
        dirs[:] = [d for d in dirs if not spec.match_file(os.path.join(rel_root, d))]
        level = rel_root.count(os.sep) if rel_root else 0
        indent = "│   " * level + "├── " if level > 0 else ""
        if rel_root:
            tree_lines.append(f"{indent}{os.path.basename(root)}/")
        sub_indent = "│   " * (level + 1) + "├── " if rel_root else "├── "
        for file in sorted(files):
            file_path = os.path.join(rel_root, file)
            if not spec.match_file(file_path):
                tree_lines.append(f"{sub_indent}{file}")
    return "\n".join(tree_lines)

def gather_codebase_context(root_dir, spec):
    context = []
    priority_files = ["package.json", "requirements.txt", "Cargo.toml", "go.mod", "pom.xml", "Dockerfile", "README.md"]
    for root, dirs, files in os.walk(root_dir):
        rel_root = os.path.relpath(root, root_dir)
        if rel_root == ".":
            rel_root = ""
        dirs[:] = [d for d in dirs if not spec.match_file(os.path.join(rel_root, d))]
        for file in files:
            file_path = os.path.join(root, file)
            rel_path = os.path.join(rel_root, file) if rel_root else file
            if spec.match_file(rel_path):
                continue
            if file in priority_files or file.endswith((".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".cpp", ".c", ".php")):
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                        if len(content) > 3000:
                            content = content[:3000] + "\n... [truncated]"
                        context.append(f"--- FILE: {rel_path} ---\n{content}\n")
                except Exception:
                    pass
    return "\n".join(context)

def main():
    root_dir = "."
    spec = load_gitignore(root_dir)
    
    print("Generating project directory tree...")
    project_tree = generate_tree(root_dir, spec)
    
    print("Gathering project source context...")
    codebase_context = gather_codebase_context(root_dir, spec)
    
    prompt = f"""
    You are an expert technical writer and software architect. Analyze the following project structure and source code snippets to generate a professional, standardized, and comprehensive README.md.

    ### Project Folder Structure:
    ```text
    {project_tree}
    ```

    ### Codebase Context:
    {codebase_context}

    ### Requirements for the generated README.md:
    1. **Project Title & Catchy Description**: Clear overview of what the project does based on its framework/code.
    2. **Features**: Key functional highlights.
    3. **Folder Structure**: Display the tree structure provided above with brief explanations of key directories.
    4. **Tech Stack**: List languages, frameworks, libraries, and tools used.
    5. **Getting Started / Installation**: Clear prerequisite and setup steps inferred from configuration files.
    6. **License / Usage**: Standard placeholder or inferred details.
    
    Output ONLY valid markdown content for the README.md file without conversational filler.
    """

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    
    models_to_try = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    response = None
    max_attempts_per_model = 2
    base_backoff = 2

    for model_name in models_to_try:
        success = False
        for attempt in range(1, max_attempts_per_model + 1):
            try:
                print(f"Trying model '{model_name}' (Attempt {attempt}/{max_attempts_per_model})...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                success = True
                break
            except errors.ServerError as e:
                print(f"Server error on {model_name}: {e}")
                if attempt == max_attempts_per_model:
                    print(f"Model {model_name} exhausted. Trying next model...")
                else:
                    wait = base_backoff ** attempt + random.uniform(0, 1)
                    time.sleep(wait)
            except Exception as e:
                print(f"Client/Network error with model {model_name}: {e}. Trying next model...")
                break
        
        if success:
            break

    if not response or not response.text:
        raise RuntimeError("All fallback models failed to generate content.")

    with open("README.md", "w", encoding="utf-8") as f:
        f.write(response.text)
    print("README.md successfully updated!")

if __name__ == "__main__":
    main()
