from __future__ import annotations

import os
from pathlib import Path


def load_env_file(
    filename: str | os.PathLike[str] = ".env",
    *,
    override: bool = True,
    required: bool = False,
) -> None:
    """
    Load environment variables from a .env-style file.

    Supported syntax:

        # comment
        KEY=value
        KEY="quoted value"
        KEY='quoted value'
        KEY=value # inline comment
        export KEY=value

    Args:
        filename: Path to the environment file.
        override: If True, values from the file replace existing
                  environment variables. Defaults to False.
        required: If True, raise FileNotFoundError when the file
                  does not exist. Otherwise, silently return.

    Raises:
        FileNotFoundError: If required=True and the file is missing.
        ValueError: If a non-empty line is malformed.
        OSError: If the file cannot be read.
    """
    path = Path(filename)

    if not path.is_file():
        if required:
            raise FileNotFoundError(f"Environment file not found: {path}")
        return

    with path.open("r", encoding="utf-8") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.strip()

            # Empty line or full-line comment.
            if not line or line.startswith("#"):
                continue

            # Support: export KEY=value
            if line.startswith("export "):
                line = line[7:].lstrip()

            # Split only on the first '=' so values can contain '='.
            if "=" not in line:
                raise ValueError(
                    f"Invalid environment variable at "
                    f"{path}:{line_number}: {raw_line.rstrip()!r}"
                )

            key, value = line.split("=", 1)

            key = key.strip()
            value = value.strip()

            if not key:
                raise ValueError(
                    f"Empty environment variable name at "
                    f"{path}:{line_number}"
                )

            # Validate the variable name.
            if not (
                key[0].isalpha() or key[0] == "_"
            ) or not all(
                char.isalnum() or char == "_"
                for char in key
            ):
                raise ValueError(
                    f"Invalid environment variable name {key!r} "
                    f"at {path}:{line_number}"
                )

            # Remove matching surrounding quotes.
            if len(value) >= 2 and value[0] == value[-1]:
                if value[0] in {'"', "'"}:
                    value = value[1:-1]

            # Remove simple inline comments from unquoted values.
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()

            if override:
                os.environ[key] = value
            else:
                os.environ.setdefault(key, value)
