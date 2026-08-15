"""섹션 텍스트를 임베딩용 청크로 분할."""


def chunk_text(text: str, max_chars: int = 1200, overlap: int = 150) -> list[str]:
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 <= max_chars:
            buf = f"{buf}\n\n{p}" if buf else p
            continue
        if buf:
            chunks.append(buf)
        if len(p) <= max_chars:
            buf = p
        else:
            # 문단 자체가 너무 길면 강제로 슬라이딩 윈도우 분할
            start = 0
            while start < len(p):
                end = start + max_chars
                chunks.append(p[start:end])
                start = end - overlap
            buf = ""
    if buf:
        chunks.append(buf)
    return chunks
