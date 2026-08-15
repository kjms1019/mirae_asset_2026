# mirae_asset_2026 — 공시 Agent

제10회 2026 미래에셋증권 AI Festival 참가 프로젝트. 과제는 **공시 Agent(Disclosure Analyst)** —
DART 공시 데이터를 기반으로 자연어 질의에 검색·비교·계산·근거기반 답변을 하는 AI Agent 구현.

## 대회 핵심 규정

- **LLM은 HyperCLOVA X만 사용 가능.** 다른 LLM 사용 시 평가 제외.
- 제공된 코퍼스 외 데이터(뉴스·리포트·위키 등) 사용 불가, OpenDART 등 외부 API 실시간 호출 불가.
- 근거 없는 미래예측·투자의견 생성 금지. 확인 불가 시 "확인할 수 없음" 명시 필수.
- 모든 답변에 근거 공시 표시 필수.
- 예선 제출(3종, **마감 09.06**): 소스코드+Dockerfile+README / 기술제안서 / 평가용 API 서버(End-point+스키마).
  마감 후 커밋·push 등 변경 시 실격.

## 저장소 구조

```
├── data/3.공시/corpus/   # 주최측 제공 DART 공시 코퍼스 (gitignore — 용량 5.15GB, 구글드라이브 별도 보관)
├── 과제소개자료/          # 대회 설명자료 PDF (gitignore)
├── eda/
│   ├── generate_eda.py   # EDA 재현 스크립트 (data/ 필요, 저장소 루트에서 실행)
│   ├── eda_summary.json  # 스크립트 실행 결과 (수치 검증용)
│   └── report.html       # EDA 시각화 리포트 — 브라우저로 열어서 확인 (자체 데이터 내장, 독립 실행)
├── lib/                   # 파서(XML→텍스트) · 청킹 · Gemini 클라이언트 · 검색 인덱스
├── build_index.py         # 데모 인덱스 빌드 스크립트 (data/ 필요)
├── app.py                 # Streamlit 데모 앱 (Gemini 기반, 최종 제출용 아님)
├── index/                 # build_index.py 산출물 (청크 551개 + 임베딩, git에 포함 — 재빌드 없이 바로 실행 가능)
├── eval/
│   └── eval_set.json      # 검증용 질문-정답 셋 (실데이터 검증 완료, 13문항)
├── requirements.txt
├── .env.example
└── README.md
```

`data/`와 `과제소개자료/`, 그리고 개인적으로 참고용으로 받아둔 SK하이닉스 공시 PDF는 git에 올리지 않습니다
(Google Drive에 별도 보관). 로컬에서 분석할 때는 `data/3.공시/corpus/README.md`·`data_filter.md`를 먼저 확인하세요.

## 데이터셋 요약

- **기업 유니버스**: 70개사(KOSPI 61 / KOSDAQ 9), 20개 섹터, `universe.csv`/`.xlsx`
- **문서**: 4,204건 (XML 4,616개, 5.15GB), `manifest.jsonl`에 문서별 메타데이터
  - 정기공시 1,054 · 주요사항보고서 598 · 거래소공시 1,469 · 지분공시 1,083
  - 정정공시 1,004건은 원본과 병행 수집 (`is_correction` 플래그)
- 기간: 2023.01 ~ 2026.03(1분기)

## EDA 핵심 발견

전체 리포트는 [`eda/report.html`](./eda/report.html)을 브라우저로 열어서 확인 (차트·수치 전부 포함, 인터넷 연결 불필요).

1. **무결성 100%** — universe.csv의 문서 건수 선언과 manifest.jsonl 실제 집계가 70개사 전원 완전 일치.
2. **지분공시(holding) 스키마 함정** — 1,083건 전건에서 `flr_nm`(제출인=실제 대량보유자, 예: 국민연금·
   BlackRock·계열사)이 `corp_name`(발행회사)과 다름. "누가 보유했나" 질의에는 flr_nm이 핵심 엔터티.
3. **거래소공시(exchange)만 HTML** — 확장자는 `.xml`이지만 실제 내용은 span 기반 HTML 폼. 나머지
   3종(periodic/major/holding)은 DART 고유 DOCUMENT XML 스키마 — 최소 2개 파서 필요.
4. **정기공시 목차는 업종 무관 100% 동일** — 반도체·은행·보험·증권 4개 업종 비교 결과 I~XII 표준 목차
   완전 동일. 섹션 기반 파서 하나로 70개사 전체 커버 가능.
5. **정정공시 비율 편차 큼** — doc_group별 거래소공시 43% > 주요사항보고서 28.9% > 정기공시 15.1% >
   지분공시 3.8%. 기업별로는 삼성E&A(63.8%)·삼성바이오로직스(60.7%)·현대건설(55.6%) 등 건설/EPC 계열이
   최상위 — 원본·정정본을 시간순으로 연결하는 로직 없이는 "최신 계약조건" 류 질의가 오답 위험.

## Gemini 데모 (팀 내부 검증용, 최종 제출 아님)

**주의: 대회 규정상 최종 제출물의 LLM은 HyperCLOVA X만 허용됩니다.** 이 데모는 팀이 RAG 파이프라인과
전처리 구조를 빠르게 검증하기 위해 Gemini로 대체 구현한 것이며, 예선/본선 제출용 코드가 아닙니다.

- **범위**: 10개사(삼성전자·SK하이닉스·현대자동차·기아·NAVER·카카오·LG에너지솔루션·삼성SDI·KB금융·하이브)의
  최신(FY2025) 사업보고서만 인덱싱. 섹션: 회사의 개요 / 사업의 내용 / 요약재무정보 / 경영진단 및 분석의견.
- **구조**: XML 파싱(`lib/parser.py`, DART DOCUMENT 스키마의 SECTION/TITLE/TABLE 태그를 표 구조 보존하며
  텍스트화) → 섹션 단위 청킹(`lib/chunk.py`) → Gemini 임베딩(`gemini-embedding-001`) → numpy 코사인 유사도
  검색 → Gemini(`gemini-2.5-flash`) 답변 생성 + 근거 표시. 청크 수(551개) 규모에서는 별도 벡터 DB 없이
  인메모리 검색으로 충분해서 최대한 단순하게 구성.
- **로컬 실행**:
  ```bash
  pip install -r requirements.txt
  cp .env.example .env   # GEMINI_API_KEY 채워넣기
  python build_index.py  # 이미 index/에 빌드되어 있어 보통 생략 가능. 데모 범위(10개사)를 바꿀 때만 재실행
  streamlit run app.py
  ```
- **알려진 한계**: 순수 RAG만으로는 정확한 수치를 못 찾고 근처 섹션(경영진단 등)에서 우연히 맞히는 경우가
  있음을 실제로 확인함 — `eval/eval_set.json`의 Q-07 검증 중 발견. 이게 우리가 DB 설계 논의에서 결론 낸
  "closed형 수치 질의는 구조화 팩트 테이블이 필요하다"는 주장의 실증 근거.

## 평가셋 (`eval/eval_set.json`)

대회 공식 평가문제는 비공개이므로, PDF의 참고용 질의 set(6개 유형: 검색및정보추출/다중조회및비교연산/
복합문서추론 × closed/open)을 본떠 우리 데모 10개사의 실제 원문에서 검증한 13문항을 자체 제작했다.
모든 closed 문항의 `expected_answer`는 원문 XML을 직접 파싱해 사람이 대조 검증함. 특히:

- **Q-07**: "삼성전자와 SK하이닉스 중 2025년 영업이익이 더 큰 곳은?" → 정답은 **SK하이닉스**(47.2조 >
  43.6조) — 모델이 사전지식으로 "삼성전자가 항상 크다"고 안일하게 답하면 틀리는 함정 문항.
- **Q-12**: LG에너지솔루션의 Ford/Freudenberg 배터리 공급계약이 체결(2024) → 정정(공시유보 해제) →
  해지(2025)로 이어지는 실제 체인을 확인. Freudenberg 건은 정정공시와 해지공시의 금액이 서로 달라
  (4,082,377백만원 vs 3,921,711백만원) — 근거기반 정확성을 테스트하는 실전 트랩 케이스.

## 참고 (2025년 전년도 대회)

- https://github.com/SeoroMin/mirae_AGENT — 9회(2025) "Financial Agent" 과제 팀 저장소. Agent 구조도,
  HyperCLOVA X 인증 헤더 포함 평가 API 호출 예시가 README에 정리되어 있어 API 서버 설계 참고할만함.

## 현재 상태 / 다음 단계

- [x] 대회 요강 파악, 데이터셋 EDA 완료
- [x] DB/전처리 설계 방향 논의 (구조화 팩트 테이블 + 벡터 검색 + 얕은 관계 엣지 하이브리드)
- [x] Gemini 기반 데모(RAG) + 검증용 평가셋 구축
- [ ] 실제 제출용 파이프라인: HyperCLOVA X로 교체, 재무제표 구조화 추출 확대
- [ ] Agent 아키텍처(툴콜링/라우팅) 설계
- [ ] 평가용 API 서버 스펙 확정
- [ ] Streamlit Cloud 배포
