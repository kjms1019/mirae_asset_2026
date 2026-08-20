"""조회 툴 — LLM 이 부르는 여섯 개의 원시연산.

설계 규칙 두 가지가 강하게 걸려 있다.

  1. 모든 툴은 3값을 반환한다. FOUND / KNOWN_EMPTY / OUT_OF_SCOPE.
     "0건임을 확인함"과 "우리 범위 밖이라 확인 불가"는 완전히 다른 답이고,
     빈 결과로 뭉뚱그리면 LLM 이 이 둘을 구분할 방법이 없다. 평가지표
     '정보한계대응'이 걸린 지점이라 프롬프트가 아니라 타입으로 가른다.

  2. 반환값에 근거(citations)가 필수다. 근거 표시 요건을 프롬프트로 타이르지
     않고 자료구조가 강제한다.

상태성 조회는 as_of(인지 시점)를 받고 기본값이 "최신 정정 반영"이다. 라우터에게
"이 질문은 정정 확인이 필요한가"를 판단시키면 반드시 새기 때문에, 판단 자체를
없앤다.
"""
import json
import re
from dataclasses import dataclass, field

from .entity import Resolver

FOUND = "found"
KNOWN_EMPTY = "known_empty"
OUT_OF_SCOPE = "out_of_scope"

#: 코퍼스 수집 범위 (README + 감사 F12)
CORPUS_FIRST_DT = 20230101
CORPUS_LAST_DT = 20260630
#: 정기공시 요약재무정보는 최근 3개년 비교표를 실으므로 2021년까지는 값이 존재한다
EARLIEST_FISCAL_YEAR = 2021


@dataclass
class Result:
    status: str
    data: list = field(default_factory=list)
    citations: list = field(default_factory=list)
    note: str = ""
    tool: str = ""

    @property
    def ok(self) -> bool:
        return self.status == FOUND

    def to_dict(self) -> dict:
        return {"tool": self.tool, "status": self.status, "data": self.data,
                "citations": self.citations, "note": self.note}


def _cite(row) -> dict:
    return {"rcept_no": row["rcept_no"], "report_nm": row["report_nm"],
            "rcept_dt": row["rcept_dt"], "corp_name": row["corp_name"],
            "section": row["section"] if "section" in row.keys() else None}


class Tools:
    def __init__(self, conn):
        self.conn = conn
        self.resolver = Resolver(conn)

    # ── 공통 ────────────────────────────────────────────────────────────
    def _corp(self, name):
        cid = self.resolver.resolve(name) if name else None
        return cid

    def _corp_row(self, corp_id):
        return self.conn.execute("SELECT * FROM corps WHERE corp_id=?",
                                 (corp_id,)).fetchone()

    def _docmeta(self, rcept_nos):
        if not rcept_nos:
            return []
        q = ("SELECT d.rcept_no, d.report_nm, d.rcept_dt, c.corp_name FROM docs d"
             " JOIN corps c ON c.corp_id=d.corp_id WHERE d.rcept_no IN (%s)"
             % ",".join("?" * len(rcept_nos)))
        return [dict(r) for r in self.conn.execute(q, tuple(rcept_nos))]

    # ── 1. 재무 팩트 (상태 x 정형) ──────────────────────────────────────
    def fact_lookup(self, corp: str, account: str, year: int,
                    as_of: int | None = None) -> Result:
        """회사·연도·계정과목 → 값. 같은 사실에 여러 주장이 있으면 최신을 채택한다."""
        cid = self._corp(corp)
        if not cid:
            return Result(OUT_OF_SCOPE, tool="fact_lookup",
                          note=f"'{corp}'을(를) 코퍼스의 기업으로 특정하지 못했습니다.")
        if year and year < EARLIEST_FISCAL_YEAR:
            return Result(OUT_OF_SCOPE, tool="fact_lookup",
                          note=f"{year}년은 코퍼스 수집 범위 밖입니다. "
                               f"정기공시는 {EARLIEST_FISCAL_YEAR}년까지만 비교표로 확인됩니다.")
        params = [cid, account]
        sql = ("SELECT c.*, d.report_nm, co.corp_name FROM claims c"
               " JOIN docs d ON d.rcept_no=c.asserted_by"
               " JOIN corps co ON co.corp_id=c.subject_id"
               " WHERE c.subject_id=? AND c.predicate=?")
        if year:
            sql += " AND c.valid_year=?"
            params.append(year)
        if as_of:
            sql += " AND c.asserted_at<=?"
            params.append(as_of)
        sql += " ORDER BY c.asserted_at DESC, c.claim_id DESC"
        rows = self.conn.execute(sql, params).fetchall()
        if not rows:
            corp_row = self._corp_row(cid)
            return Result(KNOWN_EMPTY, tool="fact_lookup",
                          note=f"{corp_row['corp_name']}의 {year}년 '{account}' 값을 "
                               f"요약재무정보에서 찾지 못했습니다. 계정과목명이 다를 수 있습니다.")
        best = rows[0]
        superseded = len({r["asserted_by"] for r in rows}) > 1
        return Result(
            FOUND, tool="fact_lookup",
            data=[{"corp": best["corp_name"], "account": account, "year": year,
                   "value": best["value_num"], "unit": best["unit"],
                   "superseded_claims": len(rows) - 1}],
            citations=[{"rcept_no": best["asserted_by"], "report_nm": best["report_nm"],
                        "rcept_dt": best["asserted_at"], "corp_name": best["corp_name"],
                        "section": best["section"]}],
            note=("이후 정정본이 있어 최신 주장을 채택했습니다." if superseded else ""))

    # ── 2. 서사형 검색 (상태·사건 x 서사형) ─────────────────────────────
    def narrative_search(self, query: str, corp: str | None = None,
                         doc_group: str | None = None, top_k: int = 6,
                         bm25=None, chunk_ids=None) -> Result:
        cid = self._corp(corp) if corp else None
        if corp and not cid:
            return Result(OUT_OF_SCOPE, tool="narrative_search",
                          note=f"'{corp}'을(를) 특정하지 못했습니다.")
        sql = ("SELECT ch.chunk_id, ch.text, ch.section, ch.rcept_no, ch.doc_group,"
               " d.report_nm, d.rcept_dt, co.corp_name FROM chunks ch"
               " JOIN docs d ON d.rcept_no=ch.rcept_no"
               " JOIN corps co ON co.corp_id=ch.corp_id WHERE 1=1")
        params = []
        if cid:
            sql += " AND ch.corp_id=?"
            params.append(cid)
        if doc_group:
            sql += " AND ch.doc_group=?"
            params.append(doc_group)
        rows = [dict(r) for r in self.conn.execute(sql, params)]
        if not rows:
            return Result(KNOWN_EMPTY, tool="narrative_search",
                          note="해당 조건의 본문이 색인에 없습니다.")
        from .search import BM25
        local = BM25([r["text"] for r in rows])
        hits = local.search(query, top_k=top_k)
        if not hits:
            return Result(KNOWN_EMPTY, tool="narrative_search",
                          note="질의어와 일치하는 본문을 찾지 못했습니다.")
        data, cites = [], []
        for idx, score in hits:
            r = rows[idx]
            data.append({"text": r["text"][:1500], "score": round(score, 3),
                         "section": r["section"], "corp": r["corp_name"]})
            cites.append({"rcept_no": r["rcept_no"], "report_nm": r["report_nm"],
                          "rcept_dt": r["rcept_dt"], "corp_name": r["corp_name"],
                          "section": r["section"]})
        return Result(FOUND, tool="narrative_search", data=data, citations=cites)

    # ── 3. 사건 검색 (사건 x 정형) ──────────────────────────────────────
    def event_search(self, corp: str, event_type: str | None = None,
                     date_from: int | None = None, date_to: int | None = None,
                     limit: int = 30) -> Result:
        """거래소공시·주요사항보고서 이벤트 조회.

        해당 유형 공시가 0건인 기업은 KNOWN_EMPTY 로 답한다 — 데이터가 없는 게
        아니라 그런 이벤트가 없었던 것이고, 이 둘을 섞으면 감점된다 (감사 F04).
        """
        cid = self._corp(corp)
        if not cid:
            return Result(OUT_OF_SCOPE, tool="event_search",
                          note=f"'{corp}'을(를) 코퍼스의 기업으로 특정하지 못했습니다.")
        corp_row = self._corp_row(cid)
        sql = ("SELECT d.*, co.corp_name FROM docs d JOIN corps co ON co.corp_id=d.corp_id"
               " WHERE d.corp_id=? AND d.doc_group IN ('exchange','major')")
        params = [cid]
        if event_type:
            sql += " AND (d.doc_subtype LIKE ? OR d.report_nm LIKE ?)"
            params += [f"%{event_type}%", f"%{event_type}%"]
        if date_from:
            sql += " AND d.rcept_dt>=?"
            params.append(date_from)
        if date_to:
            sql += " AND d.rcept_dt<=?"
            params.append(date_to)
        sql += " ORDER BY d.rcept_dt DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in self.conn.execute(sql, params)]
        if not rows:
            zero = (corp_row["n_exchange"] == 0 and corp_row["n_major"] == 0)
            note = (f"{corp_row['corp_name']}은(는) 수집 기간(2023.01~2026.03) 동안 "
                    f"거래소공시·주요사항보고서가 0건입니다. 자료 미확보가 아니라 "
                    f"해당 이벤트가 없었던 것입니다."
                    if zero else "조건에 맞는 공시가 없습니다.")
            return Result(KNOWN_EMPTY, tool="event_search", note=note,
                          data=[{"corp": corp_row["corp_name"],
                                 "n_exchange": corp_row["n_exchange"],
                                 "n_major": corp_row["n_major"],
                                 "n_periodic": corp_row["n_periodic"]}])
        data = [{"rcept_no": r["rcept_no"], "date": r["rcept_dt"],
                 "type": r["doc_subtype"], "report_nm": r["report_nm"],
                 "is_correction": bool(r["is_correction"])} for r in rows]
        return Result(FOUND, tool="event_search", data=data,
                      citations=[_cite(r) for r in rows[:10]])

    # ── 4. 정정·후속 체인 (사건 x 관계) ────────────────────────────────
    def chain_resolve(self, rcept_no: str) -> Result:
        """한 공시의 정정 이력을 앞뒤로 따라간다.

        원본이 코퍼스 밖이면 그 사실을 상태로 반환한다 — exchange 정정의 44%가
        여기 해당하고, 이때 "원래 조건"은 답하면 안 된다 (감사 F09).
        """
        rcept_no = str(rcept_no)
        base = self.conn.execute(
            "SELECT d.*, co.corp_name FROM docs d JOIN corps co ON co.corp_id=d.corp_id"
            " WHERE d.rcept_no=?", (rcept_no,)).fetchone()
        if not base:
            return Result(OUT_OF_SCOPE, tool="chain_resolve",
                          note=f"접수번호 {rcept_no} 문서가 코퍼스에 없습니다.")
        backward = self.conn.execute(
            "SELECT * FROM edges WHERE edge_type='correction' AND src_id=?",
            (rcept_no,)).fetchall()
        forward = self.conn.execute(
            "SELECT * FROM edges WHERE edge_type='correction' AND dst_id=?",
            (rcept_no,)).fetchall()
        steps, dangling = [], False
        for e in backward:
            attrs = json.loads(e["attrs"] or "{}")
            if e["status"] == "dangling_out_of_corpus":
                dangling = True
            steps.append({"direction": "이 문서가 정정한 대상", "status": e["status"],
                          "target_rcept_no": e["dst_id"],
                          "원본_제출일": attrs.get("원본_제출일"),
                          "정정사유": attrs.get("정정사유"),
                          "정정항목": attrs.get("정정항목")})
        for e in forward:
            attrs = json.loads(e["attrs"] or "{}")
            steps.append({"direction": "이 문서를 정정한 문서", "status": e["status"],
                          "target_rcept_no": e["src_id"],
                          "정정사유": attrs.get("정정사유"),
                          "정정항목": attrs.get("정정항목")})
        note = ""
        if dangling:
            note = ("이 정정공시가 가리키는 원본이 코퍼스 수집 기간(2023.01~) 이전에 "
                    "제출되어 원문을 확인할 수 없습니다. 정정 전 값은 정정공시에 "
                    "기재된 범위까지만 확인 가능합니다.")
        if base["is_attachment_added"]:
            note += " 이 문서는 [첨부추가]본입니다(정정공시가 아니라 원본으로 수집됨)."
        if not steps:
            return Result(KNOWN_EMPTY, tool="chain_resolve",
                          note=f"{base['report_nm']}에는 확인된 정정 이력이 없습니다." + note,
                          citations=[_cite(base)])
        return Result(FOUND, tool="chain_resolve", data=steps, note=note,
                      citations=[_cite(base)])

    # ── 5. 지분 관계 (상태 x 관계) ─────────────────────────────────────
    def known_filers(self) -> list[str]:
        if not hasattr(self, "_filers"):
            self._filers = [r[0] for r in self.conn.execute(
                "SELECT DISTINCT filer_name FROM docs WHERE doc_group='holding'"
                " AND filer_name IS NOT NULL")]
        return self._filers

    def ownership_lookup(self, corp: str, direction: str = "holders",
                         holder: str | None = None,
                         as_of: int | None = None) -> Result:
        """direction='holders' 이 회사를 누가 보유했나 / 'holdings' 이 회사가 무엇을 보유했나.

        지분공시의 주어는 제출인(flr_nm)이지 발행회사가 아니다 (감사 F10).
        보고가 없다고 보유가 없는 것은 아니므로 그 한계를 note 로 명시한다.
        """
        cid = self._corp(corp)
        if not cid:
            return Result(OUT_OF_SCOPE, tool="ownership_lookup",
                          note=f"'{corp}'을(를) 특정하지 못했습니다.")
        corp_row = self._corp_row(cid)
        if direction == "holders":
            sql = ("SELECT e.*, d.report_nm, d.rcept_dt AS dt, co.corp_name"
                   " FROM edges e JOIN docs d ON d.rcept_no=e.asserted_by"
                   " JOIN corps co ON co.corp_id=? WHERE e.edge_type='ownership'"
                   " AND e.dst_id=?")
            params = [cid, cid]
        else:
            sql = ("SELECT e.*, d.report_nm, d.rcept_dt AS dt, co.corp_name"
                   " FROM edges e JOIN docs d ON d.rcept_no=e.asserted_by"
                   " JOIN corps co ON co.corp_id=e.dst_id WHERE e.edge_type='ownership'"
                   " AND e.src_id=?")
            params = [cid]
        if as_of:
            sql += " AND e.asserted_at<=?"
            params.append(as_of)
        sql += " ORDER BY e.asserted_at DESC"
        rows = [dict(r) for r in self.conn.execute(sql, params)]
        limit_note = ("5% 미만 보유는 대량보유 보고 대상이 아니므로, 보고가 없다는 "
                      "것이 보유하지 않는다는 뜻은 아닙니다.")
        if not rows:
            return Result(KNOWN_EMPTY, tool="ownership_lookup",
                          note=f"{corp_row['corp_name']}에 대해 해당 방향의 "
                               f"대량보유상황보고가 코퍼스에 없습니다. " + limit_note)
        # 특정 보유자를 지목한 질문이면 그 보유자만 남긴다. 없으면 "보고가 없다"를
        # 명시해야 한다 — 사전지식으로 보유 여부를 단정하는 것이 가장 흔한 환각이다.
        if holder:
            hn = holder.replace(" ", "")
            kept = [r for r in rows
                    if hn in (json.loads(r["attrs"] or "{}").get("filer_name") or
                              "").replace(" ", "")]
            if not kept:
                others = sorted({json.loads(r["attrs"] or "{}").get("filer_name")
                                 for r in rows} - {None})
                return Result(
                    KNOWN_EMPTY, tool="ownership_lookup",
                    data=[{"queried_holder": holder, "actual_filers": others}],
                    note=f"코퍼스에는 '{holder}'이(가) {corp_row['corp_name']} 지분을 "
                         f"5% 이상 보유한다는 대량보유상황보고가 없습니다. 이 종목에 "
                         f"보고를 제출한 주체는 {', '.join(others) or '없음'}입니다. "
                         + limit_note)
            rows = kept

        latest: dict[str, dict] = {}
        for r in rows:
            attrs = json.loads(r["attrs"] or "{}")
            key = attrs.get("filer_name") or r["src_id"]
            if key not in latest:
                latest[key] = {
                    "counterpart": key, "ratio": attrs.get("보유비율"),
                    "last_report_dt": r["dt"], "rcept_no": r["asserted_by"],
                    "in_universe": r["status"] == "resolved", "n_reports": 0}
            latest[key]["n_reports"] += 1
        return Result(FOUND, tool="ownership_lookup",
                      data=list(latest.values()), note=limit_note,
                      citations=[{"rcept_no": r["asserted_by"],
                                  "report_nm": r["report_nm"], "rcept_dt": r["dt"],
                                  "corp_name": corp_row["corp_name"], "section": None}
                                 for r in rows[:8]])

    # ── 6. 계약 조회 (사건 x 관계, 클러스터 폴드) ──────────────────────
    def contract_lookup(self, corp: str, keyword: str | None = None,
                        limit: int = 5) -> Result:
        """계약 단위로 조회하고 클러스터 전체를 폴드해 최종 조건을 만든다.

        하나의 계약이 체결→정정→해지로 여러 문서에 흩어져 있어, 문서 단위로 보면
        같은 계약이 여러 건으로 세어지거나 정정 전 조건을 최종값으로 답하게 된다.
        """
        cid = self._corp(corp)
        if not cid:
            return Result(OUT_OF_SCOPE, tool="contract_lookup",
                          note=f"'{corp}'을(를) 특정하지 못했습니다.")
        corp_row = self._corp_row(cid)
        if corp_row["n_exchange"] == 0:
            return Result(KNOWN_EMPTY, tool="contract_lookup",
                          note=f"{corp_row['corp_name']}은(는) 수집 기간 동안 "
                               f"거래소공시가 0건입니다. 자료 미확보가 아니라 해당 "
                               f"공시를 한 적이 없는 것입니다.")
        sql = ("SELECT * FROM contracts WHERE corp_id=? AND contract_id NOT IN"
               " (SELECT src_id FROM edges WHERE edge_type='same_contract')")
        params = [cid]
        if keyword:
            # 키워드가 클러스터 멤버(정정본)의 값에 걸릴 수 있으므로, 멤버를 정규
            # 계약으로 되돌린 뒤 매칭한다. 정정으로 처음 공개된 계약상대명
            # (예: 유보 해제된 '테슬라')이 여기 해당한다.
            sql += (" AND (counterparty LIKE ? OR contract_id IN ("
                    " SELECT COALESCE(e.dst_id, cl.subject_id) FROM claims cl"
                    " LEFT JOIN edges e ON e.edge_type='same_contract'"
                    " AND e.src_id=cl.subject_id WHERE cl.value_text LIKE ?))")
            params += [f"%{keyword}%", f"%{keyword}%"]
        sql += " ORDER BY first_dt DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in self.conn.execute(sql, params)]
        if not rows:
            return Result(KNOWN_EMPTY, tool="contract_lookup",
                          note="조건에 맞는 계약을 찾지 못했습니다.")
        data, cites = [], []
        for c in rows:
            members = [c["contract_id"]] + [
                r["src_id"] for r in self.conn.execute(
                    "SELECT src_id FROM edges WHERE edge_type='same_contract' AND dst_id=?",
                    (c["contract_id"],))]
            placeholders = ",".join("?" * len(members))
            claims = self.conn.execute(
                "SELECT predicate, value_text, unit, asserted_by, asserted_at"
                f" FROM claims WHERE subject_id IN ({placeholders})"
                " ORDER BY asserted_at DESC, claim_id DESC", tuple(members)).fetchall()
            folded, sources = {}, {}
            for cl in claims:
                if cl["predicate"] not in folded:
                    folded[cl["predicate"]] = cl["value_text"]
                    sources[cl["predicate"]] = cl["asserted_by"]
            data.append({"contract_id": c["contract_id"], "status": c["status"],
                         "n_docs": c["n_docs"], "first_dt": c["first_dt"],
                         "latest_dt": c["latest_dt"], "fields": folded,
                         "field_sources": sources})
            cites += self._docmeta([c["first_rcept_no"], c["latest_rcept_no"]])
        note = ("계약별 값은 정정을 반영한 최신 주장입니다."
                if any(d["n_docs"] > 1 for d in data) else "")
        return Result(FOUND, tool="contract_lookup", data=data,
                      citations=cites[:10], note=note)

    # ── 7. 계산 ────────────────────────────────────────────────────────
    _SAFE = re.compile(r"^[\d\s\+\-\*\/\(\)\.\,]+$")

    def calc(self, expression: str) -> Result:
        """LLM 암산을 금지하고 산술을 여기로 넘긴다."""
        expr = expression.replace(",", "")
        if not self._SAFE.match(expr):
            return Result(OUT_OF_SCOPE, tool="calc",
                          note="산술 연산자와 숫자만 허용됩니다.")
        try:
            value = eval(expr, {"__builtins__": {}}, {})   # noqa: S307 - 정규식으로 제한됨
        except Exception as exc:                            # noqa: BLE001
            return Result(OUT_OF_SCOPE, tool="calc", note=f"계산 실패: {exc}")
        return Result(FOUND, tool="calc",
                      data=[{"expression": expression, "value": value}])

    # ── 보조: 코퍼스 커버리지 안내 ─────────────────────────────────────
    def corpus_profile(self, corp: str) -> Result:
        cid = self._corp(corp)
        if not cid:
            return Result(OUT_OF_SCOPE, tool="corpus_profile",
                          note=f"'{corp}'은(는) 코퍼스 70개사에 포함되지 않습니다.")
        r = self._corp_row(cid)
        return Result(FOUND, tool="corpus_profile", data=[dict(r)])
