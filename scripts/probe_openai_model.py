import argparse
import json
import os
import sys
from pathlib import Path

import httpx
from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import src.config  # noqa: F401 - side-effect: load .env into os.environ
from src.agents._openai_native import _api_key, _base_url, _ssl_verify_setting


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--base-url", default="")
    args = parser.parse_args()

    model_name = args.model
    if args.manifest:
        document = json.loads(args.manifest.read_text(encoding="utf-8"))
        models = document.get("models") or []
        if not models:
            parser.error("manifest contains no models")
        model = models[0]
        manifest_env = dict((document.get("defaults") or {}).get("env") or {})
        manifest_env.update(model.get("env") or {})
        os.environ.update({str(key): str(value) for key, value in manifest_env.items()})
        model_name = model.get("model") or model.get("name")
    if not model_name:
        parser.error("--model is required when --manifest is not provided")

    kwargs = {
        "api_key": _api_key(),
        "base_url": args.base_url or _base_url() or "https://165.154.193.90/v1",
    }
    verify = _ssl_verify_setting()
    if verify is not True:
        kwargs["http_client"] = httpx.Client(verify=verify)

    client = OpenAI(**kwargs)
    response = client.responses.create(
        model=model_name,
        input=[{"role": "user", "content": "Reply with pong only."}],
        max_output_tokens=16,
    )
    text = getattr(response, "output_text", "") or str(response)
    print("ok", model_name, text.replace("\n", " ")[:120])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("error", type(exc).__name__, str(exc)[:300], file=sys.stderr)
        raise
