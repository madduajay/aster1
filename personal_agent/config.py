import os
from pathlib import Path
from urllib.parse import urlsplit
import json

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = Path(os.environ.get("PERSONAL_AGENT_RUNTIME", ROOT / ".runtime"))
ORIGIN = "https://aster.localhost:55468"
MODEL = "gpt-5.4-mini"
SEARCH_MODEL = "gpt-5.4-mini"
BROWSER_MODEL = os.environ.get('PERSONAL_AGENT_BROWSER_MODEL', 'gpt-6.1-terra')
VERIFICATION_MODEL = "gpt-6.1-terra"
MODEL_MAX_RETRIES = 2
MEMORY_CONSOLIDATION_INTERVAL = 3600
MEMORY_IDLE_SECONDS = 300
TEMPORAL_ADDRESS = os.environ.get('TEMPORAL_ADDRESS','127.0.0.1:7233')
TEMPORAL_NAMESPACE = os.environ.get('TEMPORAL_NAMESPACE','default')
DINING_COMPANY_URL = os.environ.get('DINING_COMPANY_URL','http://127.0.0.1:55469/mcp/')
DINING_TASK_QUEUE = os.environ.get('DINING_TASK_QUEUE','aster-dining')
BROWSER_HEADLESS = os.environ.get('PERSONAL_AGENT_BROWSER_HEADLESS','1')!='0'
TIMEZONE = os.environ.get('PERSONAL_AGENT_TIMEZONE', 'America/Phoenix')


def allowed_origins():
    origins = {ORIGIN}
    path = RUNTIME / 'public-origin.json'
    if path.exists():
        origin = json.loads(path.read_text())['origin']
        parts = urlsplit(origin)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                or parts.path or parts.query or parts.fragment or parts.port not in {None, 443}
                or origin != 'https://' + parts.hostname):
            raise ValueError('Invalid configured public HTTPS origin')
        origins.add(origin)
    return origins


def prepare():
    os.umask(0o077)
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)


def keychain_key():
    """Read the authorized item without printing it or invoking a shell."""
    descriptor = os.environ.pop('PERSONAL_AGENT_KEY_FD', None)
    if descriptor is not None:
        with os.fdopen(int(descriptor)) as pipe:
            value = pipe.read(1024).strip()
        if not value.startswith('sk-') or any(c.isspace() for c in value):
            raise RuntimeError('Invalid startup credential')
        return value
    import subprocess
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", "local.aster.openai",
         "-a", "aster", "-w", str(Path.home() / "Library/Keychains/login.keychain-db")],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError("OpenAI Keychain access failed; unlock/authorize the item locally.")
    value = result.stdout.strip()
    if not value.startswith("sk-") or any(c.isspace() for c in value):
        raise RuntimeError("The existing OpenAI credential is invalid.")
    return value
