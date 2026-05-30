#!/usr/bin/env python3
"""
Experience Server (adapted from QuantaAlpha/MemGovern @ main).

Provides semantic search and experience retrieval endpoints.
Schema: id, title, symptom, guidance (repo/issue_id removed).

Configuration via environment variables:
  DB_DIR          path to chroma_db_experience/
  JSON_DATA_PATH  path to experience_data.json
  MODEL_PATH      sentence-transformers model id or local path
                  (default Qwen/Qwen3-Embedding-0.6B)
  HOST            default 0.0.0.0
  PORT            default 9030
"""
from flask import Flask, request, jsonify
import chromadb
from chromadb import Documents, EmbeddingFunction, Embeddings
from sentence_transformers import SentenceTransformer
import json
import os
import uuid
import logging
from pathlib import Path

# ================= Configuration =================
DB_DIR = os.environ.get("DB_DIR", "./chroma_db_experience")
JSON_DATA_PATH = os.environ.get("JSON_DATA_PATH", "./experience_data.json")
MODEL_PATH = os.environ.get("MODEL_PATH", "Qwen/Qwen3-Embedding-0.6B")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "9030"))
# =================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

collection = None
experience_data = {}


class LocalQwenEmbedding(EmbeddingFunction):
    def __init__(self, model_path):
        self.model = SentenceTransformer(model_path, trust_remote_code=True)

    def __call__(self, input: Documents) -> Embeddings:
        embeddings = self.model.encode(input, normalize_embeddings=True)
        return embeddings.tolist()


def init_service():
    global collection, experience_data
    try:
        logger.info("Loading Knowledge Base JSON...")
        logger.info(f"JSON_DATA_PATH={JSON_DATA_PATH}")
        if os.path.exists(JSON_DATA_PATH):
            with open(JSON_DATA_PATH, 'r', encoding='utf-8') as f:
                experience_data = json.load(f)
        else:
            logger.error(f"JSON file not found at {JSON_DATA_PATH}")
            return False

        # custom_knowledge.json is intentionally NOT loaded here. Custom
        # rules are reached via the tag-tree router path
        # (src/memory/custom_route.py) — keeping them out of ChromaDB
        # avoids polluting the open-source bug-shape index with
        # meta-methodology entries that the embedding model cannot rank
        # well anyway.

        logger.info("Initializing ChromaDB...")
        logger.info(f"DB_DIR={DB_DIR}")
        logger.info(f"MODEL_PATH={MODEL_PATH}")
        emb_fn = LocalQwenEmbedding(MODEL_PATH)

        client = chromadb.PersistentClient(path=DB_DIR)
        collection = client.get_collection(
            name="experience_knowledge",
            embedding_function=emb_fn,
        )
        logger.info("Service Initialized Successfully.")
        return True
    except Exception as e:
        logger.error(f"Initialization Failed: {e}")
        return False


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "docs_count": collection.count() if collection else 0})


@app.route('/search', methods=['POST'])
def search():
    """POST {"query": "...", "top_k": 5}"""
    try:
        data = request.get_json()
        query = data.get('query', '')
        top_k = data.get('top_k', 5)

        if not query:
            return jsonify({"success": False, "error": "Missing query"}), 400
        if collection is None:
            return jsonify({"success": False, "error": "Server not initialized"}), 500

        try:
            logger.info(f"[TOOL] /search top_k={top_k} query={str(query)[:200]!r}")
        except Exception:
            pass

        results = collection.query(query_texts=[query], n_results=top_k)

        formatted = []
        if results['ids']:
            for i, doc_id in enumerate(results['ids'][0]):
                meta = results['metadatas'][0][i]
                score = results['distances'][0][i]
                exp_item = experience_data.get(doc_id, {}) if isinstance(experience_data, dict) else {}
                formatted.append({
                    "id": doc_id,
                    "score": score,
                    "content_preview": meta.get('content_preview', ''),
                    "symptom": exp_item.get("symptom", ""),
                })

        return jsonify({"success": True, "results": formatted})
    except Exception as e:
        logger.error(f"Search Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/get_experience', methods=['GET'])
def get_experience():
    """GET ?id=<unique_id>"""
    unique_id = request.args.get('id')
    if not unique_id:
        return jsonify({"success": False, "error": "Missing id parameter"}), 400
    try:
        logger.info(f"[TOOL] /get_experience id={str(unique_id)[:200]!r}")
    except Exception:
        pass

    item = experience_data.get(unique_id)
    if item:
        return jsonify({
            "success": True,
            "data": {
                "id": unique_id,
                "title": item.get("title", ""),
                "symptom": item.get("symptom", ""),
                "guidance": item.get("guidance", ""),
            },
        })
    return jsonify({"success": False, "error": "ID not found"}), 404


@app.route('/add', methods=['POST'])
def add_knowledge():
    """POST {"title": "...", "symptom": "...", "guidance": "...",
             "tags": {"repo_type": ..., "task_type": ..., "change_shape": ...}}

    Append a custom knowledge entry to custom_knowledge.json. The entry
    is reached via the tag-tree router (src/memory/custom_route.py),
    NOT via ChromaDB — so this handler does not touch the Chroma
    collection. Each tag axis must be either a list of enum values or
    null (wildcard); see src/models/custom_rules.py for the enums.
    """
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    symptom = (data.get("symptom") or "").strip()
    guidance = (data.get("guidance") or "").strip()
    tags = data.get("tags") or {}

    if not symptom:
        return jsonify({"success": False, "error": "symptom is required"}), 400

    new_id = f"custom-{uuid.uuid4().hex[:12]}"
    record = {
        "id": new_id,
        "title": title,
        "symptom": symptom,
        "guidance": guidance,
        "tags": tags,
    }

    custom_path = Path(JSON_DATA_PATH).parent / "custom_knowledge.json"
    try:
        if custom_path.exists():
            existing = json.loads(custom_path.read_text(encoding="utf-8"))
        else:
            existing = {}
        existing[new_id] = record
        custom_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.error(f"custom_knowledge.json write failed: {e}")
        return jsonify({"success": False, "error": f"File write failed: {e}"}), 500

    logger.info(f"[/add] New custom knowledge added: id={new_id}")
    return jsonify({"success": True, "id": new_id})


if __name__ == '__main__':
    if init_service():
        logger.info(f"Starting Experience Server on {HOST}:{PORT}")
        app.run(host=HOST, port=PORT)
    else:
        logger.error("Failed to start service.")
