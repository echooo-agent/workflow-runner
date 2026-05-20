#!/usr/bin/env python3
"""
Echo Workloop Agent — MiniMax-powered autonomous PR fixer
Uses [TOOL_CALL] text format to bypass MiniMax's tool_call ID validation issues
"""

import os, sys, json, subprocess, re, time
import urllib.request

# ── Config ──────────────────────────────────────────────────────────
MINIMAX_API_KEY  = os.environ.get("MINIMAX_API_KEY", "")
MODEL            = os.environ.get("MODEL", "MiniMax-M2.7-highspeed")
BASE_URL         = "https://api.minimaxi.com/v1"
TARGET_REPO      = os.environ.get("TARGET_REPO", "NousResearch/hermes-agent")
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
WORK_DIR         = os.environ.get("WORK_DIR", "/tmp/echo-work")
MAX_ITERATIONS   = 10
MAX_RESPONSE_CHARS = 16000
# ────────────────────────────────────────────────────────────────────

# ── Tool Descriptions ───────────────────────────────────────────────

SYSTEM_PROMPT = """You are Echo, DTAlex's autonomous execution agent fixing GitHub issues.

You have access to these tools. Call them by name in your response:
- read_file(path) — read a source file from the repository
- search_files(pattern, file_glob) — grep search in repository files
- list_dir(path) — list directory contents  
- apply_fix(files: {path: content}) — write files to apply your fix

WORKFLOW:
1. Understand the issue (read files, search code)
2. Plan the fix
3. Apply the fix using apply_fix()
4. Output a JSON summary comment

IMPORTANT:
- Use tools to actually explore the codebase before writing code
- If the issue is unclear, read CONTRIBUTING.md and relevant source files first
- Only call apply_fix when you have a concrete fix ready
- Output your final JSON summary after calling apply_fix
- Format tool calls as: read_file("src/app.py")
- Format apply_fix as: apply_fix({"src/app.py": "file content here"})
- If you cannot fix it, still use tools to investigate and explain what you found"""

# ── Tool Execution ─────────────────────────────────────────────────

TOOL_FUNCS = {
    "read_file": lambda args, path: _read_file(args, path),
    "search_files": lambda args, path: _search_files(args, path),
    "list_dir": lambda args, path: _list_dir(args, path),
    "run": lambda args, path: _run(args, path),
    "apply_fix": lambda args, path: _apply_fix(args, path),
}

def _read_file(args, repo_path):
    p = os.path.join(repo_path, args.get("path", args.get("file_path", "")))
    if not os.path.exists(p): return f"File not found: {args}"
    try:
        with open(p) as f: return f.read()[:8000]
    except Exception as e: return f"Error: {e}"

def _search_files(args, repo_path):
    pattern = args.get("pattern", "")
    glob = args.get("file_glob", "*")
    cmd = f'grep -rn --include="{glob}" "{pattern}" {repo_path} 2>/dev/null | head -30'
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout[:3000] or "No matches"

def _list_dir(args, repo_path):
    p = os.path.join(repo_path, args.get("path", "."))
    if not os.path.exists(p): return f"Dir not found: {args}"
    try: return "\n".join(sorted(os.listdir(p))[:50])
    except Exception as e: return f"Error: {e}"

def _run(args, repo_path):
    cmd = args.get("cmd", args.get("command", ""))
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=repo_path)
    return f"stdout:\n{r.stdout[:2000]}\nstderr:\n{r.stderr[:500]}\nexit: {r.returncode}"

def _apply_fix(args, repo_path):
    files_arg = args.get("files", {})
    # files may be a string (raw model output) or already a dict
    if isinstance(files_arg, str):
        try:
            files_arg = json.loads(files_arg)
        except:
            return f"Error: could not parse files as JSON: {files_arg[:200]}"
    written = []
    for path, content in files_arg.items():
        fp = os.path.join(repo_path, path)
        os.makedirs(os.path.dirname(fp) or repo_path, exist_ok=True)
        with open(fp, "w") as f: f.write(content)
        written.append(path)
    return f"Written {len(written)}: {', '.join(written)}"

def execute_tool(name, args, repo_path):
    """args is a dict like {"path": "src/app.py"}"""
    try:
        if name not in TOOL_FUNCS: return f"Unknown tool: {name}"
        return TOOL_FUNCS[name](args, repo_path)
    except Exception as e:
        return f"Error: {e}"

# ── API ────────────────────────────────────────────────────────────

def api_call(messages, max_tokens=8192):
    data = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False
    }
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(data).encode(),
        headers={"Authorization": f"Bearer {MINIMAX_API_KEY}", "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        raise Exception(f"API Error {e.code}: {body}")

# ── GitHub ─────────────────────────────────────────────────────────

def gh_api(endpoint, token=None):
    t = token or GITHUB_TOKEN
    req = urllib.request.Request(
        f"https://api.github.com{endpoint}",
        headers={"Authorization": f"token {t}", "Accept": "application/vnd.github.v3+json"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

def gh_post(endpoint, data, token=None):
    t = token or GITHUB_TOKEN
    req = urllib.request.Request(
        f"https://api.github.com{endpoint}",
        data=json.dumps(data).encode(),
        headers={"Authorization": f"token {t}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

def run(cmd, cwd=None):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
    return r.stdout, r.stderr, r.returncode

# ── Tool Call Parsing ──────────────────────────────────────────────

def parse_tool_calls(text):
    """Parse [TOOL_CALL] blocks with {tool => name, args => { --key "value" }} format"""
    calls = []
    block_pattern = r'\[TOOL_CALL\]\s*(.*?)\s*\[/TOOL_CALL\]'
    for block_match in re.finditer(block_pattern, text, re.DOTALL):
        block = block_match.group(1).strip()
        # Extract tool name
        name_match = re.search(r'tool\s*=>\s*"(\w+)"', block)
        if not name_match: continue
        name = name_match.group(1)
        if name not in TOOL_FUNCS: continue
        # Find the args section using brace counting
        args_m = re.search(r'args\s*=>\s*\{(.+)', block, re.DOTALL)
        if not args_m: continue
        args_str = args_m.group(1)
        # Parse --key "value" pairs with brace-counting for nested values
        args = {}
        i = 0
        while i < len(args_str):
            m = re.match(r'--(\w+)\s+', args_str[i:])
            if not m: break
            key = m.group(1)
            i += m.end()
            if args_str[i] == '{':
                depth, j = 1, i+1
                while j < len(args_str) and depth > 0:
                    if args_str[j] == '{': depth += 1
                    elif args_str[j] == '}': depth -= 1
                    j += 1
                val = args_str[i:j-1]
                i = j
            elif args_str[i] == '"':
                j = i+1
                while j < len(args_str):
                    if args_str[j] == '\\': j += 2
                    elif args_str[j] == '"': j += 1; break
                    else: j += 1
                val = args_str[i:j-1]
                i = j
            else:
                break
            args[key] = val
        calls.append({"name": name, "args": args})
    return calls

# ── Repo ───────────────────────────────────────────────────────────

def clone_target_repo():
    target_path = f"{WORK_DIR}/target"
    os.makedirs(target_path, exist_ok=True)
    if os.path.exists(f"{target_path}/.git"):
        run("git pull origin main", cwd=target_path)
    else:
        run(f"git clone https://x-access-token:{GITHUB_TOKEN}@github.com/{TARGET_REPO}.git {target_path}")
    return target_path

def find_issue():
    print("=== Finding issue ===")
    issues = gh_api(f"/repos/{TARGET_REPO}/issues?state=open&per_page=50&sort=updated&direction=asc")
    for issue in issues:
        if issue.get("pull_request") or issue.get("assignee"): continue
        labels = [l["name"].lower() for l in issue.get("labels", [])]
        if any(l in labels for l in ["bug", "type/bug", "help wanted", "good first issue"]):
            print(f"Selected: #{issue['number']} — {issue['title']}")
            return issue
    for issue in issues:
        if not issue.get("pull_request") and not issue.get("assignee"):
            print(f"Selected (fallback): #{issue['number']} — {issue['title']}")
            return issue
    return None

def get_issue_detail(issue_num):
    return gh_api(f"/repos/{TARGET_REPO}/issues/{issue_num}")

def commit_and_push(repo_path, branch_name, commit_msg):
    run("git config user.email 'echooo-agent@users.noreply.github.com'", cwd=repo_path)
    run("git config user.name 'echooo-agent'", cwd=repo_path)
    run(f"git checkout -b {branch_name}", cwd=repo_path)
    run("git add -A", cwd=repo_path)
    out, err, rc = run("git diff --cached --stat", cwd=repo_path)
    if rc or not out.strip(): return False, "No changes"
    run(f"git commit -m {json.dumps(commit_msg)}", cwd=repo_path)
    out, err, rc = run(f"git push -u origin {branch_name}", cwd=repo_path)
    if rc: return False, err[:300]
    return True, "Pushed"

def create_pr(title, body, head, base="main"):
    return gh_post(f"/repos/{TARGET_REPO}/pulls", {
        "title": title, "body": body, "head": head, "base": base
    })

# ── Agent Loop ─────────────────────────────────────────────────────

def run_workloop():
    print(f"=== Echo Workloop === Model={MODEL} Target={TARGET_REPO}")

    repo_path = clone_target_repo()
    print(f"Repo cloned: {repo_path}")

    issue = find_issue()
    if not issue: return {"status": "no_issue"}
    issue_num = issue["number"]
    detail = get_issue_detail(issue_num)
    issue_text = f"Issue #{issue_num}: {issue['title']}\n\n{detail.get('body', 'No description')}"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"""Repository: {TARGET_REPO}
Issue: {issue_text}
Repo path: {repo_path}

Please investigate this issue and apply a fix. Start by exploring the codebase."""}
    ]

    final_summary = {}

    for i in range(MAX_ITERATIONS):
        print(f"\n--- Iteration {i+1}/{MAX_ITERATIONS} ---")
        
        try:
            response = api_call(messages, max_tokens=8192)
        except Exception as e:
            print(f"API call failed: {e}")
            break

        msg = response["choices"][0]["message"]
        text = msg.get("content", "") or ""
        print(f"Model response ({len(text)} chars)")
        
        # Parse tool calls from text
        tool_calls = parse_tool_calls(text)
        
        if tool_calls:
            # Execute each tool and collect results
            tool_results_text = ""
            for tc in tool_calls:
                print(f"  → {tc['name']}({tc['args']})")
                result = execute_tool(tc["name"], tc["args"], repo_path)
                tool_results_text += f"\n[{tc['name']} result]\n{result}\n"
            
            # Add model text + tool results as user message
            messages.append({"role": "user", "content": f"Tool results:\n{tool_results_text}\nContinue or apply your fix."})
            continue
        else:
            # No tool calls — model is done, extract summary
            json_match = re.search(r'\{[^{}]*"analysis"[^{}]*\}', text, re.DOTALL)
            if json_match:
                try: final_summary = json.loads(json_match.group())
                except: pass
            break

    # Check if fix was applied
    out, _, _ = run("git status --short", cwd=repo_path)
    has_changes = bool(out.strip())
    print(f"\n=== Fix check: {'APPLIED' if has_changes else 'NOT APPLIED'} ===")
    
    if not has_changes:
        return {"status": "no_fix", "issue": issue_num, "iteration": i+1, "summary": final_summary}

    # Commit and PR
    branch_name = f"echo/fix-{issue_num}"
    title = f"fix(#{issue_num}): {issue['title'][:80]}"
    body = (
        f"## Summary\n\nFixes #{issue_num}\n\n"
        f"**Analysis:** {final_summary.get('analysis', 'See code changes.')}\n\n"
        f"**Plan:** {final_summary.get('plan', 'See code changes.')}\n\n"
        f"**Files:** {', '.join(final_summary.get('files_changed', []))}\n\n"
        f"---\n_Echo (DTAlex's execution layer)_"
    )

    ok, err = commit_and_push(repo_path, branch_name, title)
    if not ok: return {"status": "commit_failed", "issue": issue_num, "reason": err}

    print(f"Pushed: {branch_name}")
    pr = create_pr(title, body, branch_name)
    if "html_url" in pr:
        print(f"PR: {pr['html_url']}")
        return {"status": "success", "issue": issue_num, "pr": pr['html_url'], "pr_num": pr['number']}
    return {"status": "pr_failed", "issue": issue_num, "error": pr.get("message")}

if __name__ == "__main__":
    result = run_workloop()
    print(f"\n=== RESULT: {json.dumps(result)} ===")
