"""Unchanged function bodies excerpted for the standalone packet; see manifest.json."""
import json,os,platform,re,subprocess,urllib.request
from pathlib import Path


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def capture(argv, *, cwd=None, env=None):
    result = subprocess.run([str(a) for a in argv], cwd=cwd, env=env, text=True,
                            capture_output=True, timeout=60)
    if result.returncode:
        # Do not print full command diagnostics or inherited environment.
        raise RuntimeError(f"{argv[0]} exited {result.returncode}")
    return result.stdout


def require_hosted(provider="github", droplet_id=None):
    if provider == "digitalocean":
        if platform.system() != "Linux":
            raise ValueError("DigitalOcean execution requires Linux; local builds are refused")
        if not droplet_id or not re.fullmatch(r"[0-9]{1,20}", droplet_id):
            raise ValueError("DigitalOcean execution requires the owned droplet ID")
        with urllib.request.urlopen("http://169.254.169.254/metadata/v1/id", timeout=5) as response:
            actual = response.read(32).decode().strip()
        if actual != droplet_id:
            raise ValueError("DigitalOcean metadata differs from the owned droplet ID")
        return
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or os.environ.get("RUNNER_OS") != "Linux" or platform.system() != "Linux"):
        raise ValueError("execution requires a disposable GitHub-hosted Linux runner; local builds are refused")


def output_path(value, provider="github"):
    out = Path(value).resolve()
    temp = (Path("/root/ax-validation") if provider == "digitalocean" else Path(os.environ["RUNNER_TEMP"])).resolve()
    if out == temp or not out.is_relative_to(temp):
        raise ValueError("output must be a fresh directory below " + str(temp))
    return out
