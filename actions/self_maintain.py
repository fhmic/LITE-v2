#self_maintain.py
"""
Lets LITE inspect and repair its own source code, and build new features
into itself — scan for syntax errors and lint warnings, write fixes, or
create/extend files to implement something new the user asked for. Same
generate-verify-rollback pipeline either way, so a feature request is held
to the same safety bar as a bug fix.

Safety model:
- Only ever touches .py files inside LITE's own install directory (BASE_DIR)
  — every target path is resolved and checked to be inside it before any
  read or write happens. This applies to creating new files too.
- Every file is backed up (with a timestamp) before it's modified, into
  config/self_backups/. A fix that fails to compile is automatically rolled
  back from that backup — LITE never leaves itself in a broken state.
- Only runs when explicitly asked (voice/text command) — nothing here runs
  on its own in the background.
- A fix only ever changes the specific file(s) involved, one at a time, with
  a hard cap on how many files a single "auto" run will touch — so a bad
  request can't cascade into a wide, unreviewed rewrite.
- Changes take effect on the next restart (editing a .py file on disk does
  not alter the already-running process), so a fix is always reported back
  to the user together with a reminder to restart when ready.
"""
import ast
import json
import py_compile
import re
import shutil
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
BACKUP_DIR      = BASE_DIR / "config" / "self_backups"
MODEL_NAME      = "gemini-3.5-flash"

EXCLUDED_DIR_NAMES = {"__pycache__", "self_backups", ".git", "certs"}
MAX_AUTO_FIXES     = 3          # per "auto" invocation — keeps changes reviewable
MAX_CONTEXT_CHARS  = 6000       # cap on file size sent to the model per fix


# ── Model access ─────────────────────────────────────────────────────────────

def _get_api_key() -> str:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return (json.load(f).get("gemini_api_key") or "").strip()
    except Exception:
        return ""


def _generate(prompt: str) -> str:
    """
    Routes through the shared AI client — tries Gemini first, automatically
    falls back to Claude or a configured custom/local endpoint if Gemini
    fails or its quota is exhausted, so self-repair isn't dependent on a
    single provider being available.
    """
    from core.ai_client import generate_content as _ai_generate
    return _ai_generate(prompt, model=MODEL_NAME).text or ""


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


# ── Path safety ──────────────────────────────────────────────────────────────

def _safe_resolve(path_str: str) -> Path:
    """
    Resolves a user/model-supplied path against BASE_DIR and refuses to
    return anything outside of it. Raises ValueError on any attempt to
    escape (../, absolute paths elsewhere, symlink tricks, etc.).
    """
    candidate = Path(path_str)
    full = candidate if candidate.is_absolute() else (BASE_DIR / candidate)
    full = full.resolve()
    try:
        full.relative_to(BASE_DIR.resolve())
    except ValueError:
        raise ValueError(
            f"Refusing to touch '{path_str}' — it's outside LITE's own directory."
        )
    if full.suffix != ".py":
        raise ValueError(f"Refusing to touch '{path_str}' — self-repair only edits .py files.")
    return full


def _iter_py_files(subpath: str = "") -> list[Path]:
    root = BASE_DIR
    if subpath:
        root = _safe_resolve(subpath) if subpath.endswith(".py") else (BASE_DIR / subpath).resolve()
        try:
            root.relative_to(BASE_DIR.resolve())
        except ValueError:
            raise ValueError(f"'{subpath}' is outside LITE's own directory.")
        if root.is_file():
            return [root]

    files = []
    for p in root.rglob("*.py"):
        if any(part in EXCLUDED_DIR_NAMES for part in p.parts):
            continue
        files.append(p)
    return sorted(files)


# ── Static checks (no execution — safe to run anytime) ─────────────────────

def _compile_check(path: Path) -> str | None:
    """Returns None if the file compiles cleanly, else the error text."""
    try:
        py_compile.compile(str(path), doraise=True)
        return None
    except py_compile.PyCompileError as e:
        return str(e.exc_value) if e.exc_value else str(e)
    except Exception as e:
        return str(e)


def _pyflakes_check(path: Path) -> list[str]:
    """
    Best-effort lint pass — undefined names, unused imports, etc.
    Returns [] if pyflakes isn't installed rather than failing the scan.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pyflakes", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        out = (result.stdout or "").strip()
        if not out:
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def _ast_sanity(path: Path) -> str | None:
    """A second, dependency-free syntax check via the ast module."""
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        return None
    except SyntaxError as e:
        return f"{e.msg} (line {e.lineno})"
    except Exception as e:
        return str(e)


def _scan_file(path: Path) -> dict:
    compile_err = _compile_check(path)
    issues = {
        "file":            str(path.relative_to(BASE_DIR)),
        "compile_error":   compile_err,
        "flake_warnings":  [] if compile_err else _pyflakes_check(path),
    }
    return issues


def scan_self(target: str = "") -> tuple[list[dict], list[dict]]:
    """Returns (broken, warned) — files with compile errors vs. lint-only warnings."""
    files = _iter_py_files(target)
    broken, warned = [], []
    for f in files:
        result = _scan_file(f)
        if result["compile_error"]:
            broken.append(result)
        elif result["flake_warnings"]:
            warned.append(result)
    return broken, warned


# ── Backup / restore ─────────────────────────────────────────────────────────

def _backup(path: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    rel   = path.relative_to(BASE_DIR)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest  = BACKUP_DIR / f"{rel.as_posix().replace('/', '__')}.{stamp}.bak"
    shutil.copy2(path, dest)
    return dest


# ── Fixing ───────────────────────────────────────────────────────────────────

def fix_file(path: Path, issue_text: str = "") -> tuple[bool, str, Path | None]:
    """
    Attempts to fix one file. Always backs up first and always verifies the
    result still compiles — rolling back automatically if it doesn't.
    Returns (success, message, backup_path).
    """
    original = path.read_text(encoding="utf-8")

    if not issue_text:
        compile_err = _compile_check(path)
        flake       = _pyflakes_check(path) if not compile_err else []
        if compile_err:
            issue_text = f"SyntaxError / compile error:\n{compile_err}"
        elif flake:
            issue_text = "Lint warnings:\n" + "\n".join(flake)
        else:
            return False, f"No issues found in {path.relative_to(BASE_DIR)} — nothing to fix.", None

    backup_path = _backup(path)

    truncated = original
    was_truncated = False
    if len(truncated) > MAX_CONTEXT_CHARS * 2:
        truncated = truncated[:MAX_CONTEXT_CHARS * 2]
        was_truncated = True

    prompt = f"""You are an expert Python debugger fixing one file in an existing desktop app
called LITE. Fix ONLY the issue(s) described below — do not refactor, rename, or
remove anything that isn't part of the fix, and do not change behavior that
isn't related to the reported issue.

File: {path.relative_to(BASE_DIR)}
{"(file was truncated for length — fix based on what's shown)" if was_truncated else ""}

Issue(s) to fix:
{issue_text[:2500]}

Current code:
{truncated}

Rules:
- Output ONLY the complete corrected file content. No explanation, no markdown, no backticks.
- Preserve all existing imports, function signatures, and unrelated logic exactly.
- Do not introduce new external dependencies.
- If the file was truncated, only reproduce/fix the portion shown.

Corrected code:"""

    try:
        raw   = _generate(prompt)
        fixed = _strip_fences(raw)
    except Exception as e:
        return False, f"Could not generate a fix for {path.relative_to(BASE_DIR)}: {e}", backup_path

    if not fixed.strip():
        return False, f"Model returned an empty fix for {path.relative_to(BASE_DIR)} — left unchanged.", backup_path

    path.write_text(fixed, encoding="utf-8")

    compile_err = _compile_check(path)
    if compile_err:
        # Roll back — never leave the app in a broken state.
        shutil.copy2(backup_path, path)
        return (
            False,
            f"Fix for {path.relative_to(BASE_DIR)} didn't compile cleanly — rolled back. "
            f"Error: {compile_err[:300]}",
            backup_path,
        )

    integration_error = _integration_check()
    if integration_error:
        # Compiles fine on its own but breaks something that imports from
        # it (e.g. a function this fix renamed or removed) — same class
        # of bug py_compile can't catch. Roll back rather than leave a
        # working-in-isolation, broken-in-practice file.
        shutil.copy2(backup_path, path)
        return (
            False,
            f"Fix for {path.relative_to(BASE_DIR)} compiled fine alone but broke "
            f"main.py's imports — rolled back. Error: {integration_error[:300]}",
            backup_path,
        )

    return (
        True,
        f"Fixed {path.relative_to(BASE_DIR)} (backup: {backup_path.name}). "
        f"Restart LITE for the change to take effect.",
        backup_path,
    )


def _codebase_map() -> str:
    """
    A compact architecture map fed to the model before it decides where a
    new feature's code should live — so placement matches LITE's actual
    conventions (PyQt6 GUI, actions/ pattern, main.py tool registration)
    instead of the model (or the user) having to guess blind.
    """
    lines = [
        "LITE is a single Python 3 desktop app. Everything below is Python — "
        "never propose another language or GUI toolkit.",
        "",
        "Architecture:",
        "- main.py — entry point. Owns the live-session loop, the TOOLS "
        "function-calling schema (a Python list of dicts, each with "
        "name/description/parameters), and the dispatch chain that routes "
        "a called tool name to its actions/ function via "
        "`elif name == \"...\":`. Any NEW voice/text-triggered command "
        "MUST be registered in BOTH places in main.py, in addition to "
        "its actions/ file.",
        "- ui.py — the entire GUI, built in PyQt6 (NOT tkinter, NOT any "
        "web framework, NOT any other toolkit). MainWindow is the main "
        "window. Floating/overlay panels follow an established pattern — "
        "see the ClipboardPanel and WeatherPanel classes for reference: a "
        "QWidget subclass, styled with the `C` color-palette class and "
        "Courier New font, shown/hidden via a toggle and repositioned "
        "from MainWindow.resizeEvent(). Any new GUI-only surface (a "
        "panel, overlay, dashboard widget) belongs directly in ui.py "
        "using this same pattern — not a new file, not a different "
        "toolkit.",
        "- actions/ — one file per voice/text-callable capability. Each "
        "file exposes a top-level function taking "
        "(parameters: dict, player=None, session_memory=None, speak=None) "
        "-> str, named to match the file's purpose. This is where "
        "feature LOGIC (data fetching, processing, side effects) goes — "
        "not GUI code.",
        "- core/ai_client.py — shared multi-provider LLM client "
        "(Gemini -> Claude -> Groq -> custom endpoint fallback chain). "
        "Reuse this for anything needing an LLM call rather than adding "
        "a new provider integration.",
        "- config/api_keys.json — all persisted settings and API keys "
        "live here as flat JSON keys. Add new keys here rather than "
        "inventing a new config file.",
        "",
        "Existing actions/ files (for naming/style/convention reference):",
    ]
    actions_dir = BASE_DIR / "actions"
    if actions_dir.is_dir():
        for f in sorted(actions_dir.glob("*.py")):
            if f.name.startswith("_"):
                continue
            purpose, sig = "", "?"
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
                doc_match = re.search(r'"""(.*?)"""', text, re.DOTALL)
                if doc_match:
                    purpose = doc_match.group(1).strip().splitlines()[0][:120]
                sig_match = re.search(r"^def (\w+)\(([^)]*)\)", text, re.MULTILINE)
                if sig_match:
                    sig = f"{sig_match.group(1)}({sig_match.group(2)[:60]})"
            except Exception:
                pass
            lines.append(f"  - actions/{f.name}: {sig}" + (f" — {purpose}" if purpose else ""))
    return "\n".join(lines)


def infer_feature_files(description: str) -> list[str]:
    """
    Decides which file(s) a new feature needs, from a map of LITE's actual
    codebase — so build_feature() never has to fall back to asking the
    user which folder to use. Falls back to a single sensibly-named new
    actions/ file if the inference call itself fails.
    """
    prompt = f"""{_codebase_map()}

A user asked LITE to build this new feature:
"{description}"

Decide which file(s) this requires, following the architecture above
exactly. Rules:
- Reuse an existing file if the feature clearly extends its existing
  purpose. Otherwise create a new actions/<snake_case_name>.py file for
  the logic.
- If this feature needs a new voice/text-triggered command, main.py MUST
  be included in the file list (for TOOLS schema + dispatch registration).
- If this feature needs any new visible GUI element, ui.py MUST be
  included (PyQt6 — never propose another toolkit or a separate file for it).
- Never invent new top-level folders or non-Python files.
- Return the minimum files actually needed — 1 to 4 total.

Respond with ONLY a JSON array of relative file paths, nothing else, no
markdown fences. Example: ["actions/weather_api.py", "main.py"]"""

    try:
        raw = _generate(prompt)
        cleaned = _strip_fences(raw)
        # Model sometimes wraps the array in a sentence despite instructions —
        # pull out the first [...] block as a safety net.
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        paths = json.loads(match.group(0) if match else cleaned)
        if not isinstance(paths, list) or not paths:
            raise ValueError("empty or non-list response")

        safe_paths, seen = [], set()
        for p in paths:
            if not isinstance(p, str):
                continue
            norm = p.strip().replace("\\", "/")
            try:
                _safe_resolve(norm)
            except ValueError:
                continue
            if norm not in seen:
                seen.add(norm)
                safe_paths.append(norm)
        if safe_paths:
            return safe_paths[:4]
    except Exception:
        pass

    # Fallback — a single new actions/ file, named from the description,
    # matching the project's existing naming convention.
    slug = re.sub(r"[^a-z0-9]+", "_", description.lower()).strip("_")[:40] or "new_feature"
    return [f"actions/{slug}.py"]


def _integration_check() -> str | None:
    """
    Actually imports main.py as a module (not run it — __name__ won't be
    "__main__" so the app never launches) in a fresh subprocess. This is
    the check per-file py_compile can't do: an import statement in
    main.py referencing a name that doesn't actually exist in the file
    it's importing from is syntactically valid Python on both sides
    individually, and only breaks the instant main.py actually loads —
    which is exactly the failure mode that's taken LITE down at startup
    before (a mismatched function name between main.py's import line and
    a newly generated actions/ file). Returns None if the import
    succeeds, else the captured error output.
    """
    main_path = BASE_DIR / "main.py"
    if not main_path.exists():
        return None  # nothing to check against
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import main"],
            cwd=str(BASE_DIR),
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        return "Integration check timed out after 20s (main.py may be doing unexpectedly heavy work at import time)."
    except Exception as e:
        return f"Could not run integration check: {e}"

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "unknown error").strip()
        return "\n".join(err.splitlines()[-15:])   # tail — the actual exception, not the full stack
    return None


def build_feature(description: str, file_paths: list[str] | None = None, max_files: int = 4) -> str:
    """
    Builds or extends one or more files together to implement a described
    feature — the capability 'fix' mode lacks: it can create files that
    don't exist yet, not just edit ones that do.

    If file_paths isn't given (or is empty), placement is inferred
    automatically via infer_feature_files() using a map of LITE's actual
    codebase conventions — no need to ask the user which folder to use.

    Each file is generated/edited and independently backed up + compile-
    verified + auto-rolled-back exactly like fix_file() — a bad result in
    one file never corrupts the others. Files are shown each other's
    content as context so a new module and its wiring into an existing
    file stay consistent with each other.

    After every file individually compiles, the WHOLE batch is also
    integration-checked by actually importing main.py in a subprocess —
    if that fails (e.g. main.py's new import line doesn't match what the
    new file actually exports), every file touched in this build is
    rolled back automatically, not just the one that's technically
    syntactically broken. Each file compiling on its own is necessary but
    not sufficient; this closes that gap.
    """
    inferred = False
    if not file_paths:
        file_paths = infer_feature_files(description)
        inferred = True
    file_paths = file_paths[:max_files]
    if not file_paths:
        return "Couldn't determine where this feature should live, sir — try rephrasing what it should do."

    resolved: list[tuple[str, Path]] = []
    for fp in file_paths:
        try:
            resolved.append((fp, _safe_resolve(fp)))
        except ValueError as e:
            return str(e)

    contents: dict[str, str | None] = {}
    is_new:   dict[str, bool] = {}
    backups:  dict[str, Path] = {}
    for fp, path in resolved:
        if path.exists():
            contents[fp] = path.read_text(encoding="utf-8")
            is_new[fp]   = False
            backups[fp]  = _backup(path)
        else:
            contents[fp] = None
            is_new[fp]   = True

    updated_contents = dict(contents)   # tracks latest content as we go, for cross-file context
    results: list[tuple[str, bool, str]] = []

    for fp, path in resolved:
        sibling_ctx = "\n\n".join(
            f"--- {other_fp} ({'NEW FILE — not yet written' if is_new[other_fp] and updated_contents[other_fp] is None else ('NEW FILE' if is_new[other_fp] else 'EXISTING')}) ---\n"
            f"{(updated_contents[other_fp] or '(not yet generated)')[:2000]}"
            for other_fp, _ in resolved if other_fp != fp
        )
        current = contents[fp] if contents[fp] is not None else "(this file does not exist yet — create it from scratch)"
        truncated = current if len(current) <= MAX_CONTEXT_CHARS * 2 else current[:MAX_CONTEXT_CHARS * 2]

        prompt = f"""You are implementing part of a multi-file feature for an existing Python
desktop app called LITE. Work ONLY on the one file below — other files
involved are shown for context/consistency only, don't repeat their content.

Feature requested: {description}

You are writing: {fp} ({"a NEW file — create it from scratch" if is_new[fp] else "an EXISTING file — extend it"})

Other files involved in this feature:
{sibling_ctx or "(no other files)"}

Current content of {fp}:
{truncated}

Rules:
- Output ONLY the complete file content for {fp}. No explanation, no markdown fences.
- If extending an existing file, preserve everything not related to this feature exactly.
- Keep naming/imports consistent with the other files shown above.
- Do not introduce new external dependencies unless clearly necessary for the feature.

Complete file content for {fp}:"""

        try:
            raw   = _generate(prompt)
            fixed = _strip_fences(raw)
        except Exception as e:
            results.append((fp, False, f"Generation failed: {e}"))
            continue
        if not fixed.strip():
            results.append((fp, False, "Model returned empty content — left unchanged."))
            continue

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fixed, encoding="utf-8")

        compile_err = _compile_check(path)
        if compile_err:
            if is_new[fp]:
                path.unlink(missing_ok=True)   # nothing prior to roll back to
                results.append((fp, False, f"New file didn't compile — removed. Error: {compile_err[:200]}"))
            else:
                shutil.copy2(backups[fp], path)
                results.append((fp, False, f"Edit didn't compile — rolled back. Error: {compile_err[:200]}"))
            continue   # keep updated_contents[fp] at its prior (pre-edit) value for later files' context

        updated_contents[fp] = fixed
        results.append((fp, True, "created" if is_new[fp] else "updated"))

    lines = [f"Feature: {description}", ""]
    if inferred:
        lines.append(f"(No file specified — placed automatically at: {', '.join(file_paths)})")
        lines.append("")
    any_ok = False
    for fp, ok, msg in results:
        lines.append(("  ✅ " if ok else "  ⚠️ ") + f"{fp} — {msg}")
        any_ok = any_ok or ok

    if any_ok:
        integration_error = _integration_check()
        if integration_error:
            # A file compiling on its own doesn't mean the batch actually
            # works together — roll back everything touched here rather
            # than leave LITE unable to start. Newly created files are
            # deleted; edited existing files are restored from backup.
            for fp, path in resolved:
                if is_new[fp]:
                    path.unlink(missing_ok=True)
                elif fp in backups:
                    shutil.copy2(backups[fp], path)
            lines.append("")
            lines.append(
                "⚠️ Integration check failed — main.py doesn't actually import "
                "cleanly with these files together, even though each one "
                "compiled fine on its own. Every file in this batch has been "
                "rolled back automatically; LITE is unaffected and unchanged."
            )
            lines.append(f"Error:\n{integration_error[:600]}")
            return "\n".join(lines)

        lines.append(
            "\n✅ Integration check passed — main.py imports cleanly with these "
            "changes. Restart LITE for them to take effect."
        )
    return "\n".join(lines)


def scan_and_fix_self(target: str = "", max_fixes: int = MAX_AUTO_FIXES) -> str:
    broken, warned = scan_self(target)

    if not broken and not warned:
        scope = f" in {target}" if target else ""
        return f"Scan complete, sir — no issues found{scope}."

    lines = []
    if broken:
        lines.append(f"Found {len(broken)} file(s) with compile errors:")
        for b in broken:
            lines.append(f"  • {b['file']}: {b['compile_error'].splitlines()[0][:140]}")
    if warned:
        lines.append(f"Found {len(warned)} file(s) with lint warnings:")
        for w in warned:
            lines.append(f"  • {w['file']}: {len(w['flake_warnings'])} warning(s)")

    # Fix priority: broken files first (they're actively failing), then
    # lint-only warnings, capped at max_fixes total per run.
    to_fix = broken + warned
    to_fix = to_fix[:max_fixes]

    if not to_fix:
        return "\n".join(lines)

    lines.append("")
    lines.append(f"Attempting fixes for {len(to_fix)} file(s)...")
    fixed_count = 0
    for item in to_fix:
        path = BASE_DIR / item["file"]
        issue_text = (
            f"SyntaxError / compile error:\n{item['compile_error']}"
            if item.get("compile_error")
            else "Lint warnings:\n" + "\n".join(item["flake_warnings"])
        )
        ok, msg, _ = fix_file(path, issue_text)
        lines.append(("  ✅ " if ok else "  ⚠️ ") + msg)
        if ok:
            fixed_count += 1

    remaining = len(broken) + len(warned) - len(to_fix)
    if remaining > 0:
        lines.append(
            f"\n{remaining} more file(s) had issues but weren't touched this run "
            f"(cap is {max_fixes} per request — ask again to continue)."
        )
    if fixed_count:
        lines.append("\nRestart LITE for the fixes to take effect.")

    return "\n".join(lines)


# ── Public entry point ───────────────────────────────────────────────────────

def self_maintain(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    p            = parameters or {}
    mode         = (p.get("mode") or "auto").strip().lower()
    target       = (p.get("file_path") or p.get("target") or "").strip()
    targets      = p.get("file_paths") or ([target] if target else [])
    issue        = (p.get("issue") or p.get("description") or "").strip()
    description  = (p.get("description") or issue).strip()
    max_fixes    = int(p.get("max_fixes") or MAX_AUTO_FIXES)

    def log(msg: str):
        print(f"[SelfMaintain] {msg}")
        if player:
            player.write_log(f"[SelfMaintain] {msg}")

    try:
        if mode == "scan":
            broken, warned = scan_self(target)
            if not broken and not warned:
                scope = f" in {target}" if target else ""
                return f"Scan complete, sir — no issues found{scope}."
            lines = []
            if broken:
                lines.append(f"{len(broken)} file(s) with compile errors:")
                for b in broken:
                    lines.append(f"  • {b['file']}: {b['compile_error'].splitlines()[0][:140]}")
            if warned:
                lines.append(f"{len(warned)} file(s) with lint warnings:")
                for w in warned:
                    lines.append(f"  • {w['file']}: {len(w['flake_warnings'])} warning(s)")
            return "\n".join(lines)

        if mode == "feature":
            if not description:
                return "Please describe the feature you'd like, sir."
            if targets:
                log(f"Building feature across {len(targets)} file(s): {description[:80]}...")
            else:
                log(f"Building feature (auto-placing in codebase): {description[:80]}...")
            return build_feature(description, targets)

        if mode == "fix":
            if not target:
                return "Please tell me which file to fix, sir."
            path = _safe_resolve(target)
            if not path.exists():
                # Not an edit — this is a creation request. Route it through
                # build_feature() instead of refusing outright.
                if not issue:
                    return (
                        f"{target} doesn't exist yet, sir — tell me what it should do "
                        f"and I'll create it (or use mode='feature' with a description)."
                    )
                log(f"{target} doesn't exist — creating it instead of editing...")
                return build_feature(issue, [target])
            log(f"Fixing {path.relative_to(BASE_DIR)}...")
            ok, msg, _ = fix_file(path, issue)
            return msg

        # mode == "auto" (default): scan everything (or a subfolder) and fix
        # up to max_fixes files in one pass.
        log(f"Scanning{' ' + target if target else ' entire codebase'}...")
        return scan_and_fix_self(target, max_fixes=max_fixes)

    except ValueError as e:
        # Path-safety refusals surface as a plain message, not a crash.
        return str(e)
    except Exception as e:
        log(f"Unexpected error: {e}")
        return f"Self-repair hit an unexpected error, sir: {e}"
