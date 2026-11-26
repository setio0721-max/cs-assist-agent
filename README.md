# CS 답변 보조 챗봇 (Graph RAG)

「AI 기반 마케팅과 브랜드 전략」 Term Project — 생리대 쇼핑몰 판매자가 고객 문의를 붙여넣으면
상품 정보·정책·과거 문의 이력에 근거한 **출처 포함 답변 초안**을 만들어 주는 내부용 챗봇입니다.
2주차 실습 앱(`marketing-kg-agent`)을 기반으로 확장했습니다.

> 수업용 가상 설정: 브랜드 **온솜결**, 회사 **(주)온결리빙**은 임의로 정한 이름입니다.

## 구성
| 경로 | 내용 |
|---|---|
| `streamlit_app.py` | 챗봇 앱(로그인, 하이브리드 검색, Claude tool-use 에이전트, 대화 기록, 개인정보 마스킹, 규정 검사) |
| `notebooks/cs_kb_ingest.ipynb` | Colab 적재 노트북(청킹 → 개체·관계 추출 → 정합 → Neo4j → bge-m3 → Qdrant) |
| `notebooks/build_notebook.py` | 노트북 생성 스크립트 |
| `data/products.md`, `data/policy.md`, `data/cs_history.xlsx` | 지식 원천 자료 |

## 2주차 앱 대비 변경점
- 검색 대상: CS 컬렉션 3종(`chunks_cs_products`, `chunks_cs_policy`, `chunks_cs_history`)만 검색
- 대화 기록 유지: 최근 6개 메시지를 에이전트에 전달(후속 질문 가능)
- 개인정보 마스킹: 전화번호·주문번호·이메일·주소·긴 숫자를 Claude 호출 전에 가림
- 규정 검사: 의약외품 효능 표현이 답변에 있으면 경고(차단하지 않음)
- 시스템 프롬프트: CS 답변 초안 형식(답변 초안 / 근거 / 확인 필요)
- 엔티티 정합: 규격 단어(중형·대형 등)·숫자가 다르면 병합하지 않도록 보완

## 실행
1. Colab에서 `notebooks/cs_kb_ingest.ipynb`를 열고 Secrets 등록 후 전체 실행
2. Streamlit Community Cloud에서 이 저장소를 배포하고 Secrets 9개 등록
   `ANTHROPIC_API_KEY`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`, `QDRANT_URL`, `QDRANT_API_KEY`, `CF_ACCOUNT_ID`, `CF_API_TOKEN`
3. 로그인 계정은 2주차와 같은 Neo4j `:User` 노드를 사용
