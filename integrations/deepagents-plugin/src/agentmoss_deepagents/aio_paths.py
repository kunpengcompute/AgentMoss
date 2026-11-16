"""POSIX sandbox paths, independent of the client operating system."""

import posixpath
import re


def normalize_aio_path(path: str, working_dir: str) -> str:
    if (
        not isinstance(path, str)
        or not path
        or "\x00" in path
        or "\\" in path
        or path.startswith("~")
        or re.match(r"^[A-Za-z]:", path)
    ):
        raise ValueError("AIO paths must be nonempty POSIX paths without NUL, tilde or Windows syntax")
    # This is lexical normalization, not a claim about remote symlinks or confinement.
    return "/" + posixpath.normpath(posixpath.join(working_dir, path)).lstrip("/")
