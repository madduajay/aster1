# Aster

A local personal agent for macOS, built with FastAPI, DeepAgents/LangGraph, OpenAI, Browser Use, SQLite and a vanilla JavaScript chat UI.

This branch contains the runtime application and setup files only. Tests, validation scripts, local databases, account passwords, API keys, TLS material, browser profiles, uploads, logs, ZIP archives and virtual environments are excluded.

## Requirements

- macOS with Python 3.12 and its standard Keychain Access utility.
- An OpenAI API key with access to the models configured in `personal_agent/config.py`.
- A trusted TLS certificate valid for `aster.localhost`, with its matching private key. Provision these locally; neither belongs in Git.

## Install

From this checkout:

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
PLAYWRIGHT_BROWSERS_PATH="$PWD/.runtime/playwright" .venv/bin/python -m playwright install chromium
.venv/bin/python -m venv .runtime/browser-use-venv
.runtime/browser-use-venv/bin/pip install -r browser-use-requirements.lock
```

In macOS Keychain Access, create or update a generic password with service/name `local.aster.openai`, account `aster`, and your API key as its password. The launcher reads this item without putting the key in source, logs or command-line arguments.

Create `.runtime/tls/` and place your trusted certificate at `.runtime/tls/server.pem` and matching private key at `.runtime/tls/server.key`. Initialize the local certificate fingerprint manifest:

```sh
.venv/bin/python - <<'PYTLS'
import hashlib, json, os
from pathlib import Path
os.umask(0o077)
directory = Path('.runtime/tls')
for path in (Path('.runtime'), directory):
    path.chmod(0o700)
files = {suffix: directory / ('server.' + suffix) for suffix in ('pem', 'key')}
for path in files.values():
    path.chmod(0o600)
manifest = directory / 'fingerprints.json'
if manifest.exists():
    raise SystemExit('Manifest already exists; keep existing fingerprints.')
manifest.write_text(json.dumps({suffix: hashlib.sha256(path.read_bytes()).hexdigest()
                                for suffix, path in files.items()}))
PYTLS
```

The manifest lets the launcher use your provisioned TLS files without its legacy certificate-copy fallback. Keep the certificate trusted by macOS and your browser; the launcher verifies its fingerprint on later starts.

```sh
.venv/bin/python manage.py setup
.venv/bin/python manage.py start
.venv/bin/python manage.py status
```

Open **https://aster.localhost:55468**. Setup provisions ten local accounts, `user01` through `user10`; initial passwords are written to the owner-only `.runtime/initial-accounts.txt`. Each user must set their own password at first login. Stop with `.venv/bin/python manage.py stop`.

## Chat and images

The UI supports formatted response cards, short answers by default, browser preview and manual takeover, and action approval cards. Click **+** in the composer to attach files, or drag and drop them. Image uploads show a clickable preview; type a question after attaching an image. PNG, JPEG, WebP and GIF are recognized. General files have a 50 MB limit; images have a 20 MB limit, with up to four recent images supplied to the model within a combined 20 MB budget. Generic documents are available for approved website uploads; general document parsing is not included.

Sessions and persistent preferences are separate. Useful commands include `/sessions`, `/open SESSION_ID`, `/files`, `/memory`, `/remember TEXT`, `/forget ID`, `/forget all`, `/correct ID TEXT`, `/debug` and `/stop`. Private website credentials and verification codes belong in the interactive browser takeover, not chat. Consequential browser actions require their displayed approval.

## Runtime configuration and storage

Model defaults, the local origin and timezone are in `personal_agent/config.py`. Browser model and default timezone can be overridden with `PERSONAL_AGENT_BROWSER_MODEL` and `PERSONAL_AGENT_TIMEZONE` before launch. The listener binds only to `127.0.0.1:55468`; no automatic startup service is installed.

All local state is under `.runtime/`, including SQLite data, user profiles, uploads, TLS files, and logs. Back it up separately while the app is stopped if you need to preserve accounts and conversations. The repository alone does not contain that state. The launcher expects to manage the process from this checkout; stop an instance in a different checkout before starting this one.

`tunnel.py` is included for optional ngrok access. To enable it, install the official ngrok executable locally at `.runtime/bin/ngrok`, place its authtoken in the owner-only `.runtime/ngrok-token.txt`, then run `.venv/bin/python tunnel.py start` and restart the app. Stop the tunnel with `.venv/bin/python tunnel.py stop`, then restart the app to remove its public origin. This is optional; the local URL works independently.
