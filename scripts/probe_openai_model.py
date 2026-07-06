import argparse
import os
import sys

from openai import OpenAI


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="https://claude.buzz7.top/v1")
    args = parser.parse_args()

    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=args.base_url,
    )
    response = client.responses.create(
        model=args.model,
        input=[{"role": "user", "content": "Reply with pong only."}],
        max_output_tokens=16,
    )
    text = getattr(response, "output_text", "") or str(response)
    print("ok", args.model, text.replace("\n", " ")[:120])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("error", type(exc).__name__, str(exc)[:300], file=sys.stderr)
        raise
