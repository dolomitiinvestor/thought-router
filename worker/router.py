"""Thought Router worker.

Polls the private `notebook` repo for issues labelled `inbox`, routes each one to
the journal, the to-do list, or an autonomous Claude Code agent run.

Usage:
    python router.py          # run forever
    python router.py --once   # process what's waiting, wait for any agent task, exit
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
REPO = f"{CONFIG['owner']}/{CONFIG['repo']}"
NOTEBOOK = Path(CONFIG["notebook_dir"])
AGENT_DIR = Path(CONFIG["agent_dir"])
ROUTES = ("journal", "todo", "agent")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CLAUDE = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
GH = shutil.which("gh") or "gh"
GIT = shutil.which("git") or "git"
MAX_COMMENT = 60000

(HERE / "logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(HERE / "logs" / "router.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("router")


def run(args, cwd=None, input_text=None, timeout=120):
    """Run a command and return stdout; raise with stderr on failure."""
    proc = subprocess.run(
        args,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(args[0]).name} failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


# ---------- GitHub ----------

def list_issues(label):
    out = run([GH, "issue", "list", "-R", REPO, "--label", label, "--state", "open",
               "--limit", "50", "--json", "number,title,body,author,labels,createdAt"])
    issues = json.loads(out)
    issues.sort(key=lambda i: i["number"])
    return issues


def labels_of(issue):
    return {l["name"] for l in issue["labels"]}


def edit_labels(number, add=(), remove=()):
    args = [GH, "issue", "edit", str(number), "-R", REPO]
    for name in add:
        args += ["--add-label", name]
    for name in remove:
        args += ["--remove-label", name]
    run(args)


def is_still_queued(number):
    issue = json.loads(run([GH, "issue", "view", str(number), "-R", REPO, "--json", "state,labels"]))
    return issue["state"] == "OPEN" and "agent" in labels_of(issue) and not labels_of(issue) & {"working", "error"}


def comment(number, text):
    if len(text) > MAX_COMMENT:
        text = text[:MAX_COMMENT] + "\n\n…(truncated)"
    run([GH, "issue", "comment", str(number), "-R", REPO, "--body-file", "-"], input_text=text)


def close(number):
    run([GH, "issue", "close", str(number), "-R", REPO])


def is_mine(issue):
    return issue["author"]["login"].lower() == CONFIG["owner"].lower()


def text_of(issue):
    return (issue.get("body") or "").strip() or issue["title"].strip()


def created_local(issue):
    return datetime.fromisoformat(issue["createdAt"].replace("Z", "+00:00")).astimezone()


# ---------- Classification ----------

CLASSIFY_PROMPT = """You sort notes that someone typed on their phone. Pick exactly one route:
- "journal": a thought, feeling, idea, question or observation to reflect on later
- "todo": something the person themselves needs to do
- "agent": a task explicitly for an AI agent to do now on a computer (research, write, draft, build, summarise, find...)

Also write a short title (max 8 words) in the note's language.
Reply with only JSON, no prose: {"route": "journal|todo|agent", "title": "..."}

Note:
<<<
%s
>>>"""


def classify(text):
    try:
        out = run([CLAUDE, "-p", "--model", CONFIG["classify_model"], "--output-format", "text"],
                  cwd=str(AGENT_DIR), input_text=CLASSIFY_PROMPT % text, timeout=180)
        match = re.search(r"\{.*\}", out, re.S)
        data = json.loads(match.group(0)) if match else {}
        route = data.get("route") if data.get("route") in ROUTES else "journal"
        title = (data.get("title") or "").strip() or first_line(text)
        return route, title
    except Exception as e:  # fall back to the journal rather than losing the entry
        log.warning("classification failed, defaulting to journal: %s", e)
        return "journal", first_line(text)


def first_line(text, limit=80):
    line = text.strip().splitlines()[0] if text.strip() else "(empty)"
    return line if len(line) <= limit else line[: limit - 1] + "…"


# ---------- Notebook (git) ----------

git_lock = threading.Lock()


def commit_and_push(message):
    run([GIT, "add", "-A"], cwd=NOTEBOOK)
    run([GIT, "commit", "-m", message], cwd=NOTEBOOK)
    run([GIT, "push"], cwd=NOTEBOOK)


def add_journal(issue, title):
    when = created_local(issue)
    path = NOTEBOOK / "journal" / f"{when:%Y-%m}.md"
    with git_lock:
        run([GIT, "pull", "--rebase", "--quiet"], cwd=NOTEBOOK)
        path.parent.mkdir(exist_ok=True)
        new = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            if new:
                f.write(f"# Journal: {when:%B %Y}\n\n")
            f.write(f"## {when:%Y-%m-%d %H:%M} · {title}\n\n{text_of(issue)}\n\n")
        commit_and_push(f"journal: {title} (#{issue['number']})")


def add_todo(issue, title):
    when = created_local(issue)
    path = NOTEBOOK / "todo.md"
    text = text_of(issue)
    with git_lock:
        run([GIT, "pull", "--rebase", "--quiet"], cwd=NOTEBOOK)
        new = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            if new:
                f.write("# To-do\n\n")
            f.write(f"- [ ] {title} ({when:%Y-%m-%d}, #{issue['number']})\n")
            if text != title and text.strip():
                for line in text.splitlines():
                    f.write(f"  > {line}\n" if line.strip() else "  >\n")
        commit_and_push(f"todo: {title} (#{issue['number']})")


# ---------- Agent ----------

agent_busy = threading.Event()


def run_agent(issue):
    number = issue["number"]
    workdir = AGENT_DIR / str(number)
    workdir.mkdir(parents=True, exist_ok=True)
    tools = ["WebSearch", "WebFetch"] + (["Bash"] if CONFIG.get("allow_bash") else [])
    args = [CLAUDE, "-p", "--permission-mode", "acceptEdits", "--output-format", "text",
            "--allowedTools", *tools]
    if CONFIG.get("agent_model"):
        args += ["--model", CONFIG["agent_model"]]
    prompt = (
        "You are working autonomously; nobody can answer questions, so make reasonable "
        "assumptions and state them. Put any files you create in the current directory. "
        "Finish with a concise summary of what you did and what the person should look at.\n\n"
        f"Task:\n{text_of(issue)}"
    )
    try:
        comment(number, f"🤖 Working on this in `{workdir}`…")
        out = run(args, cwd=str(workdir), input_text=prompt,
                  timeout=CONFIG["agent_timeout_minutes"] * 60)
        files = sorted(str(p.relative_to(workdir)) for p in workdir.rglob("*") if p.is_file())
        listing = "\n".join(f"- `{f}`" for f in files) or "_none_"
        comment(number, f"{out.strip()}\n\n---\n**Files in `{workdir}`:**\n{listing}")
        edit_labels(number, remove=["working"])
        close(number)
        log.info("agent #%s done", number)
    except Exception as e:
        log.exception("agent #%s failed", number)
        fail(number, e, remove=["working", "agent"])
    finally:
        agent_busy.clear()


def start_next_agent():
    if agent_busy.is_set():
        return None
    queued = [i for i in list_issues("agent") if is_mine(i) and not labels_of(i) & {"working", "error"}]
    # Issue lists lag behind recent edits; re-check each candidate directly before starting it.
    issue = next((i for i in queued if is_still_queued(i["number"])), None)
    if not issue:
        return None
    agent_busy.set()
    try:
        edit_labels(issue["number"], add=["working"])
    except Exception:
        agent_busy.clear()
        raise
    log.info("agent #%s starting", issue["number"])
    t = threading.Thread(target=run_agent, args=(issue,), daemon=True)
    t.start()
    return t


# ---------- Main loop ----------

def fail(number, error, remove=()):
    try:
        comment(number, f"⚠️ Thought Router hit an error:\n\n```\n{error}\n```")
        edit_labels(number, add=["error"], remove=list(remove))
    except Exception:
        log.exception("could not report error on #%s", number)


def process_inbox():
    for issue in list_issues("inbox"):
        number = issue["number"]
        if not is_mine(issue):
            log.info("skipping #%s from %s", number, issue["author"]["login"])
            continue
        labels = labels_of(issue)
        forced = [r for r in ROUTES if f"route:{r}" in labels]
        try:
            if forced:
                route, title = forced[0], first_line(text_of(issue))
            else:
                route, title = classify(text_of(issue))
            log.info("#%s -> %s: %s", number, route, title)
            if route == "journal":
                add_journal(issue, title)
                edit_labels(number, add=["journal"], remove=["inbox"])
                close(number)
            elif route == "todo":
                add_todo(issue, title)
                edit_labels(number, add=["todo"], remove=["inbox"])
                close(number)
            else:
                edit_labels(number, add=["agent"], remove=["inbox"])
        except Exception as e:
            log.exception("failed on #%s", number)
            fail(number, e, remove=["inbox"])


def main():
    once = "--once" in sys.argv
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Thought Router watching %s (once=%s)", REPO, once)
    while True:
        try:
            process_inbox()
            t = start_next_agent()
            if once:
                while t:
                    t.join()
                    t = start_next_agent()
                return
        except Exception:
            log.exception("poll failed")
            if once:
                sys.exit(1)
        time.sleep(CONFIG["poll_seconds"])


if __name__ == "__main__":
    main()
