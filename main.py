import ast
import os
import re
import subprocess
import sys
from github import Github
from openai import OpenAI

OPENAI_API_KEY = os.getenv("INPUT_OPENAI_API_KEY")
GITHUB_TOKEN = os.getenv("INPUT_GITHUB_TOKEN")
REPO_NAME = os.getenv("GITHUB_REPOSITORY")
WORKSPACE = os.getenv("GITHUB_WORKSPACE", ".")

client = OpenAI(api_key=OPENAI_API_KEY)
gh = Github(GITHUB_TOKEN)


def run_tests() -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest"],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0, result.stdout + "\n" + result.stderr


def sanitize_logs(log_text: str) -> str:
    cleaned = re.sub(r"sk-[a-zA-Z0-9]{32,}", "[REDACTED_OPENAI_KEY]", log_text)
    cleaned = re.sub(r"ghp_[a-zA-Z0-9]{36}", "[REDACTED_GITHUB_TOKEN]", cleaned)
    injection_patterns = [
        r"(?i)ignore\s+previous\s+instructions",
        r"(?i)system\s+prompt",
        r"(?i)override\s+rules",
    ]
    for pattern in injection_patterns:
        cleaned = re.sub(pattern, "[FILTERED_INJECTION_ATTEMPT]", cleaned)
    return cleaned


def generate_patch(sanitized_log: str) -> str:
    prompt = (
        "You are an automated code repair agent. A unit test failed with the following trace:\n\n"
        f"--- TEST FAILURE TRACE ---\n{sanitized_log}\n\n"
        "Task: Identify the broken target file and generate the complete corrected Python code.\n"
        "Output MUST strictly follow this format:\n"
        "FILE: relative/path/to/file.py\n"
        "CODE:\n"
        "<full corrected code content>"
    )
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
    )
    return response.choices[0].message.content.strip()


def validate_and_parse_patch(patch_response: str) -> tuple[str, str]:
    lines = patch_response.splitlines()
    target_file = None
    code_lines = []
    is_code = False

    for line in lines:
        if line.startswith("FILE:"):
            target_file = line.replace("FILE:", "").strip()
        elif line.startswith("CODE:"):
            is_code = True
        elif is_code:
            code_lines.append(line)

    if not target_file:
        raise ValueError("Guardrail Alert: LLM response missing target file path.")

    fixed_code = "\n".join(code_lines).strip()

    try:
        tree = ast.parse(fixed_code)
    except SyntaxError as e:
        raise ValueError(f"Guardrail Alert: Invalid Python syntax generated: {e}")

    forbidden_calls = {"eval", "exec", "__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
                raise SecurityError(f"Guardrail Alert: Blocked prohibited function call '{node.func.id}'.")

    return target_file, fixed_code


class SecurityError(Exception):
    pass


def apply_patch_and_open_pr(target_file: str, fixed_code: str, raw_logs: str):
    full_path = os.path.join(WORKSPACE, target_file)

    with open(full_path, "w") as f:
        f.write(fixed_code + "\n")

    passed, _ = run_tests()
    if not passed:
        print("⚠️ Local verification failed after patch. Aborting PR creation.")
        return

    branch_name = f"sentinel/auto-fix-{os.urandom(4).hex()}"
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=WORKSPACE)
    subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], cwd=WORKSPACE)
    subprocess.run(["git", "checkout", "-b", branch_name], cwd=WORKSPACE)
    subprocess.run(["git", "add", target_file], cwd=WORKSPACE)
    subprocess.run(["git", "commit", "-m", f"fix(sentinel): auto-heal {target_file}"], cwd=WORKSPACE)
    subprocess.run(["git", "push", "origin", branch_name], cwd=WORKSPACE)

    repo = gh.get_repo(REPO_NAME)
    pr_body = (
        "### 🤖 Vibe Sentinel Auto-Fix Report\n\n"
        f"**Target File:** `{target_file}`\n"
        "**Guardrail Checks:** Passed (AST Syntax Validated, No Forbidden Calls)\n\n"
        "**Original Failure Log:**\n"
        "```\n"
        f"{raw_logs[:600]}\n"
        "```\n\n"
        "*Automated patch generated and pre-verified by Vibe Sentinel.*"
    )
    repo.create_pull(
        title=f"🤖 [Draft] Fix failing tests in {target_file}",
        body=pr_body,
        head=branch_name,
        base="main",
        draft=True,
    )
    print(f"🚀 Created Draft Pull Request on branch '{branch_name}'.")


def main():
    print("🚀 [Vibe Sentinel] Initializing workflow monitoring...")
    passed, logs = run_tests()

    if passed:
        print("✅ All tests pass cleanly. No intervention required.")
        return

    print("❌ Failure detected. Running input guardrails...")
    clean_logs = sanitize_logs(logs)

    print("🧠 Generating patch via LLM...")
    patch_response = generate_patch(clean_logs)

    try:
        print("🛡️ Running AST output guardrails...")
        target_file, fixed_code = validate_and_parse_patch(patch_response)

        print("🛠️ Applying patch and opening Pull Request...")
        apply_patch_and_open_pr(target_file, fixed_code, clean_logs)
    except (ValueError, SecurityError) as e:
        print(f"🚨 Execution halted by Guardrail: {e}")


if __name__ == "__main__":
    main()