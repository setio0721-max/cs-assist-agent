"""cs_kb_ingest.ipynb 생성 스크립트 (셀 내용을 여기서 관리)"""
import nbformat as nbf

cells = []
md = lambda s: cells.append(nbf.v4.new_markdown_cell(s))
code = lambda s: cells.append(nbf.v4.new_code_cell(s))

md("""# CS 답변 보조 챗봇 — 지식 적재 노트북
2주차 「마케팅 자료 그래프 RAG」 파이프라인을 CS 자료에 맞게 바꾼 노트북입니다.

| 단계 | 내용 | 도구 |
|---|---|---|
| 1 | 자료 불러오기(상품 정보·정책 md, 문의 이력 xlsx) | GitHub raw / 직접 업로드 |
| 2 | 청킹(문서는 제목 단위, 문의 이력은 1건 = 1청크) + 개인정보 마스킹 | re |
| 3 | 개체·관계 추출(JSON 스키마 고정, 재시도) | Claude tool-use |
| 4 | 엔티티 정합(같은 유형·유사도 85 이상 병합) | rapidfuzz |
| 5 | 그래프 적재(:Entity / :RELATES, MERGE) | Neo4j Query API |
| 6 | 임베딩·벡터 저장(1024차원, COSINE) | bge-m3(Cloudflare Workers AI) + Qdrant Cloud |
| 7 | 적재 확인·검색 테스트 | — |

**준비**: 왼쪽 🔑(Secrets)에 `ANTHROPIC_API_KEY`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`, `QDRANT_URL`, `QDRANT_API_KEY`, `CF_ACCOUNT_ID`, `CF_API_TOKEN`을 등록하고 노트북 접근을 허용하세요. GPU는 필요 없습니다.
""")

code("""!pip -q install anthropic qdrant-client rapidfuzz openpyxl requests""")

md("## 0. 설정")
code("""import json, math, re, time, uuid, io
import requests, pandas as pd
from google.colab import userdata

def secret(k):
    v = userdata.get(k)
    if not v:
        raise RuntimeError(f"Secrets에 {k}가 없습니다.")
    return v.strip()

ANTHROPIC_API_KEY = secret("ANTHROPIC_API_KEY")
NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD = secret("NEO4J_URI"), secret("NEO4J_USER"), secret("NEO4J_PASSWORD")
NEO4J_DATABASE = userdata.get("NEO4J_DATABASE") or "neo4j"
QDRANT_URL, QDRANT_API_KEY = secret("QDRANT_URL"), secret("QDRANT_API_KEY")
CF_ACCOUNT_ID, CF_API_TOKEN = secret("CF_ACCOUNT_ID"), secret("CF_API_TOKEN")

MODEL = "claude-sonnet-5"
GITHUB_RAW = "https://raw.githubusercontent.com/setio0721-max/cs-assist-agent/main/data"
# 문서 ID → (원천 파일, Qdrant 컬렉션, 앱 표시 이름)
DOCS = {
    "products": ("products.md",     "chunks_cs_products", "상품 정보"),
    "policy":   ("policy.md",       "chunks_cs_policy",   "고객·배송 안내"),
    "history":  ("cs_history.xlsx", "chunks_cs_history",  "문의 이력"),
}
print("설정 완료")""")

md("## 1. 자료 불러오기\nGitHub 저장소 `data/` 폴더에서 받습니다. 받을 수 없으면 직접 업로드 창이 열립니다.")
code("""raw = {}
for doc_id, (fname, _, _) in DOCS.items():
    r = requests.get(f"{GITHUB_RAW}/{fname}", timeout=20)
    if r.ok:
        raw[doc_id] = r.content
    else:
        from google.colab import files
        print(f"{fname}을 GitHub에서 받지 못했습니다. 직접 업로드하세요.")
        up = files.upload()
        raw[doc_id] = up[fname]
    print(f"{fname}: {len(raw[doc_id]):,} bytes")""")

md("## 2. 청킹 + 개인정보 마스킹\n- md 문서: `##`·`###` 제목 단위로 자르고, 각 청크 앞에 문서명·제목을 붙임\n- 문의 이력: 1건 = 1청크(`유형/상품/문의/답변` 직렬화)\n- 마스킹 규칙은 앱(`mask_pii`)과 동일")
code(r'''PII_PATTERNS = [
    (re.compile(r"(?<!\d)20\d{14}(?!\d)"), "[주문번호]"),
    (re.compile(r"(?<!\d)01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}(?!\d)"), "[전화번호]"),
    (re.compile(r"(?<!\d)0\d{1,2}[-\s.]\d{3,4}[-\s.]\d{4}(?!\d)"), "[전화번호]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[이메일]"),
    (re.compile(r"[가-힣]+(?:특별시|광역시|도|시)\s?[가-힣]+(?:시|군|구)\s?[가-힣0-9]+(?:로|길)\s?\d+(?:-\d+)?"), "[주소]"),
    (re.compile(r"(?<!\d)\d{10,}(?!\d)"), "[번호]"),
]
def mask_pii(text):
    for p, label in PII_PATTERNS:
        text = p.sub(label, text)
    return text

def chunk_markdown(md_text, doc_title):
    lines = [l for l in md_text.splitlines() if not l.startswith(">")]   # 상단 메타 설명 제외
    chunks, h2, h3, buf = [], "", "", []
    def flush():
        body = "\n".join(buf).strip().strip("-").strip()
        if body:
            section = h2 + (f" > {h3}" if h3 else "")
            chunks.append({"section": section, "text": f"[{doc_title}] {section}\n{body}"})
    for l in lines:
        if l.startswith("# "):
            continue
        if l.startswith("## "):
            flush(); buf.clear(); h2, h3 = l[3:].strip(), ""
        elif l.startswith("### "):
            flush(); buf.clear(); h3 = l[4:].strip()
        else:
            buf.append(l)
    flush()
    return chunks

def chunk_history(xlsx_bytes):
    df = pd.read_excel(io.BytesIO(xlsx_bytes)).fillna("")
    chunks = []
    for _, r in df.iterrows():
        q, a = mask_pii(str(r["문의 내용"])), mask_pii(str(r["답변 내용"]))
        chunks.append({
            "section": f"{r['문의 유형']} · {q[:30]}",
            "inquiry_type": str(r["문의 유형"]),
            "text": f"[문의 이력] 문의 유형: {r['문의 유형']}\n상품: {r['상품']}\n문의: {q}\n모범 답변: {a}",
        })
    return chunks

chunks = {
    "products": chunk_markdown(raw["products"].decode("utf-8"), "상품 정보"),
    "policy":   chunk_markdown(raw["policy"].decode("utf-8"), "고객·배송 안내"),
    "history":  chunk_history(raw["history"]),
}
for k, v in chunks.items():
    print(f"{k}: {len(v)}청크, 예시 → {v[0]['section']}")''')

md("## 3. 개체·관계 추출 (Claude tool-use)\n도구 입력 스키마로 출력 형식을 고정합니다. 개체·관계 유형은 TRD의 그래프 스키마와 같습니다.")
code('''import anthropic
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

ENTITY_TYPES = ["Product", "Material", "Ingredient", "Certification", "Feature",
                "InquiryType", "Policy", "DeliveryMethod", "Channel"]
REL_TYPES = ["CONTAINS", "HAS_CERT", "HAS_FEATURE", "ABOUT", "GOVERNED_BY",
             "DELIVERED_BY", "APPLIES_TO", "CONTACT_VIA", "RELATED_TO"]

EXTRACT_TOOL = {
    "name": "record_graph",
    "description": "텍스트에서 추출한 개체와 관계를 기록합니다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "entities": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string", "description": "짧은 한국어 명사구(예: 온솜결 퓨어 중형, 유기농 순면커버, 반품 7일 이내)"},
                "type": {"type": "string", "enum": ENTITY_TYPES}}, "required": ["name", "type"]}},
            "relations": {"type": "array", "items": {"type": "object", "properties": {
                "source": {"type": "string"}, "target": {"type": "string"},
                "type": {"type": "string", "enum": REL_TYPES}}, "required": ["source", "target", "type"]}},
        },
        "required": ["entities", "relations"],
    },
}
EXTRACT_SYSTEM = (
    "너는 생리대 쇼핑몰 CS 지식그래프 구축 도우미야. 주어진 텍스트에 명시된 사실만 개체와 관계로 추출해. "
    "상품은 규격 단위(롱 라이너·중형·대형·오버나이트)로, 정책은 '반품 7일 이내'처럼 규칙이 드러나는 이름으로 적어. "
    "관계의 source·target은 반드시 entities의 name 중 하나여야 해. 추측하지 마."
)

def extract_graph(text, retries=3):
    for attempt in range(retries):
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=4000, system=EXTRACT_SYSTEM,
                tools=[EXTRACT_TOOL], tool_choice={"type": "tool", "name": "record_graph"},
                messages=[{"role": "user", "content": text}],
            )
            data = next(b.input for b in resp.content if b.type == "tool_use")
            ents = [e for e in data.get("entities", [])
                    if isinstance(e, dict) and e.get("name") and e.get("type") in ENTITY_TYPES]
            names = {e["name"] for e in ents}
            rels = [r for r in data.get("relations", [])
                    if isinstance(r, dict) and r.get("source") in names and r.get("target") in names
                    and r.get("type") in REL_TYPES]
            return ents, rels
        except Exception as ex:
            print(f"  재시도 {attempt + 1}/{retries}: {ex}")
            time.sleep(3 * (attempt + 1))
    return [], []

for doc_id, items in chunks.items():
    for i, c in enumerate(items):
        if "entities" in c:            # 다시 실행해도 이미 추출한 청크는 건너뜀
            continue
        c["entities"], c["relations"] = extract_graph(c["text"])
        if doc_id == "history":        # 문의 유형 열은 그대로 InquiryType 개체로 추가하고 ABOUT 관계 연결
            it = {"name": f"문의유형: {c['inquiry_type']}", "type": "InquiryType"}
            c["relations"] += [{"source": it["name"], "target": e["name"], "type": "ABOUT"}
                               for e in c["entities"] if e["type"] in ("Product", "Material", "Ingredient", "Policy", "Feature")]
            c["entities"].append(it)
        print(f"{doc_id} {i + 1}/{len(items)}: 개체 {len(c['entities'])} · 관계 {len(c['relations'])}")
print("추출 완료. 개체 0개 청크:",
      [(d, c["section"]) for d, it in chunks.items() for c in it if not c["entities"]])''')

md("## 4. 엔티티 정합 (rapidfuzz)\n같은 유형이고 이름 유사도가 85 이상이면 먼저 나온 이름으로 병합합니다. 단, 규격 단어(중형·대형 등)나 숫자가 다르면 병합하지 않습니다(2주차 방식 보완).")
code('''from rapidfuzz import fuzz

def norm(s):
    return re.sub(r"\\s+", "", s.lower())

# 보완(수업 외): 유사도만 보면 '중형'과 '대형'처럼 한 글자 다른 규격이 병합됨 → 규격 단어·숫자가 다르면 병합하지 않음
SIZE_WORDS = ["롱", "라이너", "소형", "중형", "대형", "오버나이트"]
def key_tokens(s):
    return {w for w in SIZE_WORDS if w in s} | set(re.findall(r"\\d+", s))

canon = {}      # (type, 원래 이름) → 대표 이름
reps = {}       # type → 대표 이름 목록
for items in chunks.values():
    for c in items:
        for e in c["entities"]:
            key = (e["type"], e["name"])
            if key in canon:
                continue
            match = next((r for r in reps.get(e["type"], [])
                          if fuzz.ratio(norm(r), norm(e["name"])) >= 85
                          and key_tokens(r) == key_tokens(e["name"])), None)
            canon[key] = match or e["name"]
            if not match:
                reps.setdefault(e["type"], []).append(e["name"])

def cid(etype, name):
    return f"cs:{etype}:{canon[(etype, name)]}"

total = len(canon); kept = sum(len(v) for v in reps.values())
print(f"개체 {total}개 → 대표 개체 {kept}개로 병합")''')

md("## 5. 그래프 적재 (Neo4j Query API, MERGE)\n앱과 같은 HTTPS Query API를 씁니다. `document_id`로 2주차 실습 데이터와 구분합니다.")
code('''host = NEO4J_URI.replace("neo4j+s://", "").replace("neo4j://", "").strip("/")
NEO4J_ENDPOINT = f"https://{host}/db/{NEO4J_DATABASE}/query/v2"
neo = requests.Session(); neo.auth = (NEO4J_USER, NEO4J_PASSWORD)

def cypher(statement, params=None):
    r = neo.post(NEO4J_ENDPOINT, json={"statement": statement, "parameters": params or {}}, timeout=60)
    r.raise_for_status()
    d = r.json()["data"]
    return [dict(zip(d["fields"], v)) for v in d["values"]]

nodes, edges = {}, set()
for doc_id, items in chunks.items():
    for c in items:
        type_of = {e["name"]: e["type"] for e in c["entities"]}
        c["entity_ids"] = sorted({cid(e["type"], e["name"]) for e in c["entities"]})
        for e in c["entities"]:
            i = cid(e["type"], e["name"])
            nodes[i] = {"id": i, "name": canon[(e["type"], e["name"])], "type": e["type"], "doc": f"cs_{doc_id}"}
        for r in c["relations"]:
            edges.add((cid(type_of[r["source"]], r["source"]), cid(type_of[r["target"]], r["target"]), r["type"]))

cypher("CREATE CONSTRAINT entity_cid IF NOT EXISTS FOR (e:Entity) REQUIRE e.canonical_id IS UNIQUE")
cypher("""UNWIND $rows AS row
          MERGE (e:Entity {canonical_id: row.id})
          SET e.name = row.name, e.type = row.type, e.document_id = row.doc""",
       {"rows": list(nodes.values())})
cypher("""UNWIND $rows AS row
          MATCH (a:Entity {canonical_id: row.s}), (b:Entity {canonical_id: row.t})
          MERGE (a)-[r:RELATES {type: row.type}]->(b)""",
       {"rows": [{"s": s, "t": t, "type": ty} for s, t, ty in edges if s != t]})
print(cypher("MATCH (e:Entity) WHERE e.canonical_id STARTS WITH 'cs:' RETURN count(e) AS cs_entities"))
print(cypher("MATCH (a:Entity)-[r:RELATES]->() WHERE a.canonical_id STARTS WITH 'cs:' RETURN count(r) AS cs_relations"))''')

md("## 6. 임베딩·벡터 저장 (bge-m3 + Qdrant Cloud)\n앱의 질문 임베딩과 같은 Cloudflare Workers AI bge-m3(1024차원)를 써서 문서·질문 벡터 공간을 맞춥니다. 컬렉션은 매번 새로 만듭니다.")
code('''from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct

def embed(texts, batch=16):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/@cf/baai/bge-m3"
    out = []
    for i in range(0, len(texts), batch):
        for attempt in range(3):
            r = requests.post(url, headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
                              json={"text": texts[i:i + batch], "truncate_inputs": True}, timeout=60)
            if r.ok:
                break
            time.sleep(2 * (attempt + 1))
        r.raise_for_status()
        for v in r.json()["result"]["data"]:
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
    return out

qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
for doc_id, (_, coll, label) in DOCS.items():
    items = chunks[doc_id]
    vecs = embed([c["text"] for c in items])
    if qdrant.collection_exists(coll):
        qdrant.delete_collection(coll)
    qdrant.create_collection(coll, vectors_config=VectorParams(size=len(vecs[0]), distance=Distance.COSINE))
    qdrant.upsert(coll, points=[
        PointStruct(id=str(uuid.uuid4()), vector=v, payload={
            "text": c["text"], "section": c["section"], "entity_ids": c["entity_ids"],
            "document_id": f"cs_{doc_id}", "inquiry_type": c.get("inquiry_type", "")})
        for c, v in zip(items, vecs)])
    print(f"{coll} ({label}): {qdrant.count(coll).count}개 벡터 저장")''')

md("## 7. 적재 확인·검색 테스트\n앱의 하이브리드 검색과 같은 방식(벡터 top-5 → 그래프 관계 최대 10개)으로 확인합니다.")
code('''def test_search(q, top_k=5):
    qv = embed([q])[0]
    hits = []
    for _, coll, label in DOCS.values():
        hits += [(h.score, label, h) for h in qdrant.query_points(coll, query=qv, limit=top_k).points]
    hits.sort(key=lambda x: x[0], reverse=True)
    print(f"\\n🔎 {q}")
    for score, label, h in hits[:3]:
        facts = cypher("""MATCH (a:Entity)-[r:RELATES]-(b:Entity) WHERE a.canonical_id IN $ids
                          RETURN a.name AS s, r.type AS r, b.name AS t LIMIT 3""", {"ids": h.payload["entity_ids"]})
        print(f"  {score:.3f} [{label}] {h.payload['section']}")
        for f in facts:
            print(f"        └ {f['s']} -{f['r']}-> {f['t']}")

for q in ["100% 유기농인가요?", "개봉 후 교환 가능한가요?", "N배송 주문 취소", "유통기한이 언제까지예요?"]:
    test_search(q)''')

nb = nbf.v4.new_notebook()
nb["cells"] = cells
nb["metadata"] = {"colab": {"provenance": []}, "kernelspec": {"name": "python3", "display_name": "Python 3"},
                  "language_info": {"name": "python"}}
nbf.write(nb, "cs_kb_ingest.ipynb")
print("written", len(cells), "cells")
