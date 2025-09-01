# env_utils.py
from pathlib import Path
from typing import Final
import os

try:
    from dotenv import load_dotenv  # pip install python-dotenv
except Exception:
    load_dotenv = None  # type: ignore[assignment]

def load_env(dotenv_basename: str = ".env") -> None:
    """
    Load .env that lives next to the caller script.
    Works regardless of current working directory.
    """
    if load_dotenv is None:
        return
    # Resolve the .env path relative to the script that imports this
    # We can infer caller's folder via __file__ of this module's importer, but
    # simpler: look in CWD first, then alongside this file.
    from inspect import stack
    caller = Path(stack()[1].filename).resolve()
    env_path = caller.with_name(dotenv_basename)
    if not env_path.exists():
        # fallback to this module's folder
        here = Path(__file__).resolve().parent
        env_path = here / dotenv_basename
    load_dotenv(dotenv_path=env_path)

def require_env(name: str) -> str:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        raise EnvironmentError(f"Missing required environment variable: {name}")
    return str(v)

def get_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y")
