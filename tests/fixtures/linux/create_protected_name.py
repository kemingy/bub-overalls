"""Demonstrate that a new protected name is writable in the creating command."""

from pathlib import Path


def main() -> None:
    path = Path(".ssh")
    path.mkdir()
    (path / "key").write_text("new")


if __name__ == "__main__":
    main()
