"""데모용 인덱스 빌드 스크립트.

10개 데모 기업의 최신 사업보고서(FY2025)에서 4개 섹션(회사의 개요 / 사업의 내용 /
요약재무정보 / 경영진단 및 분석의견)을 뽑아 청킹하고 Gemini로 임베딩해서
index/chunks.json + index/embeddings.npy에 저장한다.

실행: python build_index.py  (.env에 GEMINI_API_KEY 필요, 저장소 루트에서 실행)
"""
import os

from dotenv import load_dotenv

load_dotenv()

import pandas as pd

from lib.chunk import chunk_text
from lib.gemini_client import embed_texts
from lib.index import save_index
from lib.parser import get_section_by_title_suffix, get_top_sections, parse_xml

CORPUS = os.path.join("data", "3.공시", "corpus")

DEMO_COMPANIES = [
    "삼성전자", "SK하이닉스", "현대자동차", "기아", "NAVER",
    "카카오", "LG에너지솔루션", "삼성SDI", "KB금융", "하이브",
]

SECTIONS_TO_INDEX = ["I. 회사의 개요", "II. 사업의 내용", "IV. 이사의 경영진단 및 분석의견"]
NESTED_SECTION_SUFFIX = "요약재무정보"


def build_chunks() -> list[dict]:
    manifest = pd.read_json(
        os.path.join(CORPUS, "manifest.jsonl"), lines=True,
        dtype={"corp_code": str, "stock_code": str},
    )
    latest = manifest[
        manifest.corp_name.isin(DEMO_COMPANIES)
        & (manifest.doc_subtype == "annual")
        & (~manifest.is_correction)
    ]
    latest = latest.sort_values("rcept_dt").groupby("corp_name").tail(1)

    all_chunks = []
    for _, row in latest.iterrows():
        xml_path = os.path.join(CORPUS, row["file_path"], f"{row['rcept_no']}.xml")
        print(f"파싱: {row['corp_name']} ({row['report_nm']})")
        root = parse_xml(xml_path)

        top_sections = get_top_sections(root)
        texts_to_chunk = []
        for title in SECTIONS_TO_INDEX:
            if title in top_sections:
                texts_to_chunk.append((title, top_sections[title]))

        summary = get_section_by_title_suffix(root, NESTED_SECTION_SUFFIX)
        if summary:
            texts_to_chunk.append((f"III. 재무에 관한 사항 > {NESTED_SECTION_SUFFIX}", summary))

        for section_title, text in texts_to_chunk:
            for chunk in chunk_text(text):
                all_chunks.append(
                    {
                        "text": chunk,
                        "corp_name": row["corp_name"],
                        "report_nm": row["report_nm"],
                        "rcept_no": str(row["rcept_no"]),
                        "rcept_dt": str(row["rcept_dt"]),
                        "section": section_title,
                    }
                )
    return all_chunks


def main():
    chunks = build_chunks()
    print(f"총 청크 수: {len(chunks)}")
    print("임베딩 생성 중 (Gemini API 호출)...")
    embeddings = embed_texts([c["text"] for c in chunks])
    save_index(chunks, embeddings)
    print(f"저장 완료: index/chunks.json, index/embeddings.npy ({len(chunks)}개 청크)")


if __name__ == "__main__":
    main()
