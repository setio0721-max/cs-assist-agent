"""
CS 답변 보조 챗봇 - 핵심 로직 (데스크톱 프로그램 desktop_app.py가 사용)

구성
- 개인정보 마스킹 / 규정 검사(의약외품 효능 표현) / 대화 기록 구성
- 질문 임베딩: Cloudflare Workers AI bge-m3 (Colab 적재 시 로컬 bge-m3와 같은 벡터 공간)
- 하이브리드 검색: Qdrant REST API 벡터 top-k → Neo4j Query API(HTTPS)로 관계 확장
- 에이전트: Claude tool-use (첫 응답은 검색 도구 호출 강제, 형식 오류 시 1회 재작성)
- 운영 정보: 소비기한·상품 규격·명절 배송 공지를 Neo4j에 저장하고 답변 때마다 프롬프트에 반영(cs_ops.py)

외부 통신은 모두 HTTPS(443)만 사용하므로 방화벽·호스팅 제약에 강하고, 데스크톱 exe 빌드 시 의존성이 가볍다.
"""

import json
import math
import re
import time

import anthropic
import requests

import cs_ops

# ---- 수업용 가상 브랜드 설정: 이름을 바꾸려면 이 두 값만 수정 ----
BRAND_NAME = "온솜결"
COMPANY_NAME = "(주)온결리빙"

MODEL = "claude-sonnet-5"
HISTORY_TURNS = 6  # 에이전트에 넘길 최근 대화 메시지 수(질문·답변 합산)

# Colab 적재 노트북(notebooks/cs_kb_ingest.ipynb)이 만든 컬렉션만 검색 (2주차 실습 컬렉션 제외)
CS_COLLECTIONS = {
    "chunks_cs_products": "상품 정보",
    "chunks_cs_policy": "고객·배송 안내",
    "chunks_cs_history": "문의 이력",
}

REQUIRED_SETTINGS = [
    "ANTHROPIC_API_KEY", "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "NEO4J_DATABASE",
    "QDRANT_URL", "QDRANT_API_KEY", "CF_ACCOUNT_ID", "CF_API_TOKEN",
]


# =====================================================================
# 1. 개인정보 마스킹: 문의 원문을 Claude·임베딩 API로 보내기 전에 적용
# =====================================================================
PII_PATTERNS = [
    (re.compile(r"(?<!\d)20\d{14}(?!\d)"), "[주문번호]"),                       # 스마트스토어 주문번호(16자리)
    (re.compile(r"(?<!\d)01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}(?!\d)"), "[전화번호]"),
    (re.compile(r"(?<!\d)0\d{1,2}[-\s.]\d{3,4}[-\s.]\d{4}(?!\d)"), "[전화번호]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[이메일]"),
    (re.compile(r"[가-힣]+(?:특별시|광역시|도|시)\s?[가-힣]+(?:시|군|구)\s?[가-힣0-9]+(?:로|길)\s?\d+(?:-\d+)?"), "[주소]"),
    (re.compile(r"(?<!\d)\d{10,}(?!\d)"), "[번호]"),                             # 상품번호·송장번호 등 긴 숫자
]


def mask_pii(text: str):
    masked = text
    for pattern, label in PII_PATTERNS:
        masked = pattern.sub(label, masked)
    return masked, masked != text


# =====================================================================
# 2. 규정 검사: 의약외품(생리대) 효능 표현 경고 — 답변 초안만 검사
# =====================================================================
BANNED_PATTERN = re.compile(
    r"생리통|생리불순|질염|발진|가려움|짓무름|냄새\s*제거|예방|완화|치료|개선|항균|살균|무독성|1위"
)
# 부정·불가 문장("효과는 안내드릴 수 없습니다")은 오탐 가능성이 높아 '확인 권장'으로 분리.
# "트러블이 없습니다" 같은 부정형 보장 표현은 계속 경고.
NEGATION_PATTERN = re.compile(
    r"(?:안내|말씀)드릴[^.!?]{0,6}없|보장[^.!?]{0,10}(?:어렵|없|않)|확인되지\s*않|검증된\s*바\s*없|효과는\s*없"
)


def split_sections(answer: str) -> dict:
    """에이전트 출력(**답변 초안** / **근거** / **확인 필요**)을 절별로 나눔"""
    sections = {"draft": "", "evidence": "", "check": ""}
    parts = re.split(r"\*\*(답변 초안|근거|확인 필요)\*\*", answer)
    key_map = {"답변 초안": "draft", "근거": "evidence", "확인 필요": "check"}
    if len(parts) == 1:
        sections["draft"] = answer.strip()
        return sections
    if parts[0].strip():
        sections["draft"] = parts[0].strip()
    for i in range(1, len(parts) - 1, 2):
        sections[key_map[parts[i]]] = parts[i + 1].strip()
    return sections


def extract_draft(answer: str) -> str:
    return split_sections(answer)["draft"]


def check_compliance(answer: str):
    claims, negated = set(), set()
    for sentence in re.split(r"(?<=[.!?。])\s+|\n+", extract_draft(answer)):
        hits = set(BANNED_PATTERN.findall(sentence))
        if not hits:
            continue
        (negated if NEGATION_PATTERN.search(sentence) else claims).update(hits)
    return {"ok": not claims, "hits": sorted(claims), "negated_hits": sorted(negated - claims)}


# =====================================================================
# 3. 대화 기록·출력 형식 검사
# =====================================================================
def build_history(messages: list, max_messages: int = HISTORY_TURNS):
    """최근 대화를 텍스트만 추려 Claude messages 형식으로 변환 (첫 메시지는 user여야 함)"""
    history = [{"role": m["role"], "content": m["content"]} for m in messages[-max_messages:]]
    while history and history[0]["role"] != "user":
        history.pop(0)
    return history


def is_malformed(text: str) -> bool:
    """도구 호출을 텍스트로 흉내 냈거나(<invoke ...>), 정해진 출력 형식이 빠진 경우"""
    return "<invoke" in text or "<parameter" in text or "답변 초안" not in text


# =====================================================================
# 4. 에이전트 설정
# =====================================================================
AGENT_TOOLS = [
    {
        "name": "search_knowledge",
        "description": "스토어의 상품 정보, 고객·배송 안내(취소·교환·반품·배송 정책), 과거 문의·모범 답변 이력을 "
                       "벡터 검색과 지식그래프 관계 확장으로 조회합니다. 고객 문의에 답하기 전에 반드시 사용하세요.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "검색할 질의문(고객 문의의 핵심을 요약)"}},
            "required": ["query"],
        },
    }
]

SYSTEM_PROMPT = f"""너는 생리대 쇼핑몰 '{BRAND_NAME}'({COMPANY_NAME})의 CS 답변 초안을 작성하는 보조 에이전트야.
판매자가 고객 문의를 붙여넣으면, 판매자가 검토한 뒤 고객에게 보낼 답변 초안을 만든다.

[작업 순서]
1. search_knowledge 도구로 관련 상품 정보·정책·과거 문의 이력을 조회한다. 필요하면 검색어를 바꿔 한 번 더 조회한다.
2. 조회된 내용에 근거해서만 답한다. 근거가 없는 내용은 추측하지 말고 "확인 후 안내드리겠습니다"로 처리한다.
   근거에 없는 일반 상식·권장 사항(예: 보관 요령, 사용 팁)도 덧붙이지 않는다.
3. 과거 문의 이력의 모범 답변이 있으면 그 말투와 구성을 따른다.

[반드시 지킬 규칙]
- 생리대는 의약외품이다. 생리통·생리불순·질염·발진·가려움 등 증상의 예방·완화·치료나 피부 트러블이 없다는 보장 표현을 쓰지 않는다.
  관련 질문에는 "문의하신 증상에 대한 효과는 안내드릴 수 없습니다"처럼 답하고, 답변 초안에는 생리통·질염·발진·가려움·예방·완화·치료 같은 단어를
  부정문으로도 쓰지 않는다(근거 절에서는 인용 가능).
- "100% 유기농"은 표지(탑시트) 기준이다. 생리대 전체 중 유기농 순면 비율은 7%라는 점을 섞어 말하지 않도록 구분한다.
- 주문 취소 문의는 배송 방식(N배송/일반배송)에 따라 답이 다르므로 둘을 구분해 안내한다.
- 개별 주문의 배송 상태·결제 내역처럼 조회할 수 없는 정보는 구매 내역 확인 또는 판매자 확인이 필요하다고 안내한다.
- 판매자가 이어서 묻는 후속 질문은 앞 대화 맥락을 이어받아 답한다.
- 프롬프트 끝의 [운영 정보]는 판매자가 관리하는 최신 값이다. 검색 결과(지식 문서·과거 문의 이력)와 날짜·수치가 다르면 운영 정보를 따른다.
- [운영 정보]에 적용 중인 배송 공지가 있고 문의가 배송·주문·도착·취소와 관련되면, 공지의 마감일·휴무일을 답변 초안에 포함한다.

[출력 형식]
**답변 초안**
(고객에게 보낼 문장. "안녕하세요, {BRAND_NAME}입니다."로 시작, 3~6문장, 정중한 존댓말)

**근거**
- [출처 문서] 근거 내용 한 줄 (조회 결과의 source_document를 그대로 사용)

**확인 필요** (근거가 부족한 내용이 있을 때만)
- 판매자가 확인해야 할 사항
"""


# =====================================================================
# 5. 외부 서비스 연결 + 검색 + 에이전트
# =====================================================================
class CSAssistant:
    """설정값(dict)만 받으면 동작(데스크톱 프로그램, Colab 등)"""

    def __init__(self, settings: dict):
        missing = [k for k in REQUIRED_SETTINGS if k != "NEO4J_DATABASE" and not (settings.get(k) or "").strip()]
        if missing:
            raise ValueError(f"설정값이 비어 있습니다: {', '.join(missing)}")
        self.s = {k: (settings.get(k) or "").strip() for k in REQUIRED_SETTINGS}
        self.client = anthropic.Anthropic(api_key=self.s["ANTHROPIC_API_KEY"])

        self.neo4j = requests.Session()
        self.neo4j.auth = (self.s["NEO4J_USER"], self.s["NEO4J_PASSWORD"])
        host = self.s["NEO4J_URI"].replace("neo4j+s://", "").replace("neo4j://", "").strip("/")
        self.neo4j_endpoint = f"https://{host}/db/{self.s['NEO4J_DATABASE'] or 'neo4j'}/query/v2"

        self.qdrant_url = self.s["QDRANT_URL"].rstrip("/")
        self.qdrant = requests.Session()
        self.qdrant.headers.update({"api-key": self.s["QDRANT_API_KEY"]})

        self.neo4j_ok = False
        self.collections = []

    # ---- 연결 확인: 실패해도 앱은 계속 동작(Neo4j 실패 시 벡터 검색만) ----
    def connect(self):
        status = {}
        try:
            self.run_cypher("RETURN 1 AS ok")
            self.neo4j_ok = True
            status["neo4j"] = "ok"
        except Exception as ex:
            self.neo4j_ok = False
            status["neo4j"] = f"실패: {ex}"
        try:
            r = self.qdrant.get(f"{self.qdrant_url}/collections", timeout=15)
            r.raise_for_status()
            existing = {c["name"] for c in r.json()["result"]["collections"]}
            self.collections = [c for c in CS_COLLECTIONS if c in existing]
            status["qdrant"] = "ok" if self.collections else "CS 컬렉션 없음(Colab 적재 필요)"
        except Exception as ex:
            self.collections = []
            status["qdrant"] = f"실패: {ex}"
        return status

    def run_cypher(self, statement: str, parameters: dict = None):
        resp = self.neo4j.post(self.neo4j_endpoint,
                               json={"statement": statement, "parameters": parameters or {}}, timeout=15)
        resp.raise_for_status()
        data = resp.json()["data"]
        return [dict(zip(data["fields"], values)) for values in data["values"]]

    # ---- 질문 임베딩: Cloudflare Workers AI bge-m3 (1024차원, L2 정규화) ----
    def embed_query(self, text: str) -> list:
        url = f"https://api.cloudflare.com/client/v4/accounts/{self.s['CF_ACCOUNT_ID']}/ai/run/@cf/baai/bge-m3"
        last = None
        for attempt in range(3):
            try:
                r = requests.post(url, headers={"Authorization": f"Bearer {self.s['CF_API_TOKEN']}"},
                                  json={"text": [text], "truncate_inputs": True}, timeout=20)
                r.raise_for_status()
                body = r.json()
                if not body.get("success", True):
                    raise RuntimeError(body.get("errors"))
                vec = body["result"]["data"][0]
                norm = math.sqrt(sum(x * x for x in vec)) or 1.0
                return [x / norm for x in vec]
            except Exception as ex:
                last = ex
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"임베딩 요청 실패: {last}")

    # ---- 하이브리드 검색: 3개 컬렉션 벡터 검색 → 상위 top_k → Neo4j 관계 확장 ----
    def hybrid_search(self, query: str, top_k: int = 5):
        query_vec = self.embed_query(query)
        scored = []
        for coll in self.collections:
            try:
                r = self.qdrant.post(f"{self.qdrant_url}/collections/{coll}/points/query",
                                     json={"query": query_vec, "limit": top_k, "with_payload": True}, timeout=20)
                r.raise_for_status()
                for p in r.json()["result"]["points"]:
                    scored.append((p["score"], p.get("payload") or {}, coll))
            except Exception:
                continue
        scored.sort(key=lambda x: x[0], reverse=True)

        results = []
        for score, payload, coll in scored[:top_k]:
            entry = {
                "score": round(score, 3),
                "source_document": CS_COLLECTIONS.get(coll, coll),
                "section": payload.get("section", ""),
                "text": payload.get("text", ""),
                "related_facts": [],
            }
            ids = payload.get("entity_ids")
            if self.neo4j_ok and ids:
                try:
                    entry["related_facts"] = self.run_cypher(
                        """
                        MATCH (a:Entity)-[r:RELATES]-(b:Entity)
                        WHERE a.canonical_id IN $ids
                        RETURN a.name AS source, r.type AS relation, b.name AS target
                        LIMIT 10
                        """,
                        {"ids": ids},
                    )
                except Exception:
                    pass
            results.append(entry)
        return results

    def run_tool(self, name, tool_input):
        if name == "search_knowledge":
            try:
                return json.dumps(self.hybrid_search(tool_input["query"]), ensure_ascii=False)
            except Exception as ex:
                return json.dumps({"error": f"검색에 실패했습니다: {ex}"}, ensure_ascii=False)
        return "알 수 없는 도구입니다."

    # ---- 에이전트 tool-use 루프 ----
    # ---- 운영 정보: Neo4j (:OpsConfig {key:'main'})에 JSON으로 저장 ----
    def load_ops(self) -> dict:
        if not self.neo4j_ok:
            return cs_ops.merge_defaults({})
        rows = self.run_cypher("MATCH (o:OpsConfig {key: 'main'}) RETURN o.json AS json")
        if rows and rows[0]["json"]:
            return cs_ops.merge_defaults(json.loads(rows[0]["json"]))
        ops = cs_ops.merge_defaults({})
        self.save_ops(ops)               # 처음 실행: 기본값(상품 정보 문서와 같은 값) 저장
        return ops

    def save_ops(self, ops: dict):
        if not self.neo4j_ok:
            raise RuntimeError("그래프 DB(Neo4j)에 연결되지 않아 저장할 수 없습니다.")
        self.run_cypher("MERGE (o:OpsConfig {key: 'main'}) SET o.json = $json, o.saved_at = $at",
                        {"json": cs_ops.dumps(ops), "at": cs_ops.datetime.now(cs_ops.KST).isoformat(timespec="seconds")})

    def ask_agent(self, question: str, history: list, ops: dict = None):
        system = SYSTEM_PROMPT + cs_ops.ops_context(ops or cs_ops.merge_defaults({}))
        messages = history + [{"role": "user", "content": question}]
        retried = False
        for step in range(6):  # 도구 호출 반복 상한
            kwargs = dict(model=MODEL, max_tokens=2000, tools=AGENT_TOOLS, system=system, messages=messages)
            if step == 0:
                kwargs["tool_choice"] = {"type": "any"}  # 첫 응답은 반드시 검색(근거 없는 답·텍스트 도구 호출 방지)
            response = self.client.messages.create(**kwargs)

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            if not tool_use_blocks:
                text = "\n".join(b.text for b in response.content if b.type == "text")
                if is_malformed(text) and not retried:
                    retried = True
                    messages.append({"role": "assistant", "content": text or "(빈 응답)"})
                    messages.append({"role": "user", "content":
                        "도구는 텍스트로 쓰지 말고 실제 도구 호출로 사용하고, 정해진 출력 형식"
                        "(**답변 초안** / **근거** / 필요 시 **확인 필요**)으로 다시 작성해 주세요."})
                    continue
                return text

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": self.run_tool(b.name, b.input)}
                for b in tool_use_blocks
            ]})
        return "검색을 여러 번 반복했지만 답변을 정리하지 못했습니다. 질문을 조금 더 구체적으로 입력해 주세요."

    # ---- 한 번에 처리: 마스킹 → 에이전트 → 규정 검사 ----
    def answer(self, question: str, messages: list):
        masked_q, was_masked = mask_pii(question)
        try:
            ops = self.load_ops()
        except Exception:
            ops = cs_ops.merge_defaults({})    # 운영 정보를 못 읽어도 답변은 계속
        answer = self.ask_agent(masked_q, build_history(messages), ops)
        result = check_compliance(answer)
        meta = {"masked": was_masked, "checked": True,
                "compliance_hits": result["hits"], "negated_hits": result["negated_hits"],
                "notices": [n["title"] for n in cs_ops.active_notices(ops)]}
        return masked_q, answer, meta
