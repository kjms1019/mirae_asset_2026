"""공시 Agent 데모 (Streamlit).

index/disclosure.db 만 있으면 API 키 없이 그대로 돌아간다. 라우팅과 조회가 전부
로컬 규칙·SQL 이고, LLM 은 마지막 문장 다듬기에만 선택적으로 쓰이기 때문이다.
GEMINI_API_KEY 를 넣으면 서술형 답변을 생성한다 — 다만 대회 제출물의 LLM 은
HyperCLOVA X 만 허용되므로 이 경로는 팀 내부 확인용이다.
"""
import json
import os

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from lib import agent, store
from lib.tools import FOUND, KNOWN_EMPTY, OUT_OF_SCOPE, Tools

st.set_page_config(page_title="공시 Agent", page_icon="📊", layout="wide")

EXAM_PATH = os.path.join(os.path.dirname(__file__), "eval", "mock_exam_v2.json")
STATUS_UI = {
    FOUND: ("확인됨", "✅"),
    KNOWN_EMPTY: ("0건으로 확인", "⚪"),
    OUT_OF_SCOPE: ("확인 불가 — 범위 밖", "🚫"),
}


@st.cache_resource
def get_tools():
    conn = store.connect()
    return Tools(conn)


@st.cache_data
def get_stats():
    conn = store.connect()
    stats = store.counts(conn)
    stats["corrections_dangling"] = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE edge_type='correction'"
        " AND status='dangling_out_of_corpus'").fetchone()[0]
    stats["contract_clusters"] = conn.execute(
        "SELECT COUNT(*) FROM contracts WHERE n_docs>1").fetchone()[0]
    stats["unsupported"] = conn.execute(
        "SELECT COUNT(*) FROM docs WHERE parse_status='unsupported_format'").fetchone()[0]
    return stats


@st.cache_data
def get_exam():
    if not os.path.exists(EXAM_PATH):
        return []
    with open(EXAM_PATH, encoding="utf-8") as f:
        return json.load(f)["items"]


def gemini_writer():
    """선택적 LLM. 없으면 None 을 돌려주고 템플릿 답변으로 떨어진다."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key and hasattr(st, "secrets") and "GEMINI_API_KEY" in st.secrets:
        key = st.secrets["GEMINI_API_KEY"]
    if not key:
        return None
    from google import genai

    client = genai.Client(api_key=key)

    def write(question, context, citations):
        cite_lines = "\n".join(agent.format_citation(c) for c in citations[:8])
        prompt = f"""당신은 DART 공시를 근거로만 답하는 공시 분석 Agent입니다.

규칙:
- 아래 [조회결과]에 있는 값만 사용하세요. 없는 내용은 절대 만들지 마세요.
- 조회결과가 "확인 불가"나 "0건"이면 그 사실을 그대로, 이유와 함께 전하세요.
- 수치는 그대로 옮기고 단위를 반드시 표기하세요. 직접 계산하지 마세요.
- 마지막에 "근거:" 항목으로 공시명·공시일·접수번호를 표기하세요.
- 미래 예측이나 투자 의견을 만들지 마세요.

[조회결과]
{context}

[근거 공시]
{cite_lines}

[질문]
{question}
"""
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return resp.text

    return write


tools = get_tools()
stats = get_stats()
exam = get_exam()

st.title("📊 공시 Agent")
st.caption("제10회 2026 미래에셋증권 AI Festival · 공시 Agent 과제")

with st.sidebar:
    st.subheader("적재 현황")
    st.metric("문서", f"{stats['docs']:,}")
    c1, c2 = st.columns(2)
    c1.metric("사실 주장", f"{stats['claims']:,}")
    c2.metric("관계", f"{stats['edges']:,}")
    c1.metric("청크", f"{stats['chunks']:,}")
    c2.metric("계약 클러스터", f"{stats['contract_clusters']:,}")
    st.divider()
    st.caption(
        f"원본이 코퍼스 밖인 정정 **{stats['corrections_dangling']}건** · "
        f"XML 미제공 **{stats['unsupported']}건** — 이 문서들에 대한 질의는 "
        f"'확인 불가'로 답하는 것이 정답입니다.")
    st.divider()
    use_llm = st.toggle("LLM으로 문장 생성", value=False,
                        help="끄면 조회 결과를 그대로 보여줍니다. "
                             "켜려면 GEMINI_API_KEY가 필요합니다(내부 확인용).")
    show_trace = st.toggle("추론 경로 보기", value=True)

tab_ask, tab_exam, tab_design = st.tabs(["질의", "모의고사", "설계"])

with tab_ask:
    if "question" not in st.session_state:
        st.session_state.question = ""
    question = st.text_input(
        "질문", key="question",
        placeholder="예: 삼성전자와 SK하이닉스 중 2025년 영업이익이 더 큰 곳은?")
    go = st.button("질의", type="primary")

    if go and question:
        writer = gemini_writer() if use_llm else None
        if use_llm and writer is None:
            st.warning("GEMINI_API_KEY가 없어 템플릿 답변으로 진행합니다.")
        with st.spinner("조회 중..."):
            run = agent.run(question, tools)
            out = agent.compose(question, run, tools, llm=writer)

        label, icon = STATUS_UI.get(out["status"], ("", ""))
        st.markdown(f"### {icon} {label}")
        st.markdown(out["answer"])

        if out.get("unverified_numbers"):
            st.error(
                "근거 검증 실패 — 답변에 조회 결과에 없는 수치가 있습니다: "
                + ", ".join(out["unverified_numbers"][:6])
                + "  이 답변은 그대로 신뢰하면 안 됩니다.")

        if out["citations"]:
            st.markdown("#### 근거 공시")
            for c in out["citations"][:8]:
                st.markdown(f"- {agent.format_citation(c)}")

        if show_trace:
            slots = run["slots"]
            with st.expander("추론 경로", expanded=False):
                st.markdown("**해석된 슬롯**")
                st.json({"기업": [tools.resolver.name(c) for c in slots.corps],
                         "연도": slots.years, "계정": slots.account,
                         "의도": slots.intent, "키워드": slots.keyword,
                         "접수번호": slots.rcept_no})
                st.markdown("**툴 호출**")
                st.code(out["think_trace"] or "(없음)")
                st.markdown("**조회 결과 원문**")
                st.code(out.get("context", "")[:4000])

with tab_exam:
    st.markdown(
        "감사(EDA v2)에서 발견한 데이터 함정을 문항으로 옮긴 검증셋입니다. "
        "각 문항은 함정 유형 하나를 격리해 테스트하므로, 틀리면 어느 관계에서 "
        "깨졌는지가 바로 드러납니다.")
    if not exam:
        st.info("eval/mock_exam_v2.json 을 찾지 못했습니다.")
    else:
        types = sorted({it["trap_type"] for it in exam})
        pick = st.multiselect("함정 유형", types, default=types)
        for item in [i for i in exam if i["trap_type"] in pick]:
            with st.container(border=True):
                head = f"**{item['id']}** · `{item['trap_type']}` · {item['answer_type']}"
                st.markdown(head)
                st.markdown(f"**{item['question']}**")
                cols = st.columns([1, 4])
                if cols[0].button("실행", key=f"run_{item['id']}"):
                    st.session_state.question = item["question"]
                    st.rerun()
                with cols[1].expander("정답과 채점 기준"):
                    if item.get("expected_answer"):
                        st.success(item["expected_answer"])
                    for point in item.get("grading_rubric", []):
                        st.markdown(f"- {point}")
                    if item.get("distractor"):
                        st.warning(f"흔한 오답 — {item['distractor']}")
                    grading = item.get("grading", {})
                    if grading.get("auto_fail"):
                        st.error(f"자동 실패 조건 — {grading['auto_fail']}")

with tab_design:
    st.markdown("""
### 저장 구조

6개 유형 = (데이터 성격 3) × (시간 의미론 2) 인데, **테이블은 6개가 아니라 3개**입니다.
데이터 성격 축은 테이블을 가르고, 상태/사건 축은 테이블이 아니라 컬럼과 조회 규칙을
가르기 때문입니다.

| | 상태 (기간 귀속 · as-of 조회) | 사건 (시점 발생 · 순서·인과) |
|---|---|---|
| **완전정형** | `claims.valid_year` — 재무 팩트 | `claims.event_dt` — 계약금액·체결일 |
| **서사형** | `chunks` (periodic) — 사업의 내용 | `chunks` (exchange·major) — 계약 내용 |
| **얕은 관계** | `edges` (ownership) — 지분 현황 | `edges` (correction) — 정정 체인 |

`base_year` 보유 여부가 상태와 사건을 가릅니다. 정기공시 1,054건에는 100% 있고
나머지 3,150건에는 100% 없어서, 이 축은 우리가 만든 게 아니라 데이터에 이미
있던 것입니다.

### 정정을 특수 처리하지 않습니다

정정본이 주장하는 값을 그 문서의 접수일로 `claims`에 쌓아두고, 조회 시점에 가장
늦은 주장을 채택합니다. 그래서 **원본을 찾아 연결할 필요가 없고**, 원본이 코퍼스
밖인 정정 280건도 최신 상태만큼은 정확하게 나옵니다.

### LLM은 툴을 고르지 않습니다

질문을 슬롯으로 번역하는 것까지가 언어 처리이고, 어느 툴을 부를지는 규칙이 정합니다.
정확도 때문이기도 하고, CLOVA Studio의 분당 호출·토큰 한도 때문이기도 합니다.
라우팅에 LLM 호출이 0이라 한 질의당 호출을 답변 생성 1회로 묶을 수 있습니다.

### 3값 반환

모든 툴이 **확인됨 / 0건으로 확인 / 범위 밖**을 구분해 돌려줍니다. 빈 결과로
뭉뚱그리면 "그런 일이 없었다"와 "우리가 모른다"를 섞게 되고, 그게 평가지표
'정보한계대응'에서 바로 감점됩니다.
""")
