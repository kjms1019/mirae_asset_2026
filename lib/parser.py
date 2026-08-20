"""DART 공시 원문 파서.

코퍼스는 포맷이 세 갈래이고 파서도 세 갈래여야 한다 (EDA v2 감사 F05·F06 참조).

  periodic / major / holding  →  DART DOCUMENT XML (3,732건)
  exchange                    →  확장자만 .xml, 실제 내용은 HTML 폼 (1,469건)
  대체 수집분                  →  XML 없이 PDF + 뷰어 HTML (3건, 처리 불가)

DART XML은 표준 XML 파서로 열리지 않는다. 감사에서 표준 파서 통과율이
periodic 0/70, major 409/598, holding 815/1,083로 나왔다. 원인은 원문 자체의
스키마 위반 세 가지이고, `repair_xml`이 그걸 보정한다. 보정 없이 recover 파서로
넘기면 파싱은 100% 성공하지만 <연결현금흐름표>·<전기말> 같은 육안용 꺾쇠를
태그로 오인해 통째로 버린다 — periodic 문서의 43%에서 표 라벨이 사라졌다.
보정 후 recover로 넘기면 파싱 100% + 라벨 소실 0건이 된다.
"""
import os
import re

from lxml import etree, html as lxml_html

# ── DART XML 보정 ────────────────────────────────────────────────────────────

#: 이스케이프되지 않은 & (예: "O&M")
BARE_AMP = re.compile(r"&(?!(?:[a-zA-Z][a-zA-Z0-9]*|#\d+|#x[0-9a-fA-F]+);)")
#: 태그처럼 보이는 모든 것 — 이 중 DART 태그가 아닌 것은 텍스트로 되돌린다
TAGLIKE = re.compile(r"<\s*/?\s*([^\s/>!?][^\s/>]*)")
#: 속성값 안의 이스케이프되지 않은 큰따옴표 (예: ENG=""Agreed amount")
ATTR_VALUE = re.compile(r'(\s[A-Z][A-Z0-9_]*\s*=\s*")(.*?)("(?=[\s/>]))', re.S)
#: DART 태그명은 전부 대문자 ASCII(+숫자·하이픈)
DART_TAG = re.compile(r"[A-Z][A-Z0-9\-]*")

TOP_SECTION_RE = re.compile(r"^[IVXLC]+\.\s")
CELL_TAGS = ("TD", "TH", "TU", "TE")


def repair_xml(source: str) -> str:
    """DART 원문의 스키마 위반 세 가지를 보정한다.

    ② 보정이 특히 중요하다. 이걸 건너뛰면 recover 파서가 <주요배당지표>,
    <신종자본증권>, <연결현금흐름표> 같은 표 제목·행 레이블을 태그로 오인해
    버리고, 숫자만 남아 무슨 값인지 알 수 없게 된다.
    """
    source = BARE_AMP.sub("&amp;", source)  # ①
    source = TAGLIKE.sub(  # ②
        lambda mo: mo.group(0) if DART_TAG.fullmatch(mo.group(1))
        else mo.group(0).replace("<", "&lt;"),
        source,
    )
    source = ATTR_VALUE.sub(  # ③
        lambda mo: mo.group(1) + mo.group(2).replace('"', "&quot;") + mo.group(3),
        source,
    )
    return source


def parse_dart_xml(path: str):
    """DART DOCUMENT XML을 파싱해 루트 엘리먼트를 반환한다 (보정 + recover)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    parser = etree.XMLParser(recover=True, encoding="utf-8", huge_tree=True)
    return etree.fromstring(repair_xml(raw).encode("utf-8"), parser)


#: 기존 호출부 호환용 별칭
parse_xml = parse_dart_xml


# ── DART XML 렌더링 ──────────────────────────────────────────────────────────


def table_to_rows(table_el) -> list[list[str]]:
    """TABLE 엘리먼트를 행 단위 셀 리스트로 변환한다."""
    rows = []
    for tr in table_el.iter("TR"):
        cells = []
        for cell in tr:
            if cell.tag in CELL_TAGS:
                text = " ".join("".join(cell.itertext()).split())
                cells.append(text)
        if any(cells):
            rows.append(cells)
    return rows


def _render(el) -> str:
    if el.tag == "TABLE":
        rows = table_to_rows(el)
        body = "\n".join(" | ".join(r) for r in rows)
        return f"\n[표]\n{body}\n"
    parts = [el.text or ""]
    for child in el:
        parts.append(_render(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _clean(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*\n+", "\n\n", text)
    return text.strip()


def render(el) -> str:
    """엘리먼트 하위 전체를 텍스트로. 표는 행 단위로 " | " 구분해 보존한다."""
    return _clean(_render(el))


def _title_text(el) -> str | None:
    for child in el.iter("TITLE"):
        if child.get("ATOC") == "Y":
            return " ".join("".join(child.itertext()).split())
    return None


def get_top_sections(root, max_chars: int = 15000) -> dict[str, str]:
    """I~XII 최상위 섹션을 {제목: 본문}으로 반환한다.

    일부 대기업은 섹션 하나에 정관 전문·주식발행 이력이 통째로 들어 있어
    수십만 자에 달한다(카카오 "I. 회사의 개요"). max_chars로 자른다.
    """
    out = {}
    for section in root.iter("SECTION-1"):
        title = _title_text(section)
        if title and TOP_SECTION_RE.match(title):
            out[title] = render(section)[:max_chars]
    return out


def get_section_by_title_suffix(root, suffix: str, max_chars: int = 6000) -> str | None:
    """제목이 suffix로 끝나는 섹션 하나를 찾아 본문을 반환한다.

    SECTION 중첩 깊이가 기업마다 달라서, 좁은 SECTION-2부터 찾고 없으면
    SECTION-1로 넓힌다. 의도보다 큰 컨테이너가 잡히는 경우가 있어 max_chars로
    자른다 — 요약 표는 섹션 앞부분에 있으므로 잘려도 표는 보존된다.
    """
    for tag in ("SECTION-2", "SECTION-1"):
        for section in root.iter(tag):
            title = _title_text(section)
            if title and title.strip().endswith(suffix):
                return render(section)[:max_chars]
    return None


def find_tables_near(root, keyword: str, limit: int = 3) -> list[list[list[str]]]:
    """제목/캡션에 keyword가 있는 섹션 안의 표들을 행 단위로 반환한다."""
    found = []
    for tag in ("SECTION-2", "SECTION-1"):
        for section in root.iter(tag):
            title = _title_text(section)
            if title and keyword in title:
                for table in section.iter("TABLE"):
                    rows = table_to_rows(table)
                    if rows:
                        found.append(rows)
                    if len(found) >= limit:
                        return found
    return found


# ── exchange HTML 폼 파서 ────────────────────────────────────────────────────

_CORR_DATE = re.compile(
    r"정정관련\s*공시서류\s*제출일\s*([0-9]{4})[-.\s]?([0-9]{2})[-.\s]?([0-9]{2})")


def _cell_text(td) -> str:
    return " ".join(td.text_content().split())


def _is_value_cell(td) -> bool:
    """값 셀은 span.xforms_input 을 품고 있다."""
    return bool(td.xpath('.//*[contains(@class, "xforms_input")]'))


def parse_html_form(path: str) -> dict:
    """거래소공시(HTML 폼)를 구조화해 반환한다.

    반환 키
      title       문서 제목
      fields      {라벨: 값} — rowspan 계층 라벨은 "상위 - 하위"로 합침
      corrections [{항목, 정정전, 정정후}] — 정정본에만 존재
      corr_meta   {정정일자, 정정관련_공시서류, 원본_제출일, 정정사유}
      text        읽기용 평문
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        doc = lxml_html.fromstring(f.read())

    title_el = doc.xpath("//title")
    title = " ".join(title_el[0].text_content().split()) if title_el else ""

    fields: dict[str, str] = {}
    corrections: list[dict] = []

    for table in doc.xpath("//table"):
        rowspan_prefix: list[str] = []   # [(남은 행 수, 라벨)] 을 평탄화해 관리
        pending: list[list] = []
        header = None

        for tr in table.xpath(".//tr"):
            tds = tr.xpath("./td")
            if not tds:
                continue
            texts = [_cell_text(td) for td in tds]

            # 정정 비교표: 헤더가 (정정항목, 정정전, 정정후)
            if header is None and len(texts) >= 3 and "정정전" in texts and "정정후" in texts:
                header = texts
                continue
            if header is not None:
                if len(texts) >= 3 and any(texts):
                    corrections.append({
                        "항목": texts[0], "정정전": texts[1], "정정후": texts[2]})
                continue

            # 살아있는 rowspan 라벨을 앞에 붙인다
            prefix = [lbl for cnt, lbl in pending if cnt > 0]
            pending = [[cnt - 1, lbl] for cnt, lbl in pending if cnt - 1 > 0]

            labels, values = [], []
            for td, text in zip(tds, texts):
                span = int(td.get("rowspan") or 1)
                if _is_value_cell(td):
                    values.append(text)
                else:
                    if span > 1:
                        pending.append([span - 1, text])
                    labels.append(text)

            if not values:
                continue
            key_parts = [p for p in prefix + labels if p]
            key = " - ".join(key_parts) if key_parts else "(무명)"
            value = " ".join(v for v in values if v).strip()
            if key in fields and fields[key]:
                fields[f"{key} #{sum(k.startswith(key) for k in fields) + 1}"] = value
            else:
                fields[key] = value

    text = " ".join(doc.text_content().split())
    corr_meta = {}
    if "정정" in text:
        mo = _CORR_DATE.search(text)
        if mo:
            corr_meta["원본_제출일"] = int("".join(mo.groups()))
        for label, key in [("정정일자", "정정일자"),
                           ("정정관련 공시서류", "정정관련_공시서류"),
                           ("정정사유", "정정사유")]:
            for fk, fv in fields.items():
                if fk.replace(" ", "").endswith(label.replace(" ", "")):
                    corr_meta[key] = fv
                    break

    return {"title": title, "fields": fields, "corrections": corrections,
            "corr_meta": corr_meta, "text": text}


# ── 포맷 라우팅 ──────────────────────────────────────────────────────────────

FORMAT_DART_XML = "dart_xml"
FORMAT_HTML_FORM = "html_form"
FORMAT_UNSUPPORTED = "unsupported"


def resolve_document(corpus_root: str, file_path: str, rcept_no) -> tuple[str, str | None]:
    """(포맷, 본문파일경로)를 반환한다. 본문 XML이 없으면 (unsupported, None).

    대체 수집 3건(KB금융·한화오션·한화에어로스페이스)은 PDF와 뷰어 HTML만 있어
    여기서 걸러진다. 경로를 가정하지 않고 실제 디렉토리를 확인하는 이유다.
    """
    directory = os.path.join(corpus_root, file_path)
    main = os.path.join(directory, f"{rcept_no}.xml")
    if not os.path.exists(main):
        return FORMAT_UNSUPPORTED, None
    with open(main, "rb") as f:
        head = f.read(2048).decode("utf-8", "replace").lstrip().upper()
    if "<HTML" in head[:400] or "<HEAD" in head[:400]:
        return FORMAT_HTML_FORM, main
    return FORMAT_DART_XML, main
