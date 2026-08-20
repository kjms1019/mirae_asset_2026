"""어휘 검색 (BM25).

공시 도메인은 식별자가 지배적이다 — 접수번호, 법인명, 계정과목명, 섹션 제목.
이런 건 임베딩 유사도보다 정확 매칭이 낫고, 규정상으로도 안전하다(주최측 Q&A:
형태소 분석기·규칙 기반 NLP는 사용 제한 없음, 임베딩·리랭커는 답변 보류).
그래서 어휘 검색을 1차로 두고 벡터는 서술형 보강용으로만 쓴다.

한국어는 형태소 분석기 없이도 음절 bigram 으로 상당한 정확도가 나온다.
Kiwi/Mecab 을 붙이면 더 올라가지만 의존성 없이 먼저 동작하게 해둔다.
"""
import math
import re
from collections import Counter

_LATIN_NUM = re.compile(r"[A-Za-z]{2,}|\d[\d,\.]*")
_HANGUL = re.compile(r"[가-힣]+")


def tokenize(text: str) -> list[str]:
    """라틴 단어·숫자는 그대로, 한글은 음절 bigram 으로."""
    if not text:
        return []
    lowered = text.lower()
    tokens = [t.replace(",", "") for t in _LATIN_NUM.findall(lowered)]
    for run in _HANGUL.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


class BM25:
    """메모리 상 BM25. 청크 수만 개 규모에서는 전용 엔진이 필요 없다."""

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf: list[Counter] = []
        self.len: list[int] = []
        df: Counter = Counter()
        for text in docs:
            toks = tokenize(text)
            counter = Counter(toks)
            self.tf.append(counter)
            self.len.append(len(toks))
            df.update(counter.keys())
        self.n = len(docs)
        self.avg = (sum(self.len) / self.n) if self.n else 0.0
        self.idf = {
            term: math.log(1 + (self.n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }
        self.postings: dict[str, list[int]] = {}
        for idx, counter in enumerate(self.tf):
            for term in counter:
                self.postings.setdefault(term, []).append(idx)

    def search(self, query: str, top_k: int = 10,
               allowed: set[int] | None = None) -> list[tuple[int, float]]:
        terms = tokenize(query)
        if not terms:
            return []
        scores: dict[int, float] = {}
        for term in set(terms):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for idx in self.postings.get(term, ()):
                if allowed is not None and idx not in allowed:
                    continue
                freq = self.tf[idx][term]
                denom = freq + self.k1 * (
                    1 - self.b + self.b * self.len[idx] / (self.avg or 1))
                scores[idx] = scores.get(idx, 0.0) + idf * (freq * (self.k1 + 1)) / denom
        return sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]
