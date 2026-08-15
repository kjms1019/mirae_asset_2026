"""DART DOCUMENT XML(정기공시 원문) 파서.

정기공시(사업/반기/분기보고서)는 SECTION-1/SECTION-2 태그로 목차가 구획되고,
각 섹션의 첫 TITLE(ATOC="Y")이 그 섹션의 제목이다. 표는 TABLE > TR > TD/TH/TU
구조로, 계정과목-금액 정렬이 살아있으므로 행 단위로 " | "를 넣어 텍스트화하면
숫자와 라벨이 뒤섞이지 않는다.
"""
import re

from lxml import etree

TOP_SECTION_RE = re.compile(r"^[IVXLC]+\.\s")


def parse_xml(path: str):
    parser = etree.XMLParser(recover=True, encoding="utf-8", huge_tree=True)
    with open(path, "rb") as f:
        return etree.parse(f, parser).getroot()


def _table_to_text(table_el) -> str:
    lines = []
    for tr in table_el.iter("TR"):
        cells = []
        for cell in tr:
            if cell.tag in ("TD", "TH", "TU", "TE"):
                text = "".join(cell.itertext()).strip()
                if text:
                    cells.append(text)
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _render(el) -> str:
    if el.tag == "TABLE":
        return "\n[표]\n" + _table_to_text(el) + "\n"
    parts = [el.text or ""]
    for child in el:
        parts.append(_render(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _clean(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*\n+", "\n\n", text)
    return text.strip()


def _title_text(el) -> str | None:
    for child in el.iter("TITLE"):
        if child.get("ATOC") == "Y":
            return "".join(child.itertext()).strip()
    return None


def get_top_sections(root, max_chars: int = 15000) -> dict[str, str]:
    """I~XII 최상위 섹션(로마숫자 제목)을 {제목: 본문텍스트}로 반환.

    max_chars로 섹션당 길이를 제한한다 — "회사의 개요"에 정관 전문이나 주식발행
    이력이 통째로 들어있는 등 일부 대기업은 섹션 하나가 수십만 자에 달해
    (예: 카카오 "I. 회사의 개요"가 태그 9만개 이상) 데모 스코프에는 과함.
    """
    out = {}
    for section in root.iter("SECTION-1"):
        title = _title_text(section)
        if title and TOP_SECTION_RE.match(title):
            out[title] = _clean(_render(section))[:max_chars]
    return out


def get_section_by_title_suffix(root, suffix: str, max_chars: int = 6000) -> str | None:
    """제목이 suffix로 끝나는 섹션(SECTION-1 또는 SECTION-2) 하나를 찾아 본문 반환.

    "요약재무정보"처럼 큰 섹션(III. 재무에 관한 사항) 안에 중첩된 작은 하위섹션을
    통째로(주석 등 거대 테이블 없이) 뽑아낼 때 사용.

    주의: DART XML의 SECTION 중첩 깊이는 기업/보고서마다 조금씩 달라서(표준 목차는
    동일해도 내부 트리 구조는 다를 수 있음), _title_text가 이 섹션의 "첫 하위 제목"을
    상위 섹션의 제목으로 잘못 반환하는 경우가 있다 (예: 하이브 사업보고서에서 관측됨 —
    요약재무정보보다 훨씬 큰 상위 컨테이너가 매칭됨). max_chars로 결과를 잘라 청크
    폭주를 막는다 — 요약 표는 항상 섹션 앞부분에 있으므로 잘려도 표 자체는 보존된다.
    """
    for tag in ("SECTION-2", "SECTION-1"):
        for section in root.iter(tag):
            title = _title_text(section)
            if title and title.strip().endswith(suffix):
                text = _clean(_render(section))
                return text[:max_chars]
    return None
