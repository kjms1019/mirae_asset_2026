"""청크 + 임베딩을 로컬 파일(JSON + npy)로 저장/로드하고 코사인 유사도로 검색.

데모 규모(10개사 × 몇 개 섹션)에서는 전용 벡터 DB 없이 numpy 인메모리 검색으로
충분하다 — 청크 수가 수백 개 수준이라 별도 인덱싱 구조(FAISS 등)가 불필요.
"""
import json
import os

import numpy as np

INDEX_DIR = os.path.join(os.path.dirname(__file__), "..", "index")
CHUNKS_PATH = os.path.join(INDEX_DIR, "chunks.json")
EMB_PATH = os.path.join(INDEX_DIR, "embeddings.npy")


def save_index(chunks: list[dict], embeddings: list[list[float]]) -> None:
    os.makedirs(INDEX_DIR, exist_ok=True)
    with open(CHUNKS_PATH, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)
    arr = np.array(embeddings, dtype=np.float32)
    arr = arr / np.linalg.norm(arr, axis=1, keepdims=True)  # 미리 정규화해서 검색시 dot product만 하면 되게
    np.save(EMB_PATH, arr)


def load_index() -> tuple[list[dict], np.ndarray]:
    with open(CHUNKS_PATH, encoding="utf-8") as f:
        chunks = json.load(f)
    emb = np.load(EMB_PATH)
    return chunks, emb


def search(query_embedding: list[float], chunks: list[dict], emb: np.ndarray, k: int = 6) -> list[dict]:
    q = np.array(query_embedding, dtype=np.float32)
    q = q / np.linalg.norm(q)
    scores = emb @ q
    top_idx = np.argsort(-scores)[:k]
    results = []
    for i in top_idx:
        item = dict(chunks[int(i)])
        item["score"] = float(scores[i])
        results.append(item)
    return results
