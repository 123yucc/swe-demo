"""Main entrypoint shim.

Phase A: delegate CLI logic to src.app.cli.
"""

from src.app.cli import main


if __name__ == "__main__":
    main()
