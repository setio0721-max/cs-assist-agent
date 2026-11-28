# CS 답변 보조 에이전트 (Graph RAG · Windows 데스크톱 프로그램)

「AI 기반 마케팅과 브랜드 전략」 Term Project — 생리대 쇼핑몰 판매자가 고객 문의를 붙여넣으면
상품 정보·정책·과거 문의 이력에 근거한 **출처 포함 답변 초안**을 추천하는 데스크톱 프로그램입니다.
2주차 실습 앱(`marketing-kg-agent`)의 그래프 RAG 구조를 기반으로 확장했습니다.

> 수업용 가상 설정: 브랜드 **온솜결**, 회사 **(주)온결리빙**은 임의로 정한 이름입니다.

## 구성
| 경로 | 내용 |
|---|---|
| `desktop_app.py` | 데스크톱 프로그램(항상 위 작은 창, 붙여넣기 → 추천 → 답변 복사, 설정·운영 설정 탭, 알림 배너) |
| `cs_core.py` | 핵심 로직(마스킹, 규정 검사, bge-m3 임베딩, Qdrant REST + Neo4j Query API 하이브리드 검색, Claude tool-use 에이전트) |
| `cs_ops.py` | 운영 정보·알림(소비기한, 상품 규격, 명절 배송 공지, 알림 계산) — Neo4j `:OpsConfig`에 저장 |
| `.github/workflows/build-desktop.yml` | Windows 포터블 exe 자동 빌드(PyInstaller) → Artifacts / Releases zip |
| `desktop/` | exe와 함께 배포하는 `사용법.txt`, `.env.example` |
| `notebooks/cs_kb_ingest.ipynb` | Colab 적재 노트북(청킹 → 개체·관계 추출 → 정합 → Neo4j → bge-m3 → Qdrant) |
| `notebooks/build_notebook.py` | 노트북 생성 스크립트 |
| `data/products.md`, `data/policy.md`, `data/cs_history.xlsx` | 지식 원천 자료 |

## 주요 기능
- 하이브리드 검색: Qdrant 벡터 top-5 → Neo4j 관계 최대 10개 확장
- 대화 기록 유지(최근 6개 메시지), 첫 응답 검색 도구 강제·형식 오류 시 1회 재작성
- 개인정보 마스킹(Claude 호출 전), 의약외품 효능 표현 규정 검사(경고)
- **운영 설정**: 재고 소비기한·상품 규격·명절 배송 공지를 프로그램에서 수정 → 다음 답변부터 반영(지식 재적재 불필요, 지식 문서보다 우선)
- **알림 배너**: 소비기한 임박, 소비기한 장기 미확인, 명절 2주 전 공지 미등록, 배송 공지 적용 중

## 실행
1. Colab에서 `notebooks/cs_kb_ingest.ipynb`를 열고 Secrets 9개 등록 후 전체 실행(지식 적재)
   `ANTHROPIC_API_KEY`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`, `QDRANT_URL`, `QDRANT_API_KEY`, `CF_ACCOUNT_ID`, `CF_API_TOKEN`
2. Actions → *Build desktop app (Windows)* → Run workflow → Artifacts의 zip 내려받기(또는 Releases)
3. 압축 해제 → `CSAssistant.exe` 실행 → [설정]에 같은 키 입력하거나 exe 옆에 `.env` 배치
- 소스로 실행: `pip install -r requirements.txt` 후 `python desktop_app.py`(Python 3.12, tkinter 필요)
