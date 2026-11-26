"""Import provided historical spreadsheets into the configured local database."""
import argparse
import json

from app.pair_import import import_history


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mentor", required=True)
    parser.add_argument("--intern", required=True)
    args = parser.parse_args()
    print(json.dumps(import_history(args.mentor, args.intern), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
