"""Gemini API 래퍼. HyperCLOVA X 자리에 데모용으로 Gemini를 사용.

과제 규정상 최종 제출물은 HyperCLOVA X만 허용되므로, 이 파일은 이 데모 버전
전용이며 본 제출용 코드가 아님을 명확히 한다.
"""
import os
import time

from google import genai
from google.genai import types

GEN_MODEL = "gemini-2.5-flash"
EMBED_MODEL = "models/gemini-embedding-001"

_client = None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY가 설정되지 않았습니다. .env 또는 Streamlit secrets를 확인하세요."
            )
        _client = genai.Client(api_key=api_key)
    return _client


def embed_texts(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """배치 임베딩. Gemini embed_content는 요청당 최대 ~100개 텍스트를 받는다."""
    client = get_client()
    out: list[list[float]] = []
    batch_size = 20
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        for attempt in range(3):
            try:
                resp = client.models.embed_content(
                    model=EMBED_MODEL,
                    contents=batch,
                    config=types.EmbedContentConfig(task_type=task_type),
                )
                out.extend([e.values for e in resp.embeddings])
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
    return out


def embed_query(text: str) -> list[float]:
    return embed_texts([text], task_type="RETRIEVAL_QUERY")[0]


def generate_answer(question: str, context_chunks: list[dict]) -> str:
    """context_chunks: [{text, corp_name, report_nm, rcept_dt, section}, ...]"""
    client = get_client()
    context_blocks = []
    for i, c in enumerate(context_chunks, 1):
        header = f"[근거 {i}] {c['corp_name']} · {c['report_nm']} · 공시일 {c['rcept_dt']} · {c['section']}"
        context_blocks.append(f"{header}\n{c['text']}")
    context_str = "\n\n---\n\n".join(context_blocks)

    prompt = f"""당신은 DART 공시 데이터를 근거로 답하는 공시 분석 Agent입니다.

규칙:
- 아래 [근거] 블록에 있는 내용만 사실로 사용하세요. 근거에 없는 내용은 추측하지 말고 "제공된 공시에서 확인되지 않음"이라고 답하세요.
- 수치를 인용할 때는 정확히 그대로 옮기세요.
- 답변 마지막에 "근거:" 항목으로 어떤 [근거 N]을 사용했는지 기업명·공시명과 함께 표시하세요.
- 미래 예측이나 투자 의견을 생성하지 마세요.

[근거]
{context_str}

[질문]
{question}
"""
    resp = client.models.generate_content(model=GEN_MODEL, contents=prompt)
    return resp.text
