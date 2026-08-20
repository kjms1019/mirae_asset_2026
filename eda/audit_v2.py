"""공시 코퍼스 정밀 감사 (EDA v2) — 재현 스크립트.

v1(generate_eda.py)은 '집계가 맞는가'를 봤다. v2는 '집계는 맞는데 필드·그룹
레벨에서 갈라지는 곳이 어디인가'를 본다. major.doc_subtype 100% 결측처럼
전수 집계로는 정상으로 보이지만 실제 파이프라인을 조용히 망가뜨리는 유형을
찾는 것이 목적이다.

실행: python eda/audit_v2.py   (저장소 루트에서, data/ 필요)
산출: eda/audit_v2.json
"""
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

import pandas as pd
from lxml import etree

CORPUS = os.path.join("data", "3.공시", "corpus")
OUT_PATH = os.path.join("eda", "audit_v2.json")

# periodic 원문은 문서당 중앙값 2.6MB라 전수 파싱이 비싸다. 파싱 건전성 검사만
# 표본으로 돌리고, 메타데이터·파일시스템 검사는 전수로 한다.
PERIODIC_SAMPLE = 70
SEED = 11

BARE_AMP = re.compile(r"&(?!(?:[a-zA-Z][a-zA-Z0-9]*|#\d+|#x[0-9a-fA-F]+);)")
TAGLIKE = re.compile(r"<\s*/?\s*([^\s/>!?][^\s/>]*)")
ATTR = re.compile(r'(\s[A-Z][A-Z0-9_]*\s*=\s*")(.*?)("(?=[\s/>]))', re.S)
HANGUL = re.compile(r"[가-힣]")
DART_TAG = re.compile(r"[A-Z][A-Z0-9\-]*")

TAG_STRIP = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")
CORR_DATE = re.compile(r"정정관련\s*공시서류\s*제출일\s*([0-9]{4})[-.\s]?([0-9]{2})[-.\s]?([0-9]{2})")

F = {}  # findings


def head(t):
    print(f"\n{'=' * 78}\n{t}\n{'=' * 78}")


def repair_xml(s: str) -> str:
    """DART 원문의 3대 스키마 위반을 보정한다.

    ① 이스케이프 안 된 & ("O&M")
    ② DART 태그(대문자 ASCII)가 아닌 육안용 홑화살괄호 (<전기말>, <연결현금흐름표>)
    ③ 속성값 안의 이스케이프 안 된 큰따옴표 (ENG=""Agreed amount")
    ②를 보정하지 않고 recover 파서에 넘기면 표의 행 레이블이 조용히 사라진다.
    """
    s = BARE_AMP.sub("&amp;", s)
    s = TAGLIKE.sub(
        lambda mo: mo.group(0) if DART_TAG.fullmatch(mo.group(1))
        else mo.group(0).replace("<", "&lt;"), s)
    s = ATTR.sub(lambda mo: mo.group(1) + mo.group(2).replace('"', "&quot;") + mo.group(3), s)
    return s


def visual_labels(raw: str) -> set:
    """육안용 홑화살괄호 안의 한글 라벨(표 제목·행 레이블)."""
    return {n.strip("<>/ ") for n in TAGLIKE.findall(raw)
            if HANGUL.search(n) and not DART_TAG.fullmatch(n)}


def plain_text(path: str) -> str:
    t = open(path, encoding="utf-8", errors="replace").read()
    if "</style>" in t:
        t = t[t.rfind("</style>") + 8:]
    return WS.sub(" ", TAG_STRIP.sub(" ", t)).strip()


def main():
    if not os.path.isdir(CORPUS):
        sys.exit(f"코퍼스를 찾을 수 없음: {CORPUS} (저장소 루트에서 실행하세요)")

    m = pd.read_json(os.path.join(CORPUS, "manifest.jsonl"), lines=True,
                     dtype={"corp_code": str, "stock_code": str})
    u = pd.read_csv(os.path.join(CORPUS, "universe.csv"),
                    dtype={"corp_code": str, "stock_code": str})

    # ── F01. 필드 × 그룹 결측 ────────────────────────────────────────────
    head("F01. 필드 × doc_group 결측률 — 스키마가 그룹마다 갈리는 지점")
    nul = pd.DataFrame([
        {"field": c, **{g: round(s[c].isna().mean() * 100, 1) for g, s in m.groupby("doc_group")}}
        for c in m.columns]).set_index("field")
    split = nul[(nul.max(axis=1) > 99) & (nul.min(axis=1) < 1)]
    print(split.to_string() if len(split) else "  (그룹별로 갈리는 필드 없음)")
    F["F01_group_split_fields"] = split.index.tolist()

    # ── F02. base_year = 상태/사건 축 ────────────────────────────────────
    head("F02. base_year/base_month 보유 여부 = 상태(State)/사건(Event) 축")
    hasb = m.groupby("doc_group").base_year.apply(lambda s: (~s.isna()).mean() * 100).round(1)
    print(hasb.to_string())
    state = hasb[hasb > 50].index.tolist()
    event = hasb[hasb <= 50].index.tolist()
    print(f"\n  상태(기간 귀속): {state}  {int(m.doc_group.isin(state).sum())}건")
    print(f"  사건(시점 발생): {event}  {int(m.doc_group.isin(event).sum())}건")
    F["F02_state_groups"], F["F02_event_groups"] = state, event

    # ── F03. 엔티티 별칭 ─────────────────────────────────────────────────
    head("F03. 엔티티 별칭 — 질의어가 corp_name과 다르면 매칭 실패")
    al = u[u.corp_name != u.listed_name][["corp_name", "listed_name", "corp_eng_name"]]
    print(al.to_string(index=False))
    F["F03_aliases"] = al.set_index("corp_name").listed_name.to_dict()
    F["F03_alias_doc_count"] = int((m.corp_name != m.listed_name).sum())
    print(f"\n  영향 문서 {F['F03_alias_doc_count']}건")

    # ── F04. 0건 = 정상 ──────────────────────────────────────────────────
    head("F04. 문서 0건 기업 — 결측이 아니라 '없는 게 정답'")
    z = {}
    for col, g in [("n_major", "major"), ("n_exchange", "exchange"),
                   ("n_periodic", "periodic"), ("n_holding", "holding")]:
        z[g] = u[u[col] == 0].corp_name.tolist()
        print(f"  {g:9s} 0건 {len(z[g]):>2}개사  {z[g][:6]}{'…' if len(z[g]) > 6 else ''}")
    F["F04_zero_doc_firms"] = z

    # ── F05. 본문 파일 부재 ──────────────────────────────────────────────
    head("F05. 본문 {rcept_no}.xml 이 없는 문서 — XML 전제 코드가 깨지는 지점")
    no_main, fmt = [], Counter()
    for _, r in m.iterrows():
        d = os.path.join(CORPUS, r.file_path)
        if not os.path.exists(os.path.join(d, f"{r.rcept_no}.xml")):
            files = sorted(f for f in os.listdir(d) if not f.startswith("."))
            no_main.append({"corp": r.corp_name, "rcept_no": str(r.rcept_no),
                            "report_nm": r.report_nm, "files": files})
        fmt[r.file_format] += 1
    print(f"  file_format 분포: {dict(fmt)}")
    print(f"  본문 XML 없음: {len(no_main)}건")
    for x in no_main:
        print(f"    {x['corp']:10s} {x['rcept_no']}  {x['report_nm'][:34]}  → {x['files']}")
    F["F05_no_main_xml"] = no_main

    # ── F06. 실제 포맷 스니핑 ────────────────────────────────────────────
    head("F06. 본문 첫 바이트 스니핑 — 확장자와 실제 내용")
    sniff = defaultdict(Counter)
    for _, r in m.iterrows():
        p = os.path.join(CORPUS, r.file_path, f"{r.rcept_no}.xml")
        if not os.path.exists(p):
            continue
        h = open(p, "rb").read(3000).decode("utf-8", "replace").lstrip().upper()
        kind = ("DART XML(DOCUMENT)" if "<DOCUMENT" in h[:1500]
                else "HTML 폼" if ("<HTML" in h[:300] or "<HEAD" in h[:300]) else "기타")
        sniff[r.doc_group][kind] += 1
    for g, c in sniff.items():
        print(f"  {g:9s} {dict(c)}")
    F["F06_format_by_group"] = {g: dict(c) for g, c in sniff.items()}

    # ── F07/F08. 파싱 건전성 + 파서 비교 ─────────────────────────────────
    head("F07. 표준 XML 파서 통과율 / F08. recover가 버리는 표 라벨")
    scope = pd.concat([
        m[m.doc_group == "periodic"].sample(PERIODIC_SAMPLE, random_state=SEED),
        m[m.doc_group == "major"], m[m.doc_group == "holding"],
        m[m.doc_group == "exchange"].sample(200, random_state=SEED)])
    st = defaultdict(lambda: dict(n=0, strict_ok=0, A_loss_doc=0, C_loss_doc=0,
                                  A_len=0, C_len=0, B2_ok=0))
    lost_labels = Counter()
    for _, r in scope.iterrows():
        p = os.path.join(CORPUS, r.file_path, f"{r.rcept_no}.xml")
        if not os.path.exists(p):
            continue
        raw = open(p, encoding="utf-8", errors="replace").read()
        s = st[r.doc_group]
        s["n"] += 1
        try:
            ET.fromstring(raw)
            s["strict_ok"] += 1
        except ET.ParseError:
            pass
        fixed = repair_xml(raw)
        try:
            ET.fromstring(fixed)
            s["B2_ok"] += 1
        except ET.ParseError:
            pass
        lab = visual_labels(raw)

        def rec(txt):
            try:
                root = etree.fromstring(txt.encode(), etree.XMLParser(
                    recover=True, encoding="utf-8", huge_tree=True))
                return "".join(root.itertext()) if root is not None else ""
            except Exception:
                return ""
        a, c = rec(raw), rec(fixed)
        s["A_len"] += len(a)
        s["C_len"] += len(c)
        la = [t for t in lab if t and t not in a]
        if la:
            s["A_loss_doc"] += 1
            lost_labels.update(la)
        if [t for t in lab if t and t not in c]:
            s["C_loss_doc"] += 1

    print(f"{'group':10s}{'검사':>6}{'표준파서OK':>11}{'보정후OK':>10}"
          f"{'A라벨소실문서':>14}{'C라벨소실문서':>14}")
    for g, s in st.items():
        print(f"{g:10s}{s['n']:>6}{s['strict_ok']:>11}{s['B2_ok']:>10}"
              f"{s['A_loss_doc']:>14}{s['C_loss_doc']:>14}")
    ta, tc = sum(s["A_len"] for s in st.values()), sum(s["C_len"] for s in st.values())
    print(f"\n  추출 텍스트: A(recover만) {ta:,}자 → C(보정+recover) {tc:,}자 "
          f"({(tc/max(ta,1)-1)*100:+.2f}%)")
    print(f"  recover가 소실시킨 표 라벨 {len(lost_labels)}종, 상위: "
          f"{[k for k, _ in lost_labels.most_common(10)]}")
    F["F07_parse"] = {g: dict(s) for g, s in st.items()}
    F["F08_lost_labels"] = dict(lost_labels.most_common(40))
    F["F08_text_gain_pct"] = round((tc / max(ta, 1) - 1) * 100, 2)

    # ── F09. 정정 연결 + 코퍼스 경계 ─────────────────────────────────────
    head("F09. exchange 정정의 원본 소재 — '확인할 수 없음'이 정답인 구간")
    ex = m[m.doc_group == "exchange"]
    corr = ex[ex.is_correction]
    by_corp = {c: g for c, g in ex.groupby("corp_name")}
    stat = Counter()
    out_of_corpus = []
    for _, r in corr.iterrows():
        p = os.path.join(CORPUS, r.file_path, f"{r.rcept_no}.xml")
        if not os.path.exists(p):
            continue
        mo = CORR_DATE.search(plain_text(p))
        if not mo:
            stat["원문에 원본 제출일 필드 없음"] += 1
            continue
        od = int("".join(mo.groups()))
        cand = by_corp[r.corp_name]
        if len(cand[(cand.rcept_dt == od) & (cand.rcept_no != r.rcept_no)]):
            stat["코퍼스 내 원본 매칭"] += 1
        elif od < 20230101:
            stat["원본이 코퍼스 기간 밖"] += 1
            out_of_corpus.append({"corp": r.corp_name, "rcept_no": str(r.rcept_no),
                                  "report_nm": r.report_nm, "orig_dt": od})
        else:
            stat["기간 내인데 원본 부재"] += 1
    tot = sum(stat.values())
    for k, v in stat.most_common():
        print(f"  {v:>4}건 ({v/tot*100:>5.1f}%)  {k}")
    print(f"\n  코퍼스 밖 원본 참조: {len(out_of_corpus)}건 / "
          f"{len({o['corp'] for o in out_of_corpus})}개사")
    print("  상위 기업: " + ", ".join(
        f"{k}({v})" for k, v in Counter(o["corp"] for o in out_of_corpus).most_common(6)))
    F["F09_correction_origin"] = dict(stat)
    F["F09_out_of_corpus_examples"] = out_of_corpus[:40]

    # ── F10. flr_nm 의미 ─────────────────────────────────────────────────
    head("F10. flr_nm(제출인) — holding에서만 발행회사와 다르다")
    t = m.assign(same=(m.flr_nm == m.corp_name)).groupby("doc_group").same.agg(
        n="size", 일치="sum")
    t["일치율%"] = (t.일치 / t.n * 100).round(1)
    print(t.to_string())
    names = set(u.corp_name) | set(u.listed_name)
    inter = sorted({x for x in m[m.doc_group == "holding"].flr_nm if x in names})
    print(f"\n  유니버스 내부 상호 지분 보유(제출인이 70개사 중 하나): {len(inter)}개사")
    print(f"  {inter}")
    F["F10_intra_universe_holders"] = inter

    # ── F11. major 세부유형 복구 ─────────────────────────────────────────
    head("F11. major doc_subtype 전건 결측 — report_nm 괄호로 복구 가능한가")
    mj = m[m.doc_group == "major"].report_nm.str.extract(r"\((.+?)\)$")[0]
    ok = mj.notna().sum()
    print(f"  복구 성공 {ok}/{len(mj)} ({ok/len(mj)*100:.1f}%)")
    print(f"  실패 예: {m[m.doc_group=='major'].report_nm[mj.isna()].tolist()[:3]}")
    print("  상위 유형: " + ", ".join(f"{k}({v})" for k, v in mj.value_counts().head(8).items()))
    F["F11_major_subtype_recovered"] = int(ok)
    F["F11_major_subtypes"] = mj.value_counts().head(30).to_dict()

    # ── F12. 그룹별 시간 커버리지 ────────────────────────────────────────
    head("F12. 그룹별 rcept_dt 커버리지 — periodic만 시작·종료가 다르다")
    m["dt"] = pd.to_datetime(m.rcept_dt.astype(str), format="%Y%m%d", errors="coerce")
    cov = m.groupby("doc_group").dt.agg(["min", "max"])
    print(cov.to_string())
    F["F12_coverage"] = {g: [str(r["min"].date()), str(r["max"].date())]
                         for g, r in cov.iterrows()}

    os.makedirs("eda", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(F, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n\n[저장] {OUT_PATH}")


if __name__ == "__main__":
    main()
