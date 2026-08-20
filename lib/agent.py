"""질의 계획기 — 자연어를 슬롯으로 번역하고, 툴 선택은 규칙이 한다.

LLM 에게 툴을 고르게 하지 않는 것이 이 설계의 핵심이다. 이유가 둘이다.

  정확도 — "이 질문엔 어느 툴을 써야 하지"를 모델이 매번 판단하면 새는 지점이
  생긴다. 슬롯이 채워지면 툴은 결정론적으로 정해지므로 판단 자체를 없앤다.

  호출 한도 — CLOVA Studio 는 분당 요청·토큰 상한이 있고(테스트앱 60/60,000),
  타 팀이 429 를 실측했다. 라우팅을 규칙으로 하면 그 구간의 LLM 호출이 0 이라
  한 질의당 호출을 답변 생성 1회로 묶을 수 있다. 응답 타임아웃은 300초로
  넉넉하지만 진짜 제약은 시간이 아니라 호출 횟수다.

LLM 은 마지막 문장 생성에만 쓰고, 없으면 템플릿으로 답한다. 그래서 API 키
없이도 파이프라인 전체가 동작한다.
"""
import re
from dataclasses import dataclass, field

from .tools import FOUND, KNOWN_EMPTY, OUT_OF_SCOPE, Tools

ACCOUNT_PATTERNS = [
    (r"매출액|매출|영업수익|수익", "매출액"),
    (r"영업이익|영업손익", "영업이익"),
    (r"당기순이익|순이익", "당기순이익"),
    (r"자산\s*총계|총자산", "자산총계"),
    (r"부채\s*총계|총부채", "부채총계"),
    (r"자본\s*총계|자기자본", "자본총계"),
]
COMPARE_RE = re.compile(r"비교|중\s*(어디|어느|누가)|더\s*(큰|많|높|작|적|낮)|대비|vs|보다")
EVENT_RE = re.compile(r"계약|공급|수주|해지|체결|투자|증자|합병|자기주식|취득|처분|분할|공시했")
OWNER_RE = re.compile(r"지분|보유|대주주|주주|출자|보유율|5%")
CHAIN_RE = re.compile(r"정정|변경\s*이력|수정|바뀐|고쳐")
LIST_RE = re.compile(r"목록|어떤\s*것|무엇이\s*있|몇\s*건|몇\s*개")


@dataclass
class Slots:
    corps: list = field(default_factory=list)
    years: list = field(default_factory=list)
    account: str | None = None
    intent: str = "narrative"
    keyword: str | None = None
    rcept_no: str | None = None
    raw: str = ""


def parse_question(question: str, tools: Tools) -> Slots:
    """자연어 → 슬롯. 규칙 기반이라 LLM 호출이 없다."""
    s = Slots(raw=question)
    s.corps = tools.resolver.find_all(question)
    s.years = sorted({int(y) for y in re.findall(r"(20\d{2})\s*년?", question)
                      if 2000 <= int(y) <= 2030})
    mo = re.search(r"\b(\d{14})\b", question)
    if mo:
        s.rcept_no = mo.group(1)
    for pattern, canon in ACCOUNT_PATTERNS:
        if re.search(pattern, question):
            s.account = canon
            break
    if s.rcept_no or CHAIN_RE.search(question):
        s.intent = "chain"
    elif OWNER_RE.search(question):
        s.intent = "ownership"
    elif s.account and (COMPARE_RE.search(question) or len(s.corps) > 1):
        s.intent = "compare"
    elif s.account:
        s.intent = "fact"
    elif EVENT_RE.search(question):
        s.intent = "event"
    mo = re.search(r"([A-Za-z][A-Za-z .,&]{3,})", question)
    if mo and s.intent in ("event", "chain"):
        s.keyword = mo.group(1).strip()
    return s


def run(question: str, tools: Tools) -> dict:
    """슬롯을 채우고 규칙대로 툴을 호출한다. LLM 호출 없음."""
    slots = parse_question(question, tools)
    results = []
    names = [tools.resolver.name(c) for c in slots.corps]

    # 엔티티가 안 풀리면 즉시 멈춘다. 조용한 0건이 '자료 없음'으로 위장하는 것을
    # 막는 가드레일이다 (감사 F03).
    if not names and slots.intent in ("fact", "compare", "event", "ownership"):
        return {"slots": slots, "results": [], "blocked": True,
                "reason": "질문에서 코퍼스 70개사에 해당하는 기업을 찾지 못했습니다."}

    if slots.intent in ("fact", "compare"):
        year = max(slots.years) if slots.years else 2025
        for name in names:
            results.append(tools.fact_lookup(name, slots.account, year))
        found = [r for r in results if r.ok]
        if len(found) >= 2:
            a, b = found[0].data[0], found[1].data[0]
            expr = f"{a['value']} - {b['value']}"
            results.append(tools.calc(expr))
    elif slots.intent == "event":
        for name in names:
            results.append(tools.contract_lookup(name, keyword=slots.keyword))
            results.append(tools.event_search(name, event_type=None))
    elif slots.intent == "ownership":
        for name in names:
            # 방향은 조사로 가른다. "X가 보유한"이면 X가 보유 주체(holdings),
            # "X 지분을 보유한"이면 X가 대상(holders)이다. 기본값은 holders 인데,
            # 지분공시 질의가 대개 "누가 이 회사를 보유했나"이기 때문이다.
            subject = re.search(
                rf"{re.escape(name)}\s*(?:이|가|은|는)\s*[^.?]{{0,12}}?(?:보유|출자)",
                question)
            direction = "holdings" if subject else "holders"
            # 질문이 특정 보유자를 지목했는지 확인한다 (예: 국민연금공단).
            holder = None
            if direction == "holders":
                compact = question.replace(" ", "")
                for filer in tools.known_filers():
                    fn = (filer or "").replace(" ", "")
                    if len(fn) >= 3 and fn in compact and fn != name.replace(" ", ""):
                        holder = filer
                        break
                if holder is None:
                    for token in re.findall(r"[가-힣]{3,}(?:공단|공사|재단|은행|자산운용|"
                                            r"투자|캐피탈|생명|증권|홀딩스)", question):
                        holder = token
                        break
            results.append(tools.ownership_lookup(name, direction=direction,
                                                  holder=holder))
    elif slots.intent == "chain":
        if slots.rcept_no:
            results.append(tools.chain_resolve(slots.rcept_no))
        for name in names:
            results.append(tools.contract_lookup(name, keyword=slots.keyword))

    # 서사형 보강 — 정형 조회가 비었거나 서술을 요구하는 질문
    if not any(r.ok for r in results) or slots.intent == "narrative":
        results.append(tools.narrative_search(
            question, corp=names[0] if names else None, top_k=5))
    return {"slots": slots, "results": results, "blocked": False, "reason": ""}


def compose(question: str, run_result: dict, tools: Tools, llm=None) -> dict:
    """조회 결과를 답변으로 조립한다.

    llm 이 없으면 템플릿으로 답한다. 어느 쪽이든 근거는 툴 반환 타입이 이미
    강제하고 있어서 누락되지 않는다.
    """
    if run_result["blocked"]:
        return {"answer": f"확인할 수 없습니다. {run_result['reason']}",
                "citations": [], "status": OUT_OF_SCOPE, "think_trace": "엔티티 미해석"}

    results = run_result["results"]
    citations, lines, trace = [], [], []
    statuses = set()
    for r in results:
        statuses.add(r.status)
        trace.append(f"{r.tool}: {r.status}" + (f" — {r.note}" if r.note else ""))
        citations.extend(r.citations)
        if r.status == FOUND:
            lines.append(_render(r))
        elif r.status == KNOWN_EMPTY and r.note:
            lines.append(f"[{r.tool}] {r.note}")
        elif r.status == OUT_OF_SCOPE and r.note:
            lines.append(f"[{r.tool}] {r.note}")

    if FOUND in statuses:
        status = FOUND
    elif KNOWN_EMPTY in statuses:
        status = KNOWN_EMPTY
    else:
        status = OUT_OF_SCOPE

    context = "\n".join(lines) if lines else "조회 결과 없음"
    if llm is not None:
        answer = llm(question, context, citations)
    else:
        answer = _template_answer(status, lines)

    # 근거 검증 — 답변에 나온 숫자가 조회 결과에 실재하는지 대조한다.
    unverified = _verify_numbers(answer, context)
    return {"answer": answer, "citations": _dedupe(citations), "status": status,
            "think_trace": "\n".join(trace), "context": context,
            "unverified_numbers": unverified}


def _render(r) -> str:
    if r.tool == "fact_lookup":
        d = r.data[0]
        return (f"[재무] {d['corp']} {d['year']}년 {d['account']} = "
                f"{d['value']:,.0f} {d['unit']}"
                + (f" ({r.note})" if r.note else ""))
    if r.tool == "calc":
        d = r.data[0]
        return f"[계산] {d['expression']} = {d['value']:,.0f}"
    if r.tool == "contract_lookup":
        out = []
        for d in r.data:
            fields = d["fields"]
            desc = ", ".join(
                f"{k}={v}" for k, v in list(fields.items())[:6] if v and v != "-")
            out.append(f"[계약] {d['status']} (문서 {d['n_docs']}건, "
                       f"{d['first_dt']}~{d['latest_dt']}) {desc}")
        return "\n".join(out)
    if r.tool == "event_search":
        return "[공시] " + "; ".join(
            f"{d['date']} {d['type'] or d['report_nm']}" for d in r.data[:8])
    if r.tool == "ownership_lookup":
        return "[지분] " + "; ".join(
            f"{d['counterpart']}"
            + (f" {d['ratio']}%" if d.get("ratio") else "")
            + f" (보고 {d['n_reports']}건)" for d in r.data[:8])
    if r.tool == "chain_resolve":
        return "[정정이력] " + "; ".join(
            f"{s['direction']}={s['target_rcept_no']} ({s['status']})" for s in r.data)
    if r.tool == "narrative_search":
        return "\n".join(f"[본문] {d['corp']} {d['section']}: {d['text'][:400]}"
                         for d in r.data[:3])
    return f"[{r.tool}] {r.data}"


def _template_answer(status: str, lines: list[str]) -> str:
    if status == OUT_OF_SCOPE:
        return "확인할 수 없습니다.\n" + "\n".join(lines)
    if status == KNOWN_EMPTY:
        return "해당 사실이 확인되지 않았습니다.\n" + "\n".join(lines)
    return "\n".join(lines)


_NUM = re.compile(r"\d[\d,]{3,}")


def _verify_numbers(answer: str, context: str) -> list[str]:
    """답변에 등장한 큰 숫자가 조회 결과에 실재하는지 확인한다.

    감사에서 본 '정답은 맞았는데 근거는 엉뚱한 곳' 유형과, 생성 단계의 수치
    환각을 여기서 잡는다. LLM 호출 없이 로컬로 돌기 때문에 비용이 0이다.
    """
    pool = set(_NUM.findall(context.replace(",", ""))) | set(_NUM.findall(context))
    bad = []
    for token in _NUM.findall(answer):
        if token in pool or token.replace(",", "") in pool:
            continue
        bad.append(token)
    return bad


def _dedupe(citations: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in citations:
        key = c.get("rcept_no")
        if key and key not in seen:
            seen.add(key)
            out.append(c)
    return out


def format_citation(c: dict) -> str:
    """근거 표기 형식을 한 곳에서 고정한다 — 공시명 (공시일, 접수번호)."""
    return (f"{c.get('corp_name','')} {c.get('report_nm','')} "
            f"({c.get('rcept_dt','')}, 접수번호 {c.get('rcept_no','')})").strip()
