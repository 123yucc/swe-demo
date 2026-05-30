#!/usr/bin/env python3
"""
离线 CLI：向长期记忆库添加自定义知识条目。

有两种模式：
  1. 直接操作模式（默认，server 必须停止）：
     直接写入 ChromaDB PersistentClient + custom_knowledge.json。

  2. API 模式（--via-api，server 必须正在运行）：
     通过 POST /add 转发给 experience_server，由 server 完成写入。

用法示例：
  # 直接操作（server 停止时）
  python tools/add_knowledge.py \\
      --title "测试类迁移：新文件优先" \\
      --symptom "FAIL_TO_PASS 测试在 test_new.py 中，但同名测试类存在于 test_old.py" \\
      --guidance "删除 test_old.py 中的旧类，在 test_new.py 中重建它。更新导入引用。"

  # API 模式（server 运行时）
  python tools/add_knowledge.py --via-api \\
      --title "..." --symptom "..." --guidance "..."
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_DB_DIR = Path("workdir/long_term_memory/chroma_db_experience")
DEFAULT_JSON_PATH = Path("workdir/long_term_memory/experience_data.json")
DEFAULT_CUSTOM_PATH = Path("workdir/long_term_memory/custom_knowledge.json")
DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"
DEFAULT_API_URL = "http://127.0.0.1:9030/add"


def add_via_api(title: str, symptom: str, guidance: str, api_url: str) -> str:
    payload = json.dumps({"title": title, "symptom": symptom, "guidance": guidance}).encode()
    req = urllib.request.Request(
        api_url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"ERROR: Cannot connect to {api_url}: {e.reason}", file=sys.stderr)
        print("Is experience_server running? Use direct mode (drop --via-api) if not.", file=sys.stderr)
        sys.exit(1)

    if not result.get("success"):
        print(f"ERROR: Server returned failure: {result.get('error')}", file=sys.stderr)
        sys.exit(1)
    return result["id"]


def add_direct(title: str, symptom: str, guidance: str) -> str:
    try:
        import chromadb
        from sentence_transformers import SentenceTransformer
    except ImportError as e:
        print(f"ERROR: Missing dependency: {e}", file=sys.stderr)
        print("Run: pip install chromadb sentence-transformers", file=sys.stderr)
        sys.exit(1)

    db_dir = Path(os.environ.get("DB_DIR", str(DEFAULT_DB_DIR)))
    custom_path = Path(os.environ.get("CUSTOM_JSON_PATH", str(DEFAULT_CUSTOM_PATH)))
    model_id = os.environ.get("MODEL_PATH", DEFAULT_MODEL)

    if not db_dir.exists():
        print(f"ERROR: ChromaDB not found at {db_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading embedding model {model_id} ...")
    model = SentenceTransformer(model_id, trust_remote_code=True)

    new_id = f"custom-{uuid.uuid4().hex[:12]}"
    record = {"id": new_id, "title": title, "symptom": symptom, "guidance": guidance}

    print("Connecting to ChromaDB ...")
    client = chromadb.PersistentClient(path=str(db_dir))
    collection = client.get_collection(name="experience_knowledge")

    embedding = model.encode([symptom], normalize_embeddings=True).tolist()
    collection.add(
        ids=[new_id],
        embeddings=embedding,
        documents=[symptom],
        metadatas=[{"content_preview": title[:80]}],
    )
    print(f"ChromaDB: inserted id={new_id}")

    if custom_path.exists():
        existing = json.loads(custom_path.read_text(encoding="utf-8"))
    else:
        existing = {}
    existing[new_id] = record
    custom_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"custom_knowledge.json: appended id={new_id}")

    return new_id


def main():
    parser = argparse.ArgumentParser(description="Add a custom knowledge entry to LTM.")
    parser.add_argument("--title",    required=True, help="Short title for the knowledge entry")
    parser.add_argument("--symptom",  required=True, help="Symptom/scenario description (used for embedding)")
    parser.add_argument("--guidance", required=True, help="General engineering guidance (not task-specific)")
    parser.add_argument("--via-api",  action="store_true", help="Use running server's /add endpoint instead of direct write")
    parser.add_argument("--api-url",  default=DEFAULT_API_URL, help=f"Server URL (default: {DEFAULT_API_URL})")
    args = parser.parse_args()

    title    = args.title.strip()
    symptom  = args.symptom.strip()
    guidance = args.guidance.strip()

    if not symptom:
        print("ERROR: --symptom cannot be empty", file=sys.stderr)
        sys.exit(1)

    if args.via_api:
        new_id = add_via_api(title, symptom, guidance, args.api_url)
        print(f"Added via API. id={new_id}")
    else:
        new_id = add_direct(title, symptom, guidance)
        print(f"Done. id={new_id}")


if __name__ == "__main__":
    main()
