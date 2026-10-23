"""Source-checkout launcher; also works before pip install -e ."""
from pathlib import Path

__path__ = [str(Path(__file__).resolve().parent / "src" / "jp_screening")]
if __name__ == "__main__":
    from jp_screening.cli import main
    raise SystemExit(main())
