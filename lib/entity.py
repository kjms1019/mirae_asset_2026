"""엔티티 해석 — 질의어와 제출인 이름을 corp_id 로 푼다.

감사 F03: corp_name(법인 등기명)과 listed_name(상장 종목명)이 다른 기업이
6개사, 문서 327건이다. 질의가 "현대차 영업이익"으로 들어오면 corp_name 매칭은
0건을 반환하고, 그 0건이 "자료 없음"으로 위장한다. 검색 실패 중 가장 위험한
유형이라 조회 앞단에서 막는다.

감사 F10: 지분공시 제출인(flr_nm)은 155종이고 외국계는 공백이 제거된 표기다
(TheCapitalGroupCompanies,Inc.). 유니버스 70개사와 겹치는 11개사는 corp_id 로
연결해야 기업 간 지분 관계를 추적할 수 있다.
"""
import re

_PUNCT = re.compile(r"[\s\.\,\-\_\(\)ㆍ·:/&']+")
_SUFFIX = re.compile(r"(주식회사|㈜|\(주\)|co\.?,?\s*ltd\.?|corp(oration)?|inc\.?)", re.I)


def normalize(name: str) -> str:
    """별칭 매칭용 정규화 — 소문자화, 공백·구두점 제거, 법인격 접미사 제거."""
    if not name:
        return ""
    s = str(name).strip().lower()
    s = _SUFFIX.sub("", s)
    s = _PUNCT.sub("", s)
    return s


def alias_rows(universe_row) -> list[tuple[str, str, str, str]]:
    """universe 한 행에서 (alias_norm, alias, corp_id, alias_type) 목록을 만든다."""
    corp_id = str(universe_row["corp_code"])
    out = []
    seen = set()
    for field, kind in (("corp_name", "official"), ("listed_name", "listed"),
                        ("corp_eng_name", "english")):
        value = universe_row.get(field)
        if not value or (isinstance(value, float)):
            continue
        norm = normalize(value)
        if norm and norm not in seen:
            seen.add(norm)
            out.append((norm, str(value), corp_id, kind))
    return out


class Resolver:
    """별칭 테이블을 메모리에 올려 이름 → corp_id 를 푼다."""

    def __init__(self, conn):
        self._map: dict[str, list[str]] = {}
        self._display: dict[str, str] = {}
        for row in conn.execute("SELECT alias_norm, corp_id FROM corp_alias"):
            self._map.setdefault(row["alias_norm"], []).append(row["corp_id"])
        for row in conn.execute("SELECT corp_id, corp_name FROM corps"):
            self._display[row["corp_id"]] = row["corp_name"]

    def resolve(self, name: str) -> str | None:
        """정확 매칭 → 부분 포함 순으로 시도. 모호하면 None 을 반환한다."""
        if not name:
            return None
        norm = normalize(name)
        hit = self._map.get(norm)
        if hit and len(set(hit)) == 1:
            return hit[0]
        if hit:
            return None  # 모호 — 단정하지 않는다
        # 질의문 안에 기업명이 섞여 들어온 경우: 가장 긴 별칭이 포함되면 채택
        cands = [(len(a), ids) for a, ids in self._map.items() if a and a in norm]
        if cands:
            cands.sort(reverse=True)
            best = cands[0][1]
            if len(set(best)) == 1:
                return best[0]
        return None

    def find_all(self, text: str) -> list[str]:
        """자유 텍스트에서 언급된 모든 기업의 corp_id 를 길이 우선으로 찾는다."""
        norm = normalize(text)
        found, used = [], []
        for alias, ids in sorted(self._map.items(), key=lambda kv: -len(kv[0])):
            if not alias or len(alias) < 2 or alias not in norm:
                continue
            if any(alias in u for u in used):
                continue
            cid = ids[0]
            if len(set(ids)) == 1 and cid not in found:
                found.append(cid)
                used.append(alias)
        return found

    def name(self, corp_id: str) -> str:
        return self._display.get(corp_id, corp_id)
