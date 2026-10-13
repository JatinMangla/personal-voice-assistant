# Use the Coder on any computer

The coding model runs on **Kaggle's free GPU**, not on your computer. So setting it up on a new
machine only means connecting the small client that Claude Code talks to. No GPU, no model
download, no Kaggle account on that machine, and no admin rights.

## On a machine where you canNOT clone this repo (office VM, borrowed laptop)

Use the single file **`dist/jarvis_coder_installer.py`** (65 KB). It has the whole client inside
it, so it needs no clone, no GitHub access and no second password. Copy it over however you like
(email it to yourself, a USB stick, paste it, or open it in GitHub's web UI and press *Download
raw*), then:

```
python jarvis_coder_installer.py
```

It asks for your `CODER_KEY` and does the rest: unpacks the client into `~/.jarvis-coder/client`,
installs `numpy`, `httpx` and `mcp`, remembers the key so a reboot does not lose it, writes
`.mcp.json` **in the folder you run it in** so Claude Code there picks up the tools, and prints the
key fingerprint plus whether the Coder is up. Then reopen the editor and run `coder_status`.

```
python jarvis_coder_installer.py --key <your key>   # do not be asked
python jarvis_coder_installer.py --uninstall        # undo everything it added
```

It reads the folder you run it in, so on that machine the Coder answers about **that** project, not
this one. Safe to run twice, and any other MCP server already in `.mcp.json` is kept.

**The one thing it cannot do for you:** installing those three packages needs to reach PyPI once. If
a locked-down VM blocks PyPI, install them however you normally do and run the file again -
everything else works with no internet beyond your own ngrok address.

**Keep it up to date:** the file is generated, so after changing anything under `scripts/coder/` run
`python scripts/make_coder_installer.py` and commit `dist/`. A test fails if a client module is ever
left out of it.

## The one command (on a machine that has the repo)

```
git clone https://github.com/JatinMangla/personal-voice-assistant
cd personal-voice-assistant
python scripts/setup_coder_client.py
```

That is the whole setup. It asks for your `CODER_KEY` (the one in your password manager) and does
everything else itself:

1. makes a `.venv` and installs the three packages the tools need (`numpy`, `httpx`, `mcp`);
2. writes `.mcp.json` with the right python path **for that operating system** — this is the one
   line that otherwise breaks the setup on a Mac or on Linux;
3. saves `CODER_KEY` so it survives a reboot (a Windows user variable, or a line in your shell
   profile on macOS/Linux);
4. builds the local code index (no GPU, no network);
5. checks the link and prints the **key fingerprint**, so a wrong key is obvious at once.

Then **reopen your editor** and ask Claude Code to run `coder_status`.

It is safe to run again any time — every step checks before it changes anything, and another MCP
server already in your `.mcp.json` is kept.

### If you would rather not be asked for the key

```
python scripts/setup_coder_client.py --key <your key>
```

Other switches: `--no-save-key` (use it for this check only), `--skip-index` (build the index later).

## What "offline" means

`coder_status` saying **offline** is normal and not a mistake. The model only exists while a Kaggle
run is up:

- **GitHub -> Actions -> Start Coder -> Run workflow**, then wait about 6-7 minutes.
- It stops itself after 20 minutes with no questions, so it never quietly eats your free GPU hours.

**`search_code` and `repo_map` work offline**, with no GPU and no internet. `ask_repo`, `investigate`,
`summarize` and `draft_code` need the run to be up.

## Checking it worked

| What you see | What it means |
|---|---|
| `online (key fingerprint 886c40a5)` | Working. The fingerprint must match the `[coder] key fingerprint` line in the Kaggle log. |
| `offline (press Start Coder; key fingerprint here ...)` | The client is fine; no Kaggle run is up. |
| `reachable but it REFUSED the key` | The key here differs from the `CODER_KEY` GitHub secret. Set it again from your password manager. |

## Asking about another project, or another branch

Set one variable to the folders you want, separated by `;` on Windows (`:` on macOS/Linux), then
reopen the editor:

```
CODER_ALLOWED_DIRS=D:\git\personal voice assistant;D:\git\my-other-project
```

Every answer then names which project a file is in (`my-other-project/src/app.py`), each project is
searched on its own wording, and a draft has to name the project in its path, so it can never write
into the wrong one.

**For two branches of the same repo**, give git a second folder rather than switching back and forth:

```
git worktree add ..\pva-old-branch main
# then: CODER_ALLOWED_DIRS=D:\git\personal voice assistant;D:\git\pva-old-branch
git worktree remove ..\pva-old-branch
```

Each extra folder makes the index and the repo map bigger, and the map is sent with every question,
so add only what you are actually asking about.

**Do not add office or company folders** unless your employer allows that code to leave the laptop:
the questions and the code pieces go to Kaggle (Google) and through ngrok.

## Things worth knowing

- **Several machines can use one running Coder at the same time** — it is just an authenticated
  address. They share the 20-minute idle timer and the free ngrok request budget.
- **The index does not travel between machines.** It stores absolute file paths, so copying
  `~/.jarvis-coder` to a computer where the repo lives elsewhere would make every entry stale. Let
  it rebuild; it takes seconds and needs no GPU.
- **Your ratings and corrections stay on the machine that made them** (`~/.jarvis-coder`), on
  purpose: they are notes about your code and they are never uploaded.
- **The meaning-search needs the Coder running**, because your *question* has to be turned into a
  vector by the model. Offline you still get keyword and symbol search.
- **Keys**: `CODER_KEY` must be identical here and in the `CODER_KEY` GitHub secret. Never set that
  secret through a pipe in PowerShell — it adds an invisible BOM and silently breaks the key. Use
  `gh secret set CODER_KEY --body $key`.

## If something is wrong

| Problem | Fix |
|---|---|
| `/mcp` does not list `coder` | Reopen the editor. If it still does not, run the setup command again and check it printed `.mcp.json: written`. |
| `coder_status` says the key was refused | The two keys differ. Compare the fingerprint it prints with the Kaggle log. |
| Answers are about the wrong project | Check `CODER_ALLOWED_DIRS` and reopen the editor; `coder_status` lists the folders it is reading. |
| Answers look out of date | `reindex`. Searches already re-read changed files every 30 seconds. |
| It cannot find something you know is there | Start the Coder and ask again, or use `search_code` with `deep=true`: meaning-search needs the model. |
