# Thought Router

Type a thought on your phone. It lands in your **journal**, your **to-do list**, or becomes a
task for a **Claude Code agent** running on your PC.

```
Phone app ──issue labelled "inbox"──▶ private repo `notebook`
                                           │
PC worker (polls every 60s) ◀──────────────┘
  ├─ journal → notebook/journal/YYYY-MM.md  (commit + push)
  ├─ todo    → notebook/todo.md             (commit + push)
  └─ agent   → claude -p in agent-work/<issue#>, result posted as an issue comment
```

- **`docs/`**: an installable phone web app (PWA), hosted on GitHub Pages. It holds no secrets; your
  token is stored only on your phone.
- **`worker/`**: a Python script (standard library only) that uses the `gh` and `claude` CLIs you're
  already logged into.

## Setup

1. Create the private `notebook` repo, its labels and a local clone (see `worker/config.json` for paths).
2. Push this repo and enable Pages (Settings → Pages → Deploy from branch `main`, folder `/docs`).
3. Create a fine-grained token: GitHub → Settings → Developer settings → Fine-grained tokens.
   - Repository access: only `notebook`
   - Permissions: Issues: Read and write
4. On your phone, open the Pages URL, tap ⚙︎, paste the token, then use **Add to Home Screen**.
5. On the PC: `powershell -ExecutionPolicy Bypass -File worker\install-task.ps1`

## Using it

- **Send**: Claude decides where the note goes.
- **Journal / To-do / Agent**: you choose the route yourself, and no classification runs.
- **Recent** shows each entry's status. Tap one to open the issue, where agent results appear as comments.

## Safety

- The agent works only in `agent-work\<issue#>`. It can edit files there and search the web. By default
  it can't run shell commands (`"allow_bash": true` in `config.json` turns that on) or touch files elsewhere.
- Only issues written by the `owner` account in `config.json` are processed.
- Logs: `worker/logs/router.log`. Stop the worker: `Stop-ScheduledTask ThoughtRouter`.
