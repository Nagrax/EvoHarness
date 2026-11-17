"""Shared database path semantics; no implicit engine or credential loading."""
from pathlib import Path
from sqlalchemy.engine import make_url
from urllib.parse import urlencode


def normalize_database_url(value: str, *, base_dir: Path) -> str:
    try:
        url = make_url(value)
    except Exception:
        raise ValueError("invalid_database_url") from None
    if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
        path = Path(url.database).expanduser()
        path = path if path.is_absolute() else base_dir / path
        # SQLite database names are native filenames. URL.render_as_string in
        # newer SQLAlchemy encodes Windows drive/backslash characters, which
        # would turn an absolute test DB into a different relative filename.
        suffix = "?" + urlencode(dict(url.query)) if url.query else ""
        return url.drivername + ":///" + path.resolve().as_posix() + suffix
    return url.render_as_string(hide_password=False)


def sqlite_database_path(value: str, *, base_dir: Path) -> Path:
    url = make_url(normalize_database_url(value, base_dir=base_dir))
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:") or url.query:
        raise ValueError("unsupported_database_url: requires file-backed SQLite")
    return Path(url.database)
