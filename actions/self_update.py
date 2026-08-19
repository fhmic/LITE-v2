#self_update.py
"""
Lets LITE check its own GitHub repo for updates and pull them down — so the
source of truth can live on GitHub while LITE keeps the local copy in sync,
without you manually re-downloading a zip each time.

This only works if the app's own folder is (or is inside) a git repository
— i.e. it was set up with `git clone`, GitHub Desktop, or has since had
`git init` + a remote pointed at the GitHub repo. A folder extracted from a
plain "Download ZIP" has no .git folder and nothing here will apply; use
`git clone` once to switch it over, and updates from then on can flow
through this instead of re-downloading.

Safety model:
- Never pulls with uncommitted local changes present without your consent —
  by default it stashes them first (never discards) and tells you exactly
  how to get them back.
- Uses a fast-forward-only pull — it will never create merge commits or
  silently resolve conflicts. If history has diverged, it stops and reports
  that instead of guessing.
- Read-only "check" mode never touches any files — safe to run anytime,
  including automatically at startup.
- Changes take effect on next restart, same as self_maintain fixes.
"""
import json
import subprocess
import sys
import time
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _load_config() -> dict:
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _github_token() -> str:
    """
    Optional Personal Access Token for private repos, stored in
    api_keys.json as "github_token". Only needed as a fallback when no
    interactive credential prompt is available (e.g. headless/background
    runs) — with a normal desktop setup, git's own credential manager
    (Windows Credential Manager, SSH key, etc.) handles this automatically
    the first time you authenticate, same as any private-repo git command.

    Note: like the Gemini key already stored here, this sits in plaintext
    in config/api_keys.json — fine for a local personal machine, but don't
    commit that file or share it.
    """
    return (_load_config().get("github_token") or "").strip()


def _github_repo_url() -> str:
    """Optional configured source repo, e.g. https://github.com/you/lite.git — used by clone mode."""
    return (_load_config().get("github_repo_url") or "").strip()


def _auth_args() -> list[str]:
    """
    Injects a bearer-token auth header for this git invocation only, when a
    token is configured — without touching the stored remote URL (so a
    plaintext token never ends up written into .git/config).
    """
    token = _github_token()
    if not token:
        return []
    return ["-c", f"http.extraheader=AUTHORIZATION: bearer {token}"]


def _find_repo_root(start: Path, max_up: int = 4) -> Path | None:
    """
    Walks upward from `start` looking for a .git folder — handles both a
    layout where the git repo root IS the app folder, and one where the app
    folder is a subdirectory of the repo (e.g. a VP/ subfolder).
    """
    cur = start.resolve()
    for _ in range(max_up + 1):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def _run_git(args: list[str], cwd: Path, timeout: int = 20) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd),
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "git command timed out"
    except Exception as e:
        return 1, "", str(e)


def _current_branch(repo: Path) -> str:
    code, out, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    return out if code == 0 and out else "main"


def _has_local_changes(repo: Path) -> bool:
    code, out, _ = _run_git(["status", "--porcelain"], repo)
    return code == 0 and bool(out.strip())


def check_for_updates() -> dict:
    """
    Read-only — fetches remote refs and reports how far behind the local
    copy is, without changing any files. Safe to call anytime, including
    automatically at startup.
    """
    if not _git_available():
        return {"ok": False, "reason": "git is not installed or not on PATH."}

    repo = _find_repo_root(BASE_DIR)
    if not repo:
        return {
            "ok": False,
            "reason": (
                "This copy of LITE isn't a git repository (no .git folder found). "
                "Use `git clone` (or GitHub Desktop) once to set it up as one, and "
                "updates can flow through this from then on."
            ),
        }

    branch = _current_branch(repo)
    code, _, err = _run_git([*_auth_args(), "fetch", "--quiet"], repo, timeout=25)
    if code != 0:
        return {"ok": False, "reason": f"Couldn't reach GitHub: {err[:200]}"}

    code, out, _ = _run_git(
        ["rev-list", "--count", f"HEAD..origin/{branch}"], repo
    )
    behind = int(out) if code == 0 and out.isdigit() else 0

    summary = ""
    if behind:
        code2, log_out, _ = _run_git(
            ["log", "--oneline", f"HEAD..origin/{branch}", "-n", "5"], repo
        )
        summary = log_out

    return {
        "ok": True,
        "repo": str(repo),
        "branch": branch,
        "behind": behind,
        "summary": summary,
        "local_changes": _has_local_changes(repo),
    }


def update_self() -> str:
    """
    Pulls the latest commits from GitHub. Fast-forward only — refuses rather
    than creating a merge commit or resolving conflicts silently. Any
    uncommitted local changes are stashed first (never discarded).
    """
    status = check_for_updates()
    if not status["ok"]:
        return status["reason"]

    repo   = Path(status["repo"])
    branch = status["branch"]

    if status["behind"] == 0:
        return "Already up to date, sir — no new commits on GitHub."

    stash_note = ""
    if status["local_changes"]:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        code, _, err = _run_git(
            ["stash", "push", "-u", "-m", f"lite-auto-stash {stamp}"], repo
        )
        if code != 0:
            return f"Couldn't safely stash local changes, so I stopped before pulling: {err[:200]}"
        stash_note = (
            " Local changes were stashed first (nothing lost) — "
            "run `git stash pop` afterward to bring them back."
        )

    code, out, err = _run_git(
        [*_auth_args(), "pull", "--ff-only", "origin", branch], repo, timeout=30
    )
    if code != 0:
        return (
            f"Pull failed — your local history has diverged from GitHub, so I "
            f"stopped rather than guess how to merge it: {err[:250]}. "
            f"You'll need to resolve this manually (e.g. `git status` / `git log`)."
            f"{stash_note}"
        )

    return (
        f"Updated — pulled {status['behind']} commit"
        f"{'s' if status['behind'] != 1 else ''} from GitHub.{stash_note} "
        f"Restart LITE for the update to take effect.\n\n{status['summary']}"
    )


def clone_self(repo_url: str = "", dest: str = "") -> str:
    """
    Bootstraps a fresh git checkout from GitHub into a NEW folder — this
    never overwrites the currently-running app in place (git clone requires
    an empty/nonexistent target anyway, and swapping a live install's files
    from underneath itself is not something to do without your review).

    For a private repo: with a normal desktop setup, git's own credential
    manager will pop up an interactive GitHub sign-in the first time this
    runs — nothing extra to configure. If you're running headless with no
    interactive prompt available, set "github_token" (a GitHub Personal
    Access Token with repo read access) in config/api_keys.json as a
    fallback and it'll be used automatically.
    """
    if not _git_available():
        return "git is not installed or not on PATH — install Git for Windows first."

    existing = _find_repo_root(BASE_DIR)
    if existing:
        return (
            f"LITE is already a git checkout (found at {existing}) — no need to "
            f"clone again. Use mode='update' to pull the latest changes instead."
        )

    url = (repo_url or _github_repo_url()).strip()
    if not url:
        return (
            "I need a repo URL to clone from — pass one, or set \"github_repo_url\" "
            "in config/api_keys.json."
        )

    target_root = BASE_DIR.parent
    stamp        = time.strftime("%Y%m%d-%H%M%S")
    dest_path    = Path(dest).resolve() if dest else (target_root / f"LITE-clone-{stamp}")

    if dest_path.exists() and any(dest_path.iterdir()):
        return f"Destination {dest_path} already exists and isn't empty — pick a different path."

    code, out, err = _run_git(
        [*_auth_args(), "clone", "--quiet", url, str(dest_path)],
        cwd=BASE_DIR.parent, timeout=120,
    )
    if code != 0:
        # Don't echo the raw error if a token might be embedded in it via the URL
        safe_err = err.replace(_github_token(), "•••") if _github_token() else err
        return f"Clone failed: {safe_err[:300]}"

    return (
        f"Cloned to {dest_path}. This is a separate copy for you to review — "
        f"it won't have your existing config/api_keys.json (Gemini key, etc.), "
        f"so copy that over from your current install before switching to it."
    )


# ── Public entry point ───────────────────────────────────────────────────────

def self_update(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    p    = parameters or {}
    mode = (p.get("mode") or "check").strip().lower()

    def log(msg: str):
        print(f"[SelfUpdate] {msg}")
        if player:
            player.write_log(f"[SelfUpdate] {msg}")

    if mode == "clone":
        log("Cloning from GitHub...")
        return clone_self(
            repo_url=(p.get("repo_url") or "").strip(),
            dest=(p.get("dest") or "").strip(),
        )

    if mode == "update":
        log("Pulling latest changes from GitHub...")
        return update_self()

    # mode == "check" (default) — read-only
    status = check_for_updates()
    if not status["ok"]:
        return status["reason"]
    if status["behind"] == 0:
        return "You're up to date, sir — no new commits on GitHub."
    plural = "s" if status["behind"] != 1 else ""
    return (
        f"{status['behind']} new commit{plural} available on GitHub "
        f"({status['branch']} branch):\n{status['summary']}\n\n"
        f"Say 'update yourself' to pull the changes."
    )
