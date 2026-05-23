"""Allow `python -m aitp_playground.cli` to work via __main__."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
