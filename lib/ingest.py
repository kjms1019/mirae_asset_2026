"""적재 파이프라인 — 원문을 원장으로 녹인다.

3단 구조이고 각 단이 멱등이다.

  normalize  원문 → docs (포맷 판별, 엔티티 해석, parse_status 기록)
  extract    문서 → claims / edges / chunks 를 append-only 로 적재
  fold       원장 → 상태 뷰 (lib/query.py 가 조회 시점에 수행)

정정은 별도 단계가 없다. 정정본이 주장하는 값을 그 문서의 rcept_dt 로 claims 에
쌓아두면, 조회 시점에 asserted_at 이 가장 늦은 주장이 채택되면서 자동으로
최신 정정이 반영된다. 원본을 찾아 연결할 필요가 없어서, 원본이 코퍼스 밖인
exchange 정정 280건도 최신 상태만큼은 정확하게 나온다.
"""
import os
import re
import sys

import pandas as pd

from . import store
from .entity import Resolver, alias_rows, normalize
from .parser import (FORMAT_DART_XML, FORMAT_HTML_FORM, FORMAT_UNSUPPORTED,
                     get_section_by_title_suffix, get_top_sections, parse_dart_xml,
                     parse_html_form, render, resolve_document, table_to_rows)

CORPUS = os.path.join("data", "3.공시", "corpus")

NUM_RE = re.compile(r"^-?[\d,]+(\.\d+)?$")
UNIT_RE = re.compile(r"\(단위\s*[:：]\s*([^)]+)\)")
YEAR_COL_RE = re.compile(r"(\d{4})\s*년")
#: 정기공시 청킹 대상 섹션 — 서사형 질의가 실제로 향하는 곳
PERIODIC_SECTIONS = ("I. 회사의 개요", "II. 사업의 내용",
                     "III. 재무에 관한 사항", "IV. 이사의 경영진단 및 분석의견")
#: 요약재무정보에서 뽑아 쓸 핵심 계정 (동의어 포함)
KEY_ACCOUNTS = {
    "매출액": "매출액", "영업수익": "매출액", "수익(매출액)": "매출액",
    "영업이익": "영업이익", "영업이익(손실)": "영업이익",
    "당기순이익": "당기순이익", "연결총당기순이익": "당기순이익",
    "당기순이익(손실)": "당기순이익", "자산총계": "자산총계",
    "부채총계": "부채총계", "자본총계": "자본총계",
}


def _num(text: str):
    if text is None:
        return None
    t = str(text).strip().replace(",", "")
    neg = False
    if t.startswith("(") and t.endswith(")"):
        t, neg = t[1:-1], True
    if t.startswith("△") or t.startswith("▲"):
        t, neg = t[1:], True
    if not t or not re.fullmatch(r"-?\d+(\.\d+)?", t):
        return None
    v = float(t)
    return -v if neg else v


def _log(conn, stage, rcept_no, status, detail=""):
    conn.execute("INSERT OR REPLACE INTO ingest_log VALUES (?,?,?,?)",
                 (stage, str(rcept_no), status, detail[:300]))


def _done(conn, stage) -> set:
    return {r[0] for r in conn.execute(
        "SELECT rcept_no FROM ingest_log WHERE stage=? AND status='ok'", (stage,))}


# ── 1단: 엔티티 ──────────────────────────────────────────────────────────────

def stage_entities(conn, corpus=CORPUS):
    u = pd.read_csv(os.path.join(corpus, "universe.csv"),
                    dtype={"corp_code": str, "stock_code": str})
    conn.execute("DELETE FROM corps")
    conn.execute("DELETE FROM corp_alias")
    for _, r in u.iterrows():
        conn.execute(
            "INSERT INTO corps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(r.corp_code), r.corp_name, r.listed_name, r.get("corp_eng_name"),
             str(r.stock_code), r.get("market"), r.get("industry"), r.get("sector"),
             str(r.get("listing_date")), float(r.get("market_cap") or 0),
             int(r.n_periodic), int(r.n_major), int(r.n_exchange), int(r.n_holding)))
        for row in alias_rows(r):
            conn.execute("INSERT OR IGNORE INTO corp_alias VALUES (?,?,?,?)", row)

    # 폴더명 별칭 — 'JYP Ent' 처럼 폴더명 제약으로 표기가 달라진 경우 (README 주의 2)
    manifest = pd.read_json(os.path.join(corpus, "manifest.jsonl"), lines=True,
                            dtype={"corp_code": str})
    for code, name in manifest[["corp_code", "corp_name"]].drop_duplicates().values:
        conn.execute("INSERT OR IGNORE INTO corp_alias VALUES (?,?,?,?)",
                     (normalize(name), name, str(code), "folder"))
    # 제출인 표기 별칭 — 'JYP Ent.' 처럼 구두점만 다른 표기를 흡수한다.
    # 정규화가 이미 구두점을 지우므로 대개 기존 별칭과 같은 키로 떨어지고,
    # 다른 표기만 새 행이 된다. 발행회사와 제출인이 같은 문서에서만 취한다.
    same = manifest[manifest.corp_name == manifest.flr_nm]
    for code, filer in same[["corp_code", "flr_nm"]].drop_duplicates().values:
        if normalize(filer):
            conn.execute("INSERT OR IGNORE INTO corp_alias VALUES (?,?,?,?)",
                         (normalize(filer), filer, str(code), "filer"))
    conn.commit()
    return store.counts(conn)


# ── 2단: 문서 척추 ───────────────────────────────────────────────────────────

def stage_docs(conn, corpus=CORPUS):
    m = pd.read_json(os.path.join(corpus, "manifest.jsonl"), lines=True,
                     dtype={"corp_code": str, "stock_code": str})
    resolver = Resolver(conn)
    conn.execute("DELETE FROM docs")
    rows = []
    for _, r in m.iterrows():
        fmt, _path = resolve_document(corpus, r.file_path, r.rcept_no)
        subtype = r.doc_subtype
        if r.doc_group == "major" and (subtype is None or pd.isna(subtype)):
            mo = re.search(r"\((.+?)\)$", str(r.report_nm))       # 감사 F11
            subtype = mo.group(1) if mo else None
        rows.append((
            r.doc_id, str(r.rcept_no), str(r.corp_code), r.doc_group, subtype,
            r.report_nm, int(r.rcept_dt),
            None if pd.isna(r.base_year) else int(r.base_year),
            None if pd.isna(r.base_month) else int(r.base_month),
            int(bool(r.is_correction)),
            int("[첨부추가]" in str(r.report_nm)),                  # README 주의: 6건
            r.flr_nm, resolver.resolve(r.flr_nm),
            fmt, "unsupported_format" if fmt == FORMAT_UNSUPPORTED else "pending",
            r.file_path, int(r.n_files)))
    conn.executemany(
        "INSERT INTO docs VALUES (" + ",".join("?" * 17) + ")", rows)
    conn.commit()
    return store.counts(conn)


# ── 3단: 거래소공시 추출 ─────────────────────────────────────────────────────

_SKIP_FIELDS = {"(무명)"}


def _contract_id(corp_id: str, rcept_no: str) -> str:
    return f"K{corp_id}-{rcept_no}"


def extract_exchange(conn, corpus=CORPUS, limit=None):
    """거래소공시 → claims + 정정 edges + contracts.

    필드가 이미 key-value 라 추출이 싸다. 정정본은 '정정후' 값을 그 문서의
    rcept_dt 로 다시 주장하게 만들어, fold 시 자동으로 최신값이 이기게 한다.
    """
    docs = conn.execute(
        "SELECT * FROM docs WHERE doc_group='exchange' AND doc_format=? ORDER BY rcept_dt",
        (FORMAT_HTML_FORM,)).fetchall()
    if limit:
        docs = docs[:limit]
    seen = _done(conn, "exchange")
    n_claim = n_edge = 0

    for d in docs:
        if d["rcept_no"] in seen:
            continue
        path = os.path.join(corpus, d["file_path"], f"{d['rcept_no']}.xml")
        try:
            parsed = parse_html_form(path)
        except Exception as exc:                                  # noqa: BLE001
            _log(conn, "exchange", d["rcept_no"], "error", str(exc))
            continue

        cid = _contract_id(d["corp_id"], d["rcept_no"])
        subject = ("contract", cid)
        rows = []
        for key, value in parsed["fields"].items():
            if key in _SKIP_FIELDS or not value or value == "-":
                continue
            unit = "원" if "(원)" in key else ("%" if "(%)" in key else None)
            rows.append((subject[0], subject[1], key, value, _num(value), unit,
                         None, None, d["rcept_dt"], d["rcept_no"], d["rcept_dt"],
                         "거래소공시"))
        # 정정본이 주장하는 '정정후' 값 — 같은 술어에 대한 나중 주장
        for corr in parsed["corrections"]:
            item, after = corr.get("항목"), corr.get("정정후")
            if item and after and after != "-":
                rows.append(("contract", cid, item, after, _num(after), None,
                             None, None, d["rcept_dt"], d["rcept_no"],
                             d["rcept_dt"], "정정사항"))
        if rows:
            conn.executemany(
                "INSERT INTO claims (subject_type,subject_id,predicate,value_text,"
                "value_num,unit,valid_year,valid_month,event_dt,asserted_by,"
                "asserted_at,section) VALUES (" + ",".join("?" * 12) + ")", rows)
            n_claim += len(rows)

        counterparty = (parsed["fields"].get("3. 계약상대")
                        or parsed["fields"].get("계약상대"))
        conn.execute(
            "INSERT OR REPLACE INTO contracts VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, d["corp_id"], counterparty, d["rcept_no"], d["rcept_dt"],
             d["rcept_no"], d["rcept_dt"], 1,
             "terminated" if "해지" in (d["doc_subtype"] or "") else "active"))

        # 정정 엣지 — 원본을 못 찾아도 상태로 남긴다 (감사 F09)
        if d["is_correction"]:
            orig_dt = parsed["corr_meta"].get("원본_제출일")
            status, dst = "unresolved", None
            if orig_dt:
                hit = conn.execute(
                    "SELECT rcept_no FROM docs WHERE corp_id=? AND doc_group='exchange'"
                    " AND rcept_dt=? AND rcept_no<>? LIMIT 1",
                    (d["corp_id"], orig_dt, d["rcept_no"])).fetchone()
                if hit:
                    status, dst = "resolved", hit["rcept_no"]
                elif orig_dt < 20230101:
                    status = "dangling_out_of_corpus"
            conn.execute(
                "INSERT INTO edges (edge_type,src_type,src_id,dst_type,dst_id,status,"
                "attrs,asserted_by,asserted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("correction", "filing", d["rcept_no"], "filing", dst, status,
                 store.jdump({"원본_제출일": orig_dt,
                              "정정사유": parsed["corr_meta"].get("정정사유"),
                              "정정항목": parsed["corrections"]}),
                 d["rcept_no"], d["rcept_dt"]))
            n_edge += 1

        conn.execute("UPDATE docs SET parse_status='ok' WHERE rcept_no=?",
                     (d["rcept_no"],))
        # 계약 본문을 청크로
        text = parsed["text"]
        if text:
            conn.execute(
                "INSERT INTO chunks (doc_id,rcept_no,corp_id,doc_group,rcept_dt,"
                "base_year,section,ord,text) VALUES (?,?,?,?,?,?,?,?,?)",
                (d["doc_id"], d["rcept_no"], d["corp_id"], "exchange",
                 d["rcept_dt"], None, d["doc_subtype"], 0, text[:4000]))
        _log(conn, "exchange", d["rcept_no"], "ok")
        conn.commit()
    return {"claims": n_claim, "edges": n_edge}


# ── 4단: 정기공시 추출 ───────────────────────────────────────────────────────

KI_RE = re.compile(r"제\s*(\d+)\s*기")


def _parse_summary_table(rows: list[list[str]],
                         base_year: int | None = None) -> tuple[str | None, list[tuple]]:
    """요약재무정보 표 → [(계정명, 연도, 값)] 과 단위.

    컬럼-연도 매핑에 두 가지 표기가 섞여 있다.
      · 연도 라벨형  "2023년 12월말 | 2022년 12월말 | 2021년 12월말"  (삼성전자)
      · 기수형       "제78기 | 제77기 | 제76기"                      (SK하이닉스)
    기수형은 표 안에 연도가 없어서, 가장 큰 기수를 보고서의 base_year 로 놓고
    한 기수당 1년씩 거슬러 매핑한다.
    """
    unit = None
    col_years: list[int | None] = []
    out = []
    ncols = max((len(r) for r in rows), default=0)

    def align(mapping: list[int | None]) -> list[int | None]:
        """헤더 행이 데이터 행보다 짧으면 오른쪽에 맞춘다.

        구분 셀에 rowspan 이 걸린 기업은 둘째 헤더 행에 선행 셀이 없다
        (삼성SDI: ['(2025년 12월말)','(2024년 12월말)','(2023년 12월말)']).
        그대로 두면 컬럼이 한 칸씩 밀려 연도가 어긋난다.
        """
        pad = ncols - len(mapping)
        return ([None] * pad + mapping) if pad > 0 else mapping

    for ridx, row in enumerate(rows):
        joined = " ".join(row)
        # 단위는 표 머리에서만 읽는다. 본문 중간의 '기본주당순이익(단위 : 원)'
        # 같은 행별 단위를 표 전체 단위로 잘못 채택하면 금액 자릿수가 어긋난다.
        if unit is None and ridx < 3:
            mo = UNIT_RE.search(joined)
            if mo:
                unit = mo.group(1).strip()

        numeric_cells = sum(1 for c in row if _num(c) is not None)

        # 연도 라벨 행
        years = YEAR_COL_RE.findall(joined)
        if len(years) >= 2 and numeric_cells == 0:
            col_years = align([
                int(mo.group(1)) if (mo := YEAR_COL_RE.search(cell)) else None
                for cell in row])
            continue

        # 기수 행
        kis = [(i, int(mo.group(1)))
               for i, cell in enumerate(row) if (mo := KI_RE.search(cell))]
        if len(kis) >= 2 and numeric_cells == 0 and base_year:
            newest = max(k for _, k in kis)
            mapping: list[int | None] = [None] * len(row)
            for idx, k in kis:
                mapping[idx] = base_year - (newest - k)
            col_years = align(mapping)
            continue

        if not col_years or not row:
            continue
        account = row[0].strip()
        if not account or _num(account) is not None:
            continue
        for idx, cell in enumerate(row):
            if idx >= len(col_years) or col_years[idx] is None:
                continue
            value = _num(cell)
            if value is not None:
                out.append((account, col_years[idx], value))
    return unit, out


def extract_periodic(conn, corpus=CORPUS, limit=None, subtypes=("annual",)):
    """정기공시 → 재무 claims + 서사형 chunks."""
    q = ("SELECT * FROM docs WHERE doc_group='periodic' AND doc_format=?"
         " AND doc_subtype IN (%s) ORDER BY rcept_dt" %
         ",".join("?" * len(subtypes)))
    docs = conn.execute(q, (FORMAT_DART_XML, *subtypes)).fetchall()
    if limit:
        docs = docs[:limit]
    seen = _done(conn, "periodic")
    n_claim = n_chunk = 0

    for d in docs:
        if d["rcept_no"] in seen:
            continue
        path = os.path.join(corpus, d["file_path"], f"{d['rcept_no']}.xml")
        try:
            root = parse_dart_xml(path)
        except Exception as exc:                                  # noqa: BLE001
            _log(conn, "periodic", d["rcept_no"], "error", str(exc))
            continue

        # (1) 요약재무정보 → 상태 claims
        rows = []
        for keyword in ("요약연결재무정보", "요약재무정보"):
            section = None
            for tag in ("SECTION-2", "SECTION-1"):
                for el in root.iter(tag):
                    title = None
                    for t in el.iter("TITLE"):
                        if t.get("ATOC") == "Y":
                            title = " ".join("".join(t.itertext()).split())
                            break
                    if title and keyword in title:
                        section = el
                        break
                if section is not None:
                    break
            if section is None:
                continue
            # 요약재무정보 섹션의 표 구성이 기업마다 다르다. 한 표에 재무상태와
            # 손익이 같이 오기도 하고(SK하이닉스), 별개 표로 쪼개지기도 하며
            # (KB금융·삼성SDI), 그 뒤에 별도재무제표 표가 같은 계정으로 다시 온다.
            # 규칙은 "첫 등장 우선"이다. DART 관례상 연결이 별도보다 먼저 오므로,
            # (계정, 연도)마다 처음 만난 값을 쓰면 연결 기준이 자연히 채택된다.
            seen_keys: set[tuple[str, int]] = set()
            for table in section.iter("TABLE"):
                unit, facts = _parse_summary_table(table_to_rows(table), d["base_year"])
                for account, year, value in facts:
                    canon = KEY_ACCOUNTS.get(account.replace(" ", ""))
                    if not canon or (canon, year) in seen_keys:
                        continue
                    seen_keys.add((canon, year))
                    rows.append(("corp", d["corp_id"], canon, str(value), value,
                                 unit or "백만원", year, 12, None,
                                 d["rcept_no"], d["rcept_dt"], keyword))
            if rows:
                break
        if rows:
            conn.executemany(
                "INSERT INTO claims (subject_type,subject_id,predicate,value_text,"
                "value_num,unit,valid_year,valid_month,event_dt,asserted_by,"
                "asserted_at,section) VALUES (" + ",".join("?" * 12) + ")", rows)
            n_claim += len(rows)

        # (2) 서사형 섹션 → chunks
        sections = get_top_sections(root, max_chars=20000)
        ord_ = 0
        for title in PERIODIC_SECTIONS:
            body = sections.get(title)
            if not body:
                continue
            for piece in _split(body):
                conn.execute(
                    "INSERT INTO chunks (doc_id,rcept_no,corp_id,doc_group,rcept_dt,"
                    "base_year,section,ord,text) VALUES (?,?,?,?,?,?,?,?,?)",
                    (d["doc_id"], d["rcept_no"], d["corp_id"], "periodic",
                     d["rcept_dt"], d["base_year"], title, ord_, piece))
                ord_ += 1
                n_chunk += 1
        summary = get_section_by_title_suffix(root, "요약재무정보")
        if summary:
            conn.execute(
                "INSERT INTO chunks (doc_id,rcept_no,corp_id,doc_group,rcept_dt,"
                "base_year,section,ord,text) VALUES (?,?,?,?,?,?,?,?,?)",
                (d["doc_id"], d["rcept_no"], d["corp_id"], "periodic",
                 d["rcept_dt"], d["base_year"], "III. 재무 > 요약재무정보",
                 ord_, summary[:4000]))
            n_chunk += 1

        conn.execute("UPDATE docs SET parse_status='ok' WHERE rcept_no=?",
                     (d["rcept_no"],))
        _log(conn, "periodic", d["rcept_no"], "ok")
        conn.commit()
        print(f"  periodic {d['rcept_no']} {d['corp_id']} claims={len(rows)}", flush=True)
    return {"claims": n_claim, "chunks": n_chunk}


def _split(text: str, max_chars: int = 1200, overlap: int = 150) -> list[str]:
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 2 <= max_chars:
            buf = f"{buf}\n\n{p}" if buf else p
            continue
        if buf:
            chunks.append(buf)
        if len(p) <= max_chars:
            buf = p
        else:
            start = 0
            while start < len(p):
                chunks.append(p[start:start + max_chars])
                start += max_chars - overlap
            buf = ""
    if buf:
        chunks.append(buf)
    return chunks


# ── 5단: 주요사항보고서 / 지분공시 ───────────────────────────────────────────

def extract_major(conn, corpus=CORPUS, limit=None):
    docs = conn.execute(
        "SELECT * FROM docs WHERE doc_group='major' AND doc_format=? ORDER BY rcept_dt",
        (FORMAT_DART_XML,)).fetchall()
    if limit:
        docs = docs[:limit]
    seen = _done(conn, "major")
    n_claim = n_chunk = 0
    for d in docs:
        if d["rcept_no"] in seen:
            continue
        path = os.path.join(corpus, d["file_path"], f"{d['rcept_no']}.xml")
        try:
            root = parse_dart_xml(path)
        except Exception as exc:                                  # noqa: BLE001
            _log(conn, "major", d["rcept_no"], "error", str(exc))
            continue
        text = render(root)
        conn.execute("INSERT INTO claims (subject_type,subject_id,predicate,"
                     "value_text,value_num,unit,valid_year,valid_month,event_dt,"
                     "asserted_by,asserted_at,section) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("corp", d["corp_id"], "주요사항", d["doc_subtype"], None, None,
                      None, None, d["rcept_dt"], d["rcept_no"], d["rcept_dt"],
                      "주요사항보고서"))
        n_claim += 1
        for i, piece in enumerate(_split(text)[:6]):
            conn.execute(
                "INSERT INTO chunks (doc_id,rcept_no,corp_id,doc_group,rcept_dt,"
                "base_year,section,ord,text) VALUES (?,?,?,?,?,?,?,?,?)",
                (d["doc_id"], d["rcept_no"], d["corp_id"], "major", d["rcept_dt"],
                 None, d["doc_subtype"], i, piece))
            n_chunk += 1
        conn.execute("UPDATE docs SET parse_status='ok' WHERE rcept_no=?", (d["rcept_no"],))
        _log(conn, "major", d["rcept_no"], "ok")
        conn.commit()
    return {"claims": n_claim, "chunks": n_chunk}


_RATIO_RE = re.compile(r"보유비율[^0-9]{0,20}([0-9]{1,2}\.[0-9]{1,2})")


def extract_holding(conn, corpus=CORPUS, limit=None):
    """지분공시 → ownership 엣지. 방향은 제출인(보유) → 발행회사(피보유)."""
    docs = conn.execute(
        "SELECT * FROM docs WHERE doc_group='holding' AND doc_format=? ORDER BY rcept_dt",
        (FORMAT_DART_XML,)).fetchall()
    if limit:
        docs = docs[:limit]
    seen = _done(conn, "holding")
    n_edge = 0
    for d in docs:
        if d["rcept_no"] in seen:
            continue
        ratio = None
        path = os.path.join(corpus, d["file_path"], f"{d['rcept_no']}.xml")
        try:
            text = render(parse_dart_xml(path))[:60000]
            mo = _RATIO_RE.search(text)
            if mo:
                ratio = float(mo.group(1))
        except Exception:                                          # noqa: BLE001
            text = ""
        conn.execute(
            "INSERT INTO edges (edge_type,src_type,src_id,dst_type,dst_id,status,"
            "attrs,asserted_by,asserted_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("ownership", "holder", d["filer_corp_id"] or d["filer_name"],
             "corp", d["corp_id"], "resolved" if d["filer_corp_id"] else "external",
             store.jdump({"filer_name": d["filer_name"], "보유비율": ratio,
                          "is_correction": d["is_correction"]}),
             d["rcept_no"], d["rcept_dt"]))
        n_edge += 1
        if ratio is not None:
            conn.execute(
                "INSERT INTO claims (subject_type,subject_id,predicate,value_text,"
                "value_num,unit,valid_year,valid_month,event_dt,asserted_by,"
                "asserted_at,section) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("corp", d["corp_id"], f"보유비율:{d['filer_name']}", str(ratio),
                 ratio, "%", None, None, d["rcept_dt"], d["rcept_no"],
                 d["rcept_dt"], "대량보유상황보고서"))
        conn.execute("UPDATE docs SET parse_status='ok' WHERE rcept_no=?", (d["rcept_no"],))
        _log(conn, "holding", d["rcept_no"], "ok")
        if n_edge % 100 == 0:
            conn.commit()
    conn.commit()
    return {"edges": n_edge}


def cluster_contracts(conn):
    """체결→정정→해지로 흩어진 문서를 하나의 계약으로 묶는다.

    묶는 근거는 정정공시가 명시적으로 지목한 원본뿐이다. "같은 상대·비슷한 금액"
    같은 휴리스틱으로 붙이면 서로 다른 계약을 합칠 위험이 있고, 그 오답이 계약을
    못 묶는 것보다 나쁘다. 그래서 원본이 코퍼스 밖인 280건은 묶지 않고 단독으로
    남긴다 — 그 상태가 곧 "이전 이력은 확인 불가"라는 정보다.
    """
    conn.execute("DELETE FROM edges WHERE edge_type='same_contract'")
    parent: dict[str, str] = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    rows = conn.execute(
        "SELECT e.src_id, e.dst_id FROM edges e"
        " JOIN docs d ON d.rcept_no=e.src_id"
        " WHERE e.edge_type='correction' AND e.status='resolved'"
        " AND d.doc_group='exchange'").fetchall()
    by_filing = {r["rcept_no"]: r["contract_id"] for r in conn.execute(
        "SELECT first_rcept_no AS rcept_no, contract_id FROM contracts")}
    for r in rows:
        a, b = by_filing.get(r["src_id"]), by_filing.get(r["dst_id"])
        if a and b:
            parent.setdefault(a, a)
            parent.setdefault(b, b)
            union(a, b)

    groups: dict[str, list[str]] = {}
    for cid in list(parent):
        groups.setdefault(find(cid), []).append(cid)

    n = 0
    for canonical, members in groups.items():
        if len(members) < 2:
            continue
        meta = conn.execute(
            "SELECT contract_id, first_rcept_no, first_dt FROM contracts"
            " WHERE contract_id IN (%s) ORDER BY first_dt" % ",".join("?" * len(members)),
            tuple(members)).fetchall()
        root = meta[0]["contract_id"]
        last = meta[-1]
        terminated = conn.execute(
            "SELECT 1 FROM docs WHERE rcept_no IN (%s) AND doc_subtype LIKE '%%해지%%'"
            % ",".join("?" * len(members)),
            tuple(m["first_rcept_no"] for m in meta)).fetchone()
        conn.execute(
            "UPDATE contracts SET latest_rcept_no=?, latest_dt=?, n_docs=?, status=?"
            " WHERE contract_id=?",
            (last["first_rcept_no"], last["first_dt"], len(members),
             "terminated" if terminated else "amended", root))
        for m in meta[1:]:
            conn.execute(
                "INSERT INTO edges (edge_type,src_type,src_id,dst_type,dst_id,status,"
                "attrs,asserted_by,asserted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("same_contract", "contract", m["contract_id"], "contract", root,
                 "resolved", "{}", m["first_rcept_no"], m["first_dt"]))
            n += 1
    conn.commit()
    return {"clusters": sum(1 for m in groups.values() if len(m) > 1), "links": n}


def link_periodic_corrections(conn):
    """정기공시 정정 → 원본 엣지. 그룹키로 묶인다 (감사 F09, 135/137 성공)."""
    conn.execute("DELETE FROM edges WHERE edge_type='correction' AND src_id IN "
                 "(SELECT rcept_no FROM docs WHERE doc_group='periodic')")
    rows = conn.execute(
        "SELECT rcept_no, corp_id, doc_subtype, base_year, base_month, rcept_dt,"
        " is_correction FROM docs WHERE doc_group='periodic' ORDER BY rcept_dt").fetchall()
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault(
            (r["corp_id"], r["doc_subtype"], r["base_year"], r["base_month"]), []).append(r)
    n = 0
    for members in groups.values():
        originals = [m for m in members if not m["is_correction"]]
        for m in members:
            if not m["is_correction"]:
                continue
            prior = [x for x in members if x["rcept_dt"] < m["rcept_dt"]]
            dst = (max(prior, key=lambda x: x["rcept_dt"])["rcept_no"]
                   if prior else (originals[0]["rcept_no"] if originals else None))
            conn.execute(
                "INSERT INTO edges (edge_type,src_type,src_id,dst_type,dst_id,status,"
                "attrs,asserted_by,asserted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("correction", "filing", m["rcept_no"], "filing", dst,
                 "resolved" if dst else "unresolved",
                 store.jdump({"via": "group_key"}), m["rcept_no"], m["rcept_dt"]))
            n += 1
    conn.commit()
    return {"edges": n}


# ── CLI ──────────────────────────────────────────────────────────────────────

STAGES = {
    "entities": stage_entities,
    "docs": stage_docs,
    "exchange": extract_exchange,
    "major": extract_major,
    "holding": extract_holding,
    "periodic": extract_periodic,
    "links": link_periodic_corrections,
    "cluster": cluster_contracts,
}


def main(argv):
    names = argv[1:] or list(STAGES)
    conn = store.connect_for_write()
    store.init_schema(conn)
    for name in names:
        fn = STAGES.get(name)
        if not fn:
            print(f"unknown stage: {name}")
            continue
        print(f"\n=== {name} ===", flush=True)
        print(" ", fn(conn), flush=True)
    print("\n최종:", store.counts(conn))


if __name__ == "__main__":
    main(sys.argv)
