import argparse
import json


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--available", type=nonnegative, default=7)
    parser.add_argument("--reserved", type=nonnegative, default=3)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    if args.format == "json":
        print(json.dumps({"available": args.available, "reserved": args.reserved}, separators=(",", ":")))
    else:
        print(f"Остаток: {args.available}\nРезерв: {args.reserved}")


if __name__ == "__main__":
    main()
