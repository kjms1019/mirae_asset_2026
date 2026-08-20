"""저장 계층 — 스키마 정의와 접속.

설계 요지 (docs/architecture.md 2장)

  6유형 = (데이터 성격 3) x (시간 의미론 2) 인데, 테이블은 6개가 아니라 3개다.
  데이터 성격 축은 테이블을 가르고, 상태/사건 축은 테이블이 아니라 컬럼과
  조회 규칙을 가르기 때문이다.

    claims  정형   — valid_year 가 차 있으면 상태, event_dt 가 차 있으면 사건
    chunks  서사형 — doc_group 메타로 상태(periodic)/사건(exchange·major) 구분
    edges   관계   — edge_type 으로 상태(ownership)/사건(correction) 구분

  claims 와 edges 는 append-only 원장이다. 정정을 "원본을 찾아 덮어쓰는" 문제로
  풀지 않고 "같은 사실에 대한 나중 주장"으로 쌓는다. 그래서 원본이 코퍼스 밖인
  정정 280건도 최신 상태는 정확하게 나온다 — 연결할 원본이 필요 없기 때문이다.
  조회 시점에 asserted_at 이 가장 늦은 주장을 채택하는 것이 fold 규칙이다.
"""
import json
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "index", "disclosure.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

-- ── 엔티티 ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS corps (
    corp_id      TEXT PRIMARY KEY,       -- corp_code (8자리)
    corp_name    TEXT NOT NULL,          -- DART 공식 법인명
    listed_name  TEXT,                   -- 거래소 통용 종목명
    eng_name     TEXT,
    stock_code   TEXT,
    market       TEXT,
    industry     TEXT,
    sector       TEXT,
    listing_date TEXT,
    market_cap   REAL,
    n_periodic   INTEGER, n_major INTEGER, n_exchange INTEGER, n_holding INTEGER
);

-- 질의어가 corp_name 과 다를 때 매칭 실패하는 것을 막는다 (감사 F03).
-- 현대차/현대자동차, KT/케이티, LIG넥스원/LIG디펜스앤에어로스페이스 등 6개사 +
-- 'JYP Ent.' 처럼 구두점만 다른 표기 + 외국계 제출인의 공백 제거 표기.
CREATE TABLE IF NOT EXISTS corp_alias (
    alias_norm TEXT NOT NULL,            -- 정규화된 별칭 (소문자·공백/구두점 제거)
    alias      TEXT NOT NULL,
    corp_id    TEXT NOT NULL,
    alias_type TEXT,                     -- official | listed | english | folder | filer
    PRIMARY KEY (alias_norm, corp_id)
);
CREATE INDEX IF NOT EXISTS ix_alias_norm ON corp_alias(alias_norm);

-- ── 문서 척추 ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS docs (
    doc_id       TEXT PRIMARY KEY,
    rcept_no     TEXT UNIQUE NOT NULL,
    corp_id      TEXT NOT NULL,
    doc_group    TEXT NOT NULL,          -- periodic | major | exchange | holding
    doc_subtype  TEXT,                   -- major 는 report_nm 괄호에서 복구 (감사 F11)
    report_nm    TEXT,
    rcept_dt     INTEGER NOT NULL,       -- 접수일 = 인지 시점
    base_year    INTEGER,                -- 채워져 있으면 '상태' 문서 (감사 F02)
    base_month   INTEGER,
    is_correction        INTEGER DEFAULT 0,
    is_attachment_added  INTEGER DEFAULT 0,   -- [첨부추가] 6건. is_correction=0 이라 오인 주의
    filer_name   TEXT,                   -- flr_nm 원문
    filer_corp_id TEXT,                  -- 제출인이 유니버스 내 기업이면 그 corp_id
    doc_format   TEXT,                   -- dart_xml | html_form | unsupported
    parse_status TEXT,                   -- ok | unsupported_format | error
    file_path    TEXT,
    n_files      INTEGER
);
CREATE INDEX IF NOT EXISTS ix_docs_corp  ON docs(corp_id, doc_group);
CREATE INDEX IF NOT EXISTS ix_docs_dt    ON docs(rcept_dt);
CREATE INDEX IF NOT EXISTS ix_docs_group ON docs(doc_group, doc_subtype);

-- ── 사실 원장 (append-only) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS claims (
    claim_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL,          -- corp | contract | filing
    subject_id   TEXT NOT NULL,
    predicate    TEXT NOT NULL,          -- 매출액 | 영업이익 | 계약금액 | 계약상대 | 보유비율 ...
    value_text   TEXT,
    value_num    REAL,
    unit         TEXT,                   -- 원 | 백만원 | % ...
    valid_year   INTEGER,                -- 상태: 귀속 회계기간
    valid_month  INTEGER,
    event_dt     INTEGER,                -- 사건: 발생 시점
    asserted_by  TEXT NOT NULL,          -- rcept_no  ← 근거
    asserted_at  INTEGER NOT NULL,       -- rcept_dt  ← 인지 시점 (fold 기준)
    section      TEXT
);
CREATE INDEX IF NOT EXISTS ix_claims_lookup ON claims(subject_id, predicate, valid_year);
CREATE INDEX IF NOT EXISTS ix_claims_pred   ON claims(predicate);
CREATE INDEX IF NOT EXISTS ix_claims_src    ON claims(asserted_by);

-- ── 관계 원장 (append-only) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS edges (
    edge_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_type   TEXT NOT NULL,           -- correction | ownership | attachment_added | contract_member
    src_type    TEXT, src_id TEXT,
    dst_type    TEXT, dst_id TEXT,
    -- resolved            : 코퍼스 안에서 대상을 찾음
    -- dangling_out_of_corpus : 대상이 2023-01 이전이라 코퍼스 밖 (exchange 정정 280건)
    -- unresolved          : 대상을 특정하지 못함
    status      TEXT,
    attrs       TEXT,                    -- JSON
    asserted_by TEXT, asserted_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_edges_src  ON edges(edge_type, src_id);
CREATE INDEX IF NOT EXISTS ix_edges_dst  ON edges(edge_type, dst_id);

-- ── 계약 클러스터 ────────────────────────────────────────────────────────
-- 하나의 계약이 체결→정정→해지로 여러 문서에 흩어져 있어, 논리적 단위로 묶는다.
-- 묶는 근거는 명시적 링크(정정 대상 지목)뿐이다. 휴리스틱으로 붙이면 다른 계약을
-- 합칠 위험이 오답보다 크다.
CREATE TABLE IF NOT EXISTS contracts (
    contract_id  TEXT PRIMARY KEY,
    corp_id      TEXT NOT NULL,
    counterparty TEXT,
    first_rcept_no  TEXT, first_dt  INTEGER,
    latest_rcept_no TEXT, latest_dt INTEGER,
    n_docs       INTEGER DEFAULT 1,
    status       TEXT                    -- active | terminated | amended | unknown
);
CREATE INDEX IF NOT EXISTS ix_contracts_corp ON contracts(corp_id);

-- ── 서사형 ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id    TEXT NOT NULL,
    rcept_no  TEXT NOT NULL,
    corp_id   TEXT NOT NULL,
    doc_group TEXT,
    rcept_dt  INTEGER,
    base_year INTEGER,
    section   TEXT,
    ord       INTEGER,
    text      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chunks_corp ON chunks(corp_id);
CREATE INDEX IF NOT EXISTS ix_chunks_doc  ON chunks(doc_id);

-- ── 적재 진행 상태 (증분 처리용) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ingest_log (
    stage     TEXT NOT NULL,
    rcept_no  TEXT NOT NULL,
    status    TEXT,
    detail    TEXT,
    PRIMARY KEY (stage, rcept_no)
);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def reset(path: str = DB_PATH) -> sqlite3.Connection:
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)
    conn = connect(path)
    init_schema(conn)
    return conn


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def counts(conn: sqlite3.Connection) -> dict:
    out = {}
    for t in ("corps", "corp_alias", "docs", "claims", "edges", "contracts", "chunks"):
        out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return out
