"""공시 Agent 데모 — Streamlit 앱.

주의: 대회 규정상 최종 제출물의 LLM은 HyperCLOVA X만 허용된다. 이 앱은 팀 내부
데모/평가셋 검증용으로 Gemini를 사용한 버전이며, 본선/예선 제출용 코드가 아니다.
"""
import os

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from lib.gemini_client import embed_query, generate_answer
from lib.index import load_index, search

st.set_page_config(page_title="공시 Agent 데모", page_icon="📊", layout="centered")


@st.cache_resource
def _load_index():
    return load_index()


def get_api_key() -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key and hasattr(st, "secrets") and "GEMINI_API_KEY" in st.secrets:
        key = st.secrets["GEMINI_API_KEY"]
        os.environ["GEMINI_API_KEY"] = key
    return key


st.title("📊 공시 Agent 데모")
st.caption(
    "제10회 2026 미래에셋증권 AI Festival · 공시 Agent 과제 데모 버전 "
    "(LLM: Gemini — 최종 제출은 HyperCLOVA X 사용 예정)"
)

if not get_api_key():
    st.error("GEMINI_API_KEY가 설정되지 않았습니다. .env 파일 또는 Streamlit secrets를 확인하세요.")
    st.stop()

try:
    chunks, embeddings = _load_index()
except FileNotFoundError:
    st.error("인덱스 파일이 없습니다. 먼저 `python build_index.py`를 실행해주세요.")
    st.stop()

companies = sorted({c["corp_name"] for c in chunks})
st.info(f"현재 데모는 **{len(companies)}개사**의 최신 사업보고서만 인덱싱된 상태입니다: {', '.join(companies)}")

with st.sidebar:
    st.header("데모 범위")
    st.write(f"청크 수: {len(chunks)}")
    st.write("섹션: 회사의 개요 · 사업의 내용 · 요약재무정보 · 경영진단 및 분석의견")
    st.divider()
    top_k = st.slider("검색 근거 개수 (top-k)", 3, 10, 6)
    show_context = st.checkbox("검색된 근거 원문 보기", value=False)

question = st.text_input(
    "질문을 입력하세요",
    placeholder="예: 삼성전자와 SK하이닉스 중 2025년 매출액이 더 큰 곳은?",
)

if st.button("질문하기", type="primary") and question:
    with st.spinner("공시 검색 중..."):
        q_emb = embed_query(question)
        results = search(q_emb, chunks, embeddings, k=top_k)

    with st.spinner("답변 생성 중..."):
        answer = generate_answer(question, results)

    st.markdown("### 답변")
    st.markdown(answer)

    if show_context:
        st.markdown("### 검색된 근거")
        for i, r in enumerate(results, 1):
            with st.expander(
                f"[{i}] {r['corp_name']} · {r['report_nm']} · {r['section']} (유사도 {r['score']:.3f})"
            ):
                st.text(r["text"])
