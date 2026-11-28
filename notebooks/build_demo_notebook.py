"""cs_demo.ipynb 생성 스크립트 — 발표 시연용(읽기 전용, DB를 바꾸지 않음)"""
import nbformat as nbf

cells = []
md = lambda s: cells.append(nbf.v4.new_markdown_cell(s))
code = lambda s: cells.append(nbf.v4.new_code_cell(s))

md("""# CS 답변 보조 에이전트 — 시연 노트북
앱(웹·데스크톱)이 쓰는 공통 모듈 `cs_core.py`를 그대로 불러와 **내부 동작을 단계별로 보여 주는** 노트북입니다.
이미 적재된 지식그래프(Neo4j)와 벡터DB(Qdrant)를 **조회만** 하므로 여러 번 실행해도 데이터가 바뀌지 않습니다.
지식 적재는 `cs_kb_ingest.ipynb`에서 합니다.

| 셀 | 보여 주는 것 | 발표 자료 연결 |
|---|---|---|
| 1 | 연결·지식 현황 | 아키텍처 |
| 2 | 지식그래프 조회(Cypher) | 처리 흐름 |
| 3 | 하이브리드 검색(벡터 top-5 → 그래프 확장) | 핵심 코드 ② |
| 4 | 에이전트 과정 추적(도구 호출 → 검색 → 답변) | 핵심 코드 ① |
| 5 | 안전장치: 개인정보 마스킹·규정 검사 | 핵심 코드 ③ |
| 6 | 엔티티 정합 보완(중형 ≠ 대형) | 핵심 코드 ④ |

**준비**: 왼쪽 🔑(Secrets)에 `ANTHROPIC_API_KEY`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`, `QDRANT_URL`, `QDRANT_API_KEY`, `CF_ACCOUNT_ID`, `CF_API_TOKEN`을 등록하고 노트북 접근을 허용하세요.
Cloudflare 키 2개가 없으면 bge-m3 모델(약 2GB)을 Colab에서 내려받아 대신 씁니다(첫 실행 2~4분).
**발표 전**: Neo4j Aura·Qdrant Cloud가 일시정지(Paused) 상태가 아닌지 확인하세요.
""")

md("## 1. 연결·지식 현황\nGitHub에서 `cs_core.py`를 받아 앱과 같은 `CSAssistant`로 연결합니다.")
code('''!pip -q install anthropic rapidfuzz
import json, os, sys, requests
from google.colab import userdata

CORE_URL = "https://raw.githubusercontent.com/setio0721-max/cs-assist-agent/main/cs_core.py"
open("cs_core.py", "w", encoding="utf-8").write(requests.get(CORE_URL, timeout=20).text)
sys.path.insert(0, ".")
import cs_core
from cs_core import CSAssistant, CS_COLLECTIONS, REQUIRED_SETTINGS

def get_secret(k):
    try:
        return (userdata.get(k) or "").strip()
    except Exception:
        return ""

settings = {k: get_secret(k) for k in REQUIRED_SETTINGS}
use_cloudflare = bool(settings["CF_ACCOUNT_ID"] and settings["CF_API_TOKEN"])
if not use_cloudflare:                      # 키가 없으면 Colab 로컬 bge-m3(같은 벡터 공간)로 대체
    settings["CF_ACCOUNT_ID"] = settings["CF_API_TOKEN"] = "local"

core = CSAssistant(settings)
if not use_cloudflare:
    from sentence_transformers import SentenceTransformer
    _model = SentenceTransformer("BAAI/bge-m3")
    core.embed_query = lambda text: _model.encode([text], normalize_embeddings=True)[0].tolist()

status = core.connect()
print("임베딩  :", "Cloudflare Workers AI bge-m3" if use_cloudflare else "Colab 로컬 bge-m3")
print("Neo4j   :", status["neo4j"])
print("Qdrant  :", status["qdrant"])
print("모델    :", cs_core.MODEL)
print("\\n[지식 현황]")
total = 0
for coll in core.collections:
    n = core.qdrant.get(f"{core.qdrant_url}/collections/{coll}", timeout=15).json()["result"]["points_count"]
    total += n
    print(f"  {CS_COLLECTIONS[coll]:<10} {coll:<20} {n:>3}개 청크")
print(f"  {'합계':<10} {'':<20} {total:>3}개 청크")''')

md("## 2. 지식그래프 조회 (Neo4j, Cypher)\n자료에서 추출한 개체(`:Entity`)와 관계(`:RELATES`)입니다. `cs:` 접두어로 2주차 실습 데이터와 구분합니다.")
code('''rows = core.run_cypher("""
    MATCH (e:Entity) WHERE e.canonical_id STARTS WITH 'cs:'
    RETURN e.type AS type, count(*) AS n ORDER BY n DESC""")
rel = core.run_cypher("""
    MATCH (a:Entity)-[r:RELATES]->() WHERE a.canonical_id STARTS WITH 'cs:'
    RETURN count(r) AS n""")[0]["n"]
print(f"[개체 유형별 개수]  총 {sum(r['n'] for r in rows)}개 개체, {rel}개 관계")
for r in rows:
    print(f"  {r['type']:<12} {r['n']:>3}")

TARGET = "중형"
facts = core.run_cypher("""
    MATCH (a:Entity)-[r:RELATES]->(b:Entity)
    WHERE a.canonical_id STARTS WITH 'cs:' AND a.name CONTAINS $t
    RETURN a.name AS s, r.type AS r, b.name AS t ORDER BY s, r LIMIT 15""", {"t": TARGET})
print(f"\\n['{TARGET}' 개체에서 나가는 관계]")
for f in facts:
    print(f"  {f['s']} -[{f['r']}]-> {f['t']}")''')

md("## 3. 하이브리드 검색 (핵심 코드 ②)\n질문을 bge-m3로 임베딩 → 컬렉션 3개에서 벡터 검색 → 점수 상위 5개 → 각 청크의 `entity_ids`로 Neo4j 관계를 최대 10개 확장합니다. 에이전트가 쓰는 `core.hybrid_search`를 그대로 호출합니다.")
code('''QUESTION = "N배송으로 주문했는데 취소할 수 있나요?"   # ← 시연 질문을 바꿔 보세요

results = core.hybrid_search(QUESTION)
print(f"🔎 {QUESTION}\\n")
for i, r in enumerate(results, 1):
    print(f"{i}. 유사도 {r['score']:.3f}  [{r['source_document']}] {r['section']}")
    print(f"   {r['text'][:80].replace(chr(10), ' ')}…")
    for f in r["related_facts"][:4]:
        print(f"      └ 그래프: {f['source']} -[{f['relation']}]-> {f['target']}")
    if len(r["related_facts"]) > 4:
        print(f"      └ … 관계 {len(r['related_facts'])}개 중 4개 표시")''')

md("## 4. 에이전트 과정 추적 (핵심 코드 ①)\n`cs_core`의 tool-use 루프(`ask_agent`)와 같은 순서를 단계마다 출력합니다.\n- 첫 응답은 `tool_choice={\"type\": \"any\"}`로 **반드시 검색 도구를 호출**\n- 도구 호출을 글자로 흉내 내거나 형식이 빠지면(`is_malformed`) **한 번 다시 작성** 요청")
code('''from cs_core import AGENT_TOOLS, SYSTEM_PROMPT, MODEL, is_malformed, mask_pii

def trace_agent(question, max_steps=6):
    q, masked = mask_pii(question)
    print(f"👤 질문: {q}" + ("   (개인정보 마스킹 적용)" if masked else ""))
    messages, retried = [{"role": "user", "content": q}], False
    for step in range(max_steps):
        kwargs = dict(model=MODEL, max_tokens=2000, tools=AGENT_TOOLS, system=SYSTEM_PROMPT, messages=messages)
        if step == 0:
            kwargs["tool_choice"] = {"type": "any"}
        resp = core.client.messages.create(**kwargs)
        calls = [b for b in resp.content if b.type == "tool_use"]
        print(f"\\n── 단계 {step + 1}  (stop_reason: {resp.stop_reason}"
              + (", tool_choice=any" if step == 0 else "") + ")")
        if not calls:
            text = "\\n".join(b.text for b in resp.content if b.type == "text")
            if is_malformed(text) and not retried:
                retried = True
                print("⚠️ 형식 오류 감지 → 다시 작성 요청")
                messages += [{"role": "assistant", "content": text or "(빈 응답)"},
                             {"role": "user", "content": "정해진 출력 형식(**답변 초안** / **근거** / 필요 시 **확인 필요**)으로 다시 작성해 주세요."}]
                continue
            print("🧾 최종 답변\\n")
            print(text)
            return text
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in calls:
            print(f"🔧 도구 호출: {b.name}({json.dumps(b.input, ensure_ascii=False)})")
            out = core.run_tool(b.name, b.input)
            for h in json.loads(out)[:3] if out.startswith("[") else []:
                print(f"   ↳ {h['score']:.3f} [{h['source_document']}] {h['section']}  (관계 {len(h['related_facts'])}개)")
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        messages.append({"role": "user", "content": results})

answer = trace_agent("주문번호 2026112812345678 인데요, N배송 주문 취소 가능한가요?")''')

md("## 5. 안전장치 (핵심 코드 ③)\n### 개인정보 마스킹\n문의 원문은 Claude·임베딩 API로 보내기 **전에** 가립니다.")
code('''from cs_core import mask_pii, check_compliance

sample = "주문번호 2026112812345678, 연락처 010-1234-5678, 메일 test@example.com, 서울특별시 강남구 테헤란로 123 으로 보내 주세요."
masked, changed = mask_pii(sample)
print("원문  :", sample)
print("마스킹:", masked)''')
md("### 규정 검사\n의약외품(생리대) 효능 표현을 **답변 초안에서만** 찾고, 부정·불가 문장은 '확인 권장'으로 분리합니다.")
code('''cases = {
    "① 효능 주장": "**답변 초안**\\n안녕하세요, 온솜결입니다. 이 제품은 생리통 완화에 도움이 됩니다.",
    "② 부정문":   "**답변 초안**\\n안녕하세요, 온솜결입니다. 생리통 완화 효과는 안내드릴 수 없습니다.",
    "③ 정상":     "**답변 초안**\\n안녕하세요, 온솜결입니다. 표지(탑시트)는 100% 유기농 순면입니다.\\n\\n**근거**\\n- [상품 정보] 질염 등 효능 표현 금지",
}
for name, text in cases.items():
    r = check_compliance(text)
    verdict = "⚠️ 경고" if r["hits"] else ("🔎 확인 권장" if r["negated_hits"] else "✅ 통과")
    print(f"{name} → {verdict}   경고 단어 {r['hits']} / 부정문 속 단어 {r['negated_hits']}")
print("\\n※ ③은 근거 절에 '질염'이 있지만 답변 초안만 검사하므로 통과")
if "answer" in globals():
    r = check_compliance(answer)
    print("\\n[4번 셀 실제 답변 검사]", "⚠️ 경고 " + str(r["hits"]) if r["hits"] else "✅ 효능 표현 없음")''')

md("## 6. 엔티티 정합 보완 (핵심 코드 ④)\n강의 방식(이름 유사도 85 이상 병합)만 쓰면 한 글자 다른 규격이 하나로 합쳐집니다. 규격 단어·숫자가 같을 때만 병합하도록 보완했습니다(`cs_kb_ingest.ipynb` 4단계와 같은 코드).")
code('''import re
from rapidfuzz import fuzz

norm = lambda s: re.sub(r"\\s+", "", s.lower())
SIZE_WORDS = ["롱", "라이너", "소형", "중형", "대형", "오버나이트"]
key_tokens = lambda s: {w for w in SIZE_WORDS if w in s} | set(re.findall(r"\\d+", s))

pairs = [("온솜결 퓨어 중형", "온솜결 퓨어 대형"),
         ("온솜결 퓨어 중형", "온솜결퓨어 중형")]   # 띄어쓰기만 다른 같은 제품
for a, b in pairs:
    sim = fuzz.ratio(norm(a), norm(b))
    lecture = sim >= 85
    improved = lecture and key_tokens(a) == key_tokens(b)
    print(f"'{a}' vs '{b}'  유사도 {sim:.1f} → 강의 방식: {'병합' if lecture else '분리'} / 보완 후: {'병합' if improved else '분리'}")''')

nb = nbf.v4.new_notebook()
nb["cells"] = cells
nb["metadata"] = {"colab": {"provenance": []}, "kernelspec": {"name": "python3", "display_name": "Python 3"},
                  "language_info": {"name": "python"}}
nbf.write(nb, "cs_demo.ipynb")
print("written", len(cells), "cells")
