"""
공시 Agent 데이터셋 EDA — data/3.공시/corpus 전수 분석.
실행: python eda/generate_eda.py  (저장소 루트에서 실행, data/ 필요)
출력: eda/eda_summary.json (report.html이 참조하는 데이터)
"""
import json
import os

import pandas as pd

CORPUS = os.path.join(os.path.dirname(__file__), "..", "data", "3.공시", "corpus")


def main():
    universe = pd.read_csv(
        os.path.join(CORPUS, "universe.csv"),
        dtype={"corp_code": str, "stock_code": str},
    )
    manifest = pd.read_json(
        os.path.join(CORPUS, "manifest.jsonl"),
        lines=True,
        dtype={"corp_code": str, "stock_code": str},
    )

    out = {}

    out["industry_counts"] = universe["industry"].value_counts().to_dict()
    out["sector_counts"] = (
        universe.groupby(["industry", "sector"]).size().reset_index(name="n").to_dict("records")
    )
    out["market_cap_top15"] = universe.nlargest(15, "market_cap")[
        ["corp_name", "market_cap"]
    ].to_dict("records")

    universe["listing_year"] = pd.to_datetime(universe["listing_date"]).dt.year
    out["listing_year_hist"] = universe["listing_year"].value_counts().sort_index().to_dict()

    out["doc_group_counts"] = manifest["doc_group"].value_counts().to_dict()
    out["doc_subtype_counts"] = (
        manifest["doc_subtype"].fillna("(주요사항보고서 세부유형)").value_counts().to_dict()
    )

    manifest["rcept_ym"] = manifest["rcept_dt"].astype(str).str[:6]
    out["monthly_counts"] = manifest["rcept_ym"].value_counts().sort_index().to_dict()

    out["correction_rate_by_group"] = (
        (manifest.groupby("doc_group")["is_correction"].mean() * 100).round(1).to_dict()
    )

    corr = manifest.groupby("corp_name")["is_correction"].agg(["sum", "count"])
    corr["rate"] = (corr["sum"] / corr["count"] * 100).round(1)
    out["correction_rate_top10"] = (
        corr.sort_values("rate", ascending=False).head(10).reset_index().to_dict("records")
    )

    # 무결성 검증: universe 선언 건수 vs manifest 실제 집계
    actual = manifest.groupby(["corp_name", "doc_group"]).size().unstack(fill_value=0)
    uni_idx = universe.set_index("corp_name")
    mismatches = []
    for c in ["periodic", "major", "exchange", "holding"]:
        declared = uni_idx["n_" + c]
        real = actual[c].reindex(uni_idx.index).fillna(0) if c in actual.columns else 0
        diff = declared - real
        mismatches += diff[diff != 0].index.tolist()
    out["integrity_mismatches"] = sorted(set(mismatches))  # 비어있어야 정상

    # 지분공시 제출인 vs 발행회사 (전건 다름이어야 정상)
    holding = manifest[manifest.doc_group == "holding"]
    out["holding_filer_neq_company"] = int((holding["flr_nm"] != holding["corp_name"]).sum())
    out["holding_total"] = int(len(holding))

    def main_xml_size(row):
        if row["file_format"] != "xml":
            return None
        p = os.path.join(CORPUS, row["file_path"], f"{row['rcept_no']}.xml")
        return os.path.getsize(p) if os.path.exists(p) else None

    manifest["main_size"] = manifest.apply(main_xml_size, axis=1)
    out["filesize_by_group_kb"] = (
        (manifest.groupby("doc_group")["main_size"].median() / 1024).round(1).to_dict()
    )
    out["filesize_by_group_mean_kb"] = (
        (manifest.groupby("doc_group")["main_size"].mean() / 1024).round(1).to_dict()
    )
    out["filesize_by_group_max_kb"] = (
        (manifest.groupby("doc_group")["main_size"].max() / 1024).round(1).to_dict()
    )

    # TABLE 태그 밀도는 doc_group당 20건 샘플(속도상 이유) — 값은 report.html에 하드코딩된 것과 별도 재현 시 변동 가능
    out["table_density_note"] = (
        "doc_group당 랜덤 샘플 20건의 <TABLE(periodic/major/holding) 또는 "
        "<table(exchange, 소문자 HTML) 태그 개수 평균. random_state=42."
    )

    out["totals"] = {
        "n_companies": len(universe),
        "n_docs": len(manifest),
        "n_kospi": int((universe.market == "KOSPI").sum()),
        "n_kosdaq": int((universe.market == "KOSDAQ").sum()),
        "n_correction": int(manifest.is_correction.sum()),
    }

    out_path = os.path.join(os.path.dirname(__file__), "eda_summary.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)

    print(f"무결성 불일치: {out['integrity_mismatches'] or '없음'}")
    print(f"지분공시 제출인≠발행회사: {out['holding_filer_neq_company']}/{out['holding_total']}")
    print(f"저장: {out_path}")


if __name__ == "__main__":
    main()
