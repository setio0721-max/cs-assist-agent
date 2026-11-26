"""
쇼핑몰 CS 답변 보조 챗봇 - Streamlit 웹앱 (2주차 marketing-kg-agent 기반)

로컬 실행: streamlit run streamlit_app.py
배포: Streamlit Community Cloud(GitHub 저장소) 또는 Hugging Face Spaces에 이 파일 + requirements.txt 업로드

필요한 접속 정보(secrets)는 .streamlit/secrets.toml 또는 배포 플랫폼의 Secrets 설정에 등록합니다.
로그인 계정은 secrets가 아니라 Neo4j의 :User 노드(username, password_hash, status, is_admin)로 관리합니다.
[secrets.toml 예시]
ANTHROPIC_API_KEY = "sk-ant-..."
NEO4J_URI = "neo4j+s://xxxx.databases.neo4j.io"
NEO4J_USER = "xxxx"
NEO4J_PASSWORD = "..."
NEO4J_DATABASE = "xxxx"   # Aura credentials 파일의 NEO4J_DATABASE 값 (없으면 "neo4j"로 시도)
QDRANT_URL = "https://xxxx.cloud.qdrant.io"
QDRANT_API_KEY = "..."
CF_ACCOUNT_ID = "..."     # Cloudflare 계정 ID (질문 임베딩용 Workers AI)
CF_API_TOKEN = "..."      # Workers AI 권한의 API 토큰

회원가입 흐름:
- 사용자가 "회원가입 신청" 탭에서 아이디/비밀번호를 제출하면 status='pending'인 :User 노드가 생성됩니다.
- status가 'pending'인 계정은 로그인할 수 없습니다.
- is_admin=true인 계정으로 로그인하면 관리자 패널이 나타나 승인/거절할 수 있습니다.
- 최초 관리자 계정은 Colab의 manage_users.py로 미리 만들어둬야 합니다 (닭과 달걀 문제).

참고: Neo4j는 Bolt 프로토콜(포트 7687)이 아니라 Query API(HTTPS, 포트 443)로 접속합니다.
Hugging Face Spaces 등 일부 호스팅 환경은 80/443/8080 외 포트로 나가는 요청을 막기 때문에,
Bolt 드라이버(neo4j 패키지)를 쓰면 연결 시도가 응답 없이 멈춰버릴 수 있습니다.

다중 문서 지원: 노트북에서 문서마다 DOCUMENT_ID를 바꿔가며 처리하면 Qdrant에
"chunks_<DOCUMENT_ID>" 컬렉션이 각각 생성됩니다. 사이드바에서 골라 질문할 수 있습니다.
"""

import json
import math
import os
import re
import time

import streamlit as st
import anthropic
import bcrypt
import requests
from qdrant_client import QdrantClient

COLLECTION_PREFIX = "chunks_"

# ---- 수업용 가상 브랜드 설정: 이름을 바꾸려면 이 두 값만 수정 ----
BRAND_NAME = "온솜결"
COMPANY_NAME = "(주)온결리빙"

st.set_page_config(page_title="CS 답변 보조 챗봇", page_icon="💬")

# 크롬 등 브라우저의 자동 번역이 한글 텍스트를 다시 오역하는 것을 막고,
# 홈 화면 아이콘 이름도 "마케팅 지식그래프 기반 AI 에이전트"로 표시되도록 페이지 메타 정보를 보정합니다.
# 참고: st.components.v1.html은 deprecated(2026-06-01 이후 제거 예정)이므로 st.iframe으로 대체합니다.
st.iframe(
    """
    <script>
    try {
        var p = window.parent.document;
        p.documentElement.lang = "ko";
        p.documentElement.setAttribute("translate", "no");

        var notranslate = p.createElement("meta");
        notranslate.name = "google";
        notranslate.content = "notranslate";
        p.head.appendChild(notranslate);

        p.title = "CS 답변 보조 챗봇";

        var link = p.querySelector('link[rel="manifest"]');
        var manifest = {
            name: "CS 답변 보조 챗봇",
            short_name: "CS챗봇",
            start_url: ".",
            display: "standalone"
        };
        var blob = new Blob([JSON.stringify(manifest)], {type: "application/json"});
        var manifestURL = URL.createObjectURL(blob);
        if (link) {
            link.setAttribute("href", manifestURL);
        } else {
            link = p.createElement("link");
            link.rel = "manifest";
            link.href = manifestURL;
            p.head.appendChild(link);
        }
    } catch (e) {}
    </script>
    """,
    height=1,  # st.iframe은 height=0을 허용하지 않음 (양의 정수 또는 'stretch'/'content' 필요) - 최소값으로 사실상 안 보이게 처리
)


def get_secret(key: str):
    """Streamlit Cloud(st.secrets)와 Hugging Face Spaces(환경변수) 양쪽에서 동작하도록 함"""
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.environ.get(key)


# ---- Neo4j Query API - 로그인/회원가입에서도 필요하므로 무거운 리소스보다 먼저 정의 ----
@st.cache_resource
def get_neo4j_session():
    session = requests.Session()
    session.auth = (get_secret("NEO4J_USER"), get_secret("NEO4J_PASSWORD"))
    session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
    return session


def get_neo4j_endpoint():
    uri = get_secret("NEO4J_URI") or ""
    hostname = uri.replace("neo4j+s://", "").replace("neo4j://", "").strip("/")
    database = get_secret("NEO4J_DATABASE") or "neo4j"
    return f"https://{hostname}/db/{database}/query/v2"


def run_cypher(statement: str, parameters: dict = None):
    session = get_neo4j_session()
    resp = session.post(
        get_neo4j_endpoint(),
        json={"statement": statement, "parameters": parameters or {}},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    fields = data["fields"]
    return [dict(zip(fields, values)) for values in data["values"]]


# ---- 로그인 / 회원가입 ----
def find_user(username: str):
    rows = run_cypher(
        "MATCH (u:User {username: $username}) "
        "RETURN u.password_hash AS password_hash, u.status AS status, u.is_admin AS is_admin",
        {"username": username},
    )
    return rows[0] if rows else None


def create_signup_request(username: str, password: str):
    if not username or not password:
        return False, "아이디와 비밀번호를 모두 입력해주세요."
    if find_user(username):
        return False, "이미 등록되었거나 신청 중인 아이디입니다."

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    run_cypher(
        """
        CREATE (u:User {
            username: $username,
            password_hash: $hash,
            status: 'pending',
            is_admin: false,
            requested_at: datetime()
        })
        """,
        {"username": username, "hash": password_hash},
    )
    return True, "회원가입 신청이 접수되었습니다. 관리자 승인 후 로그인할 수 있습니다."


def render_login_styles():
    st.markdown(
        """
        <style>
        .stApp {
            background: linear-gradient(180deg, #eef2fb 0%, #f8fafc 55%, #ffffff 100%);
        }
        .login-hero {
            text-align: center;
            padding-top: 0.5rem;
            margin-bottom: 0.5rem;
        }
        .login-hero .icon {
            display: flex;
            justify-content: center;
            margin-bottom: 0.4rem;
        }
        .login-hero h1 {
            margin: 0.3rem 0 0 0;
            font-size: 1.5rem;
            font-weight: 800;
            color: #0f172a;
            line-height: 1.3;
        }
        .login-hero .en {
            color: #64748b;
            margin-top: 0.1rem;
            font-size: 1rem;
        }
        [data-testid="stVerticalBlockBorderWrapper"] {
            border-radius: 22px !important;
            padding: 0.4rem;
        }
        div[data-testid="stTextInput"] input {
            border-radius: 999px;
            padding: 0.65rem 1.1rem;
        }
        div[data-testid="stButton"] > button[kind="primary"] {
            background-color: #0f1b3d;
            border-color: #0f1b3d;
            border-radius: 999px;
            padding: 0.65rem 0;
            font-weight: 700;
            width: 100%;
        }
        div[data-testid="stButton"] > button[kind="primary"]:hover {
            background-color: #16264f;
            border-color: #16264f;
        }
        div[data-testid="stButton"] > button[kind="secondary"] {
            border: none;
            background: transparent;
            color: #475569;
            width: 100%;
        }
        div[data-testid="stButton"] > button[kind="secondary"]:hover {
            color: #0f1b3d;
            text-decoration: underline;
        }
        .block-container {
            max-width: 480px;
            margin: auto;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_login_header():
    st.markdown(
        """
        <div class="login-hero">
            <div class="icon">
                <svg width="76" height="76" viewBox="0 0 76 76" xmlns="http://www.w3.org/2000/svg">
                    <defs>
                        <linearGradient id="bgGrad" x1="0" y1="0" x2="1" y2="1">
                            <stop offset="0%" stop-color="#1e3a8a"/>
                            <stop offset="100%" stop-color="#0891b2"/>
                        </linearGradient>
                        <linearGradient id="badgeGrad" x1="0" y1="0" x2="1" y2="1">
                            <stop offset="0%" stop-color="#f97316"/>
                            <stop offset="100%" stop-color="#fb923c"/>
                        </linearGradient>
                        <filter id="softShadow" x="-30%" y="-30%" width="160%" height="160%">
                            <feDropShadow dx="0" dy="3" stdDeviation="4" flood-color="#0f172a" flood-opacity="0.18"/>
                        </filter>
                    </defs>
                    <rect x="3" y="3" width="70" height="70" rx="20" fill="url(#bgGrad)" filter="url(#softShadow)"/>
                    <rect x="21" y="16" width="27" height="36" rx="4" fill="white" opacity="0.97"/>
                    <rect x="26" y="24" width="17" height="2.4" rx="1.2" fill="#1e3a8a"/>
                    <rect x="26" y="30.5" width="17" height="2.4" rx="1.2" fill="#1e3a8a"/>
                    <rect x="26" y="37" width="10" height="2.4" rx="1.2" fill="#1e3a8a"/>
                    <circle cx="52" cy="50" r="14" fill="url(#badgeGrad)" stroke="white" stroke-width="2.5"/>
                    <text x="52" y="55" text-anchor="middle" font-size="12" font-weight="700" fill="white" font-family="Arial, sans-serif">AI</text>
                </svg>
            </div>
            <h1>CS 답변 보조 챗봇</h1>
            <div class="en">Graph RAG CS Assistant</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def check_login() -> bool:
    if st.session_state.get("authenticated"):
        return True

    render_login_styles()
    render_login_header()

    view = st.session_state.get("_auth_view", "login")

    with st.container(border=True):
        if view == "login":
            st.markdown("**아이디**")
            username = st.text_input(
                "아이디", placeholder="👤 아이디를 입력하세요.",
                label_visibility="collapsed", key="login_username",
            )
            st.markdown("**비밀번호**")
            password = st.text_input(
                "비밀번호", placeholder="🔒 비밀번호를 입력하세요.", type="password",
                label_visibility="collapsed", key="login_password",
            )

            _l, _c, _r = st.columns([1, 1.6, 1])
            with _c:
                login_clicked = st.button("로그인", type="primary", key="login_submit", use_container_width=True)

            col1, col2 = st.columns(2)
            with col1:
                forgot_clicked = st.button("비밀번호를 잊으셨나요?", type="secondary", key="forgot_pw")
            with col2:
                signup_link_clicked = st.button("회원가입", type="secondary", key="go_signup")

            if forgot_clicked:
                st.info("비밀번호 재설정은 관리자에게 문의해주세요.")

            if signup_link_clicked:
                st.session_state["_auth_view"] = "signup"
                st.rerun()

            if login_clicked:
                try:
                    user = find_user(username)
                except Exception as ex:
                    user = None
                    st.error(f"로그인 확인 중 오류가 발생했습니다: {ex}")
                else:
                    if not user or not user.get("password_hash"):
                        st.error("아이디 또는 비밀번호가 올바르지 않습니다.")
                    elif not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
                        st.error("아이디 또는 비밀번호가 올바르지 않습니다.")
                    elif user.get("status") != "approved":
                        st.warning("아직 관리자 승인 대기 중인 계정입니다. 승인 후 다시 시도해주세요.")
                    else:
                        st.session_state["authenticated"] = True
                        st.session_state["username"] = username
                        st.session_state["is_admin"] = bool(user.get("is_admin"))
                        st.rerun()

        else:  # 회원가입 화면
            st.markdown("**사용할 아이디**")
            new_username = st.text_input(
                "사용할 아이디", placeholder="👤 아이디를 입력하세요.",
                label_visibility="collapsed", key="signup_username",
            )
            st.markdown("**사용할 비밀번호**")
            new_password = st.text_input(
                "사용할 비밀번호", placeholder="🔒 비밀번호를 입력하세요.", type="password",
                label_visibility="collapsed", key="signup_password",
            )
            st.markdown("**비밀번호 확인**")
            new_password_confirm = st.text_input(
                "비밀번호 확인", placeholder="🔒 비밀번호를 다시 입력하세요.", type="password",
                label_visibility="collapsed", key="signup_password_confirm",
            )

            _l2, _c2, _r2 = st.columns([1, 1.6, 1])
            with _c2:
                signup_clicked = st.button("가입 신청", type="primary", key="signup_submit", use_container_width=True)
            back_clicked = st.button("← 로그인으로 돌아가기", type="secondary", key="back_to_login")

            if back_clicked:
                st.session_state["_auth_view"] = "login"
                st.rerun()

            if signup_clicked:
                if new_password != new_password_confirm:
                    st.error("비밀번호가 서로 일치하지 않습니다.")
                else:
                    try:
                        ok, message = create_signup_request(new_username, new_password)
                    except Exception as ex:
                        ok, message = False, f"신청 중 오류가 발생했습니다: {ex}"
                    if ok:
                        st.success(message)
                    else:
                        st.error(message)

    return False


if not check_login():
    st.stop()

st.sidebar.caption(f"👤 {st.session_state.get('username', '')}" + (" (관리자)" if st.session_state.get("is_admin") else ""))
if st.sidebar.button("로그아웃"):
    st.session_state.clear()
    st.rerun()


# ---- 무거운 리소스는 로그인 이후에만 로드하고, 캐싱해서 한 번만 로드 ----
@st.cache_resource
def get_anthropic_client():
    return anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))


@st.cache_resource
def get_qdrant_client():
    return QdrantClient(url=get_secret("QDRANT_URL"), api_key=get_secret("QDRANT_API_KEY"))


client = get_anthropic_client()
qdrant = get_qdrant_client()

# Neo4j 연결 확인 (실패해도 앱 자체는 계속 뜨도록 처리)
neo4j_ok = True
try:
    run_cypher("RETURN 1 AS ok")
except Exception as ex:
    neo4j_ok = False
    st.warning(f"Neo4j 연결에 실패했습니다 (그래프 확장 기능 없이 벡터 검색만 동작합니다): {ex}")


# ---- 관리자 패널: 회원가입 승인/거절 ----
if st.session_state.get("is_admin") and neo4j_ok:
    with st.expander("🛠️ 관리자 - 회원가입 승인 대기 목록", expanded=True):
        try:
            pending = run_cypher(
                "MATCH (u:User {status: 'pending'}) RETURN u.username AS username ORDER BY u.requested_at"
            )
        except Exception as ex:
            pending = []
            st.error(f"목록을 불러오지 못했습니다: {ex}")

        if not pending:
            st.caption("대기 중인 신청이 없습니다.")
        for row in pending:
            uname = row["username"]
            col1, col2, col3 = st.columns([3, 1, 1])
            col1.write(uname)
            if col2.button("승인", key=f"approve_{uname}"):
                run_cypher("MATCH (u:User {username: $u}) SET u.status = 'approved'", {"u": uname})
                st.rerun()
            if col3.button("거절", key=f"reject_{uname}"):
                run_cypher("MATCH (u:User {username: $u}) DELETE u", {"u": uname})
                st.rerun()


# ---- CS 지식 컬렉션: Colab 적재 노트북(notebooks/cs_kb_ingest.ipynb)이 만든 3개만 검색 ----
# 같은 Qdrant에 있는 2주차 실습 컬렉션(chunks_doc1 등)은 검색 대상에서 제외합니다.
CS_COLLECTIONS = {
    "chunks_cs_products": "상품 정보",
    "chunks_cs_policy": "고객·배송 안내",
    "chunks_cs_history": "문의 이력",
}
HISTORY_TURNS = 6  # 에이전트에 넘길 최근 대화 메시지 수(질문·답변 합산)


def list_cs_collections():
    try:
        existing = {c.name for c in qdrant.get_collections().collections}
    except Exception:
        return []
    return [name for name in CS_COLLECTIONS if name in existing]


available_collections = list_cs_collections()

with st.sidebar:
    st.markdown("## 📚 지식 현황")
    with st.container(border=True):
        if available_collections:
            for name in available_collections:
                st.markdown(f"✅ {CS_COLLECTIONS[name]}")
        else:
            st.error("적재된 CS 지식이 없습니다. Colab 적재 노트북을 먼저 실행하세요.")
    if st.button("🧹 새 상담 시작", use_container_width=True):
        st.session_state.messages = []
        st.rerun()
    st.caption("새 상담을 시작하면 앞 대화 맥락이 초기화됩니다.")
    st.divider()


# ---- 개인정보 마스킹: 문의 원문을 Claude·임베딩 API로 보내기 전에 적용 ----
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


# ---- 규정 검사: 의약외품(생리대) 효능 표현 경고 ----
# 단어 일치 방식이라 "효과는 안내드릴 수 없습니다" 같은 부정문에도 걸릴 수 있으므로 차단하지 않고 경고만 표시합니다.
BANNED_PATTERN = re.compile(
    r"생리통|생리불순|질염|발진|가려움|짓무름|냄새\s*제거|예방|완화|치료|개선|항균|살균|무독성|1위"
)


# 부정·불가 표현이 있는 문장("효과는 안내드릴 수 없습니다")은 단순 오탐일 가능성이 높아 경고 대신 '확인 권장'으로 분리합니다.
# 단, "트러블이 없습니다"처럼 부정형 보장 표현도 있으므로 제외하지 않고 판매자에게 보여줍니다.
NEGATION_PATTERN = re.compile(r"(?:안내|말씀)드릴[^.!?]{0,6}없|보장[^.!?]{0,10}(?:어렵|없|않)|확인되지\s*않|검증된\s*바\s*없|효과는\s*없")


def extract_draft(answer: str) -> str:
    """에이전트 출력에서 고객에게 보낼 '답변 초안' 부분만 추출(근거·확인 필요 절 제외)"""
    draft = re.split(r"\*\*근거\*\*|\*\*확인 필요\*\*", answer)[0]
    return draft.replace("**답변 초안**", "").strip()


def check_compliance(text: str):
    claims, negated = set(), set()
    for sentence in re.split(r"(?<=[.!?。])\s+|\n+", extract_draft(text)):
        hits = set(BANNED_PATTERN.findall(sentence))
        if not hits:
            continue
        (negated if NEGATION_PATTERN.search(sentence) else claims).update(hits)
    return {"ok": not claims, "hits": sorted(claims), "negated_hits": sorted(negated - claims)}


# ---- 질문 임베딩: Cloudflare Workers AI의 bge-m3 (앱 안에서 모델을 돌리지 않음) ----
# Colab의 로컬 bge-m3 벡터와 코사인 유사도 1.0000으로 일치함을 확인했습니다(CLS 풀링, 1024차원).
def embed_query(text: str) -> list:
    account_id = (get_secret("CF_ACCOUNT_ID") or "").strip()
    token = (get_secret("CF_API_TOKEN") or "").strip()
    if not account_id or not token:
        raise RuntimeError("CF_ACCOUNT_ID 또는 CF_API_TOKEN이 설정되지 않았습니다.")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/baai/bge-m3"
    last = None
    for attempt in range(3):
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"text": [text], "truncate_inputs": True},
                timeout=20,
            )
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
def hybrid_search(query: str, top_k: int = 5):
    query_vec = embed_query(query)

    scored_hits = []
    for coll in available_collections:
        try:
            response = qdrant.query_points(collection_name=coll, query=query_vec, limit=top_k)
        except Exception:
            continue
        for hit in response.points:
            scored_hits.append((hit.score, hit, coll))

    scored_hits.sort(key=lambda x: x[0], reverse=True)
    scored_hits = scored_hits[:top_k]

    results = []
    for score, hit, coll in scored_hits:
        entry = {
            "score": round(score, 3),
            "source_document": CS_COLLECTIONS.get(coll, coll),
            "section": hit.payload.get("section", ""),
            "text": hit.payload["text"],
            "related_facts": [],
        }
        entity_ids = hit.payload.get("entity_ids")
        if neo4j_ok and entity_ids:
            try:
                entry["related_facts"] = run_cypher(
                    """
                    MATCH (a:Entity)-[r:RELATES]-(b:Entity)
                    WHERE a.canonical_id IN $ids
                    RETURN a.name AS source, r.type AS relation, b.name AS target
                    LIMIT 10
                    """,
                    {"ids": entity_ids},
                )
            except Exception:
                pass
        results.append(entry)
    return results


# ---- 에이전트 tool-use 로직 ----
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

[출력 형식]
**답변 초안**
(고객에게 보낼 문장. "안녕하세요, {BRAND_NAME}입니다."로 시작, 3~6문장, 정중한 존댓말)

**근거**
- [출처 문서] 근거 내용 한 줄 (조회 결과의 source_document를 그대로 사용)

**확인 필요** (근거가 부족한 내용이 있을 때만)
- 판매자가 확인해야 할 사항
"""


def run_tool(name, tool_input):
    if name == "search_knowledge":
        try:
            return json.dumps(hybrid_search(tool_input["query"]), ensure_ascii=False)
        except Exception as ex:
            return json.dumps({"error": f"검색에 실패했습니다: {ex}"}, ensure_ascii=False)
    return "알 수 없는 도구입니다."


def build_history(messages: list, max_messages: int = HISTORY_TURNS):
    """최근 대화를 텍스트만 추려 Claude messages 형식으로 변환 (2주차 앱 한계: 대화 기록 미전달 → 개선)"""
    recent = messages[-max_messages:]
    history = [{"role": m["role"], "content": m["content"]} for m in recent]
    while history and history[0]["role"] != "user":   # 첫 메시지는 user여야 함
        history.pop(0)
    return history


def is_malformed(text: str) -> bool:
    """도구 호출을 텍스트로 흉내 냈거나(<invoke ...>), 정해진 출력 형식이 빠진 경우"""
    return "<invoke" in text or "<parameter" in text or "답변 초안" not in text


def ask_agent(question: str, history: list):
    messages = history + [{"role": "user", "content": question}]
    retried = False
    for step in range(6):  # 도구 호출 반복 상한
        kwargs = dict(model="claude-sonnet-5", max_tokens=2000, tools=AGENT_TOOLS,
                      system=SYSTEM_PROMPT, messages=messages)
        if step == 0:
            # 첫 응답은 반드시 검색 도구를 호출(근거 없이 답하거나 도구 호출을 텍스트로 흉내 내는 오류 방지)
            kwargs["tool_choice"] = {"type": "any"}
        response = client.messages.create(**kwargs)

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
            {"type": "tool_result", "tool_use_id": b.id, "content": run_tool(b.name, b.input)}
            for b in tool_use_blocks
        ]})
    return "검색을 여러 번 반복했지만 답변을 정리하지 못했습니다. 질문을 조금 더 구체적으로 입력해 주세요."


# ---- 채팅 UI ----
st.title(f"💬 {BRAND_NAME} CS 답변 보조 챗봇")
st.caption("고객 문의를 붙여넣으면 상품 정보·정책·과거 문의 이력(Neo4j 지식그래프 + Qdrant 벡터 검색)에 근거한 "
           "답변 초안을 만듭니다. 초안은 반드시 검토 후 발송하세요.")

if "messages" not in st.session_state:
    st.session_state.messages = []


def render_checks(meta: dict):
    if meta.get("masked"):
        st.info("🔒 문의에 포함된 개인정보(전화번호·주문번호 등)를 가린 뒤 처리했습니다.")
    hits = meta.get("compliance_hits") or []
    negated = meta.get("negated_hits") or []
    if hits:
        st.warning(f"⚠️ 규정 검사: 답변 초안에 의약외품 광고에서 주의할 표현({', '.join(hits)})이 있습니다. "
                   "효능을 주장하는 문장인지 확인 후 발송하세요.")
    elif negated:
        st.caption(f"🔎 규정 검사: 부정문 안에 주의 단어({', '.join(negated)})가 있습니다. "
                   "효능을 부정하는 문장인지 한 번 확인하세요.")
    elif meta.get("checked"):
        st.caption("✅ 규정 검사 통과(답변 초안에 효능 표현 없음)")


def handle_question(question: str):
    masked_q, was_masked = mask_pii(question)
    history = build_history(st.session_state.messages)

    st.session_state.messages.append({"role": "user", "content": masked_q})
    with st.chat_message("user"):
        st.markdown(masked_q)

    with st.chat_message("assistant"):
        with st.spinner("근거를 찾아 답변 초안을 작성하는 중..."):
            try:
                answer = ask_agent(masked_q, history)
            except Exception as ex:
                answer = f"답변 생성 중 오류가 발생했습니다: {ex}"
        result = check_compliance(answer)
        meta = {"masked": was_masked, "checked": True,
                "compliance_hits": result["hits"], "negated_hits": result["negated_hits"]}
        st.markdown(answer)
        render_checks(meta)

    st.session_state.messages.append({"role": "assistant", "content": answer, "meta": meta})


pending_question = st.session_state.pop("pending_question", None)

if not st.session_state.messages:
    st.markdown("**💡 고객 문의 예시**")
    example_questions = {
        "성분·소재": "100% 유기농 생리대 맞나요?",
        "피부·안전": "피부가 예민한데 써도 될까요? 생리통도 줄어드나요?",
        "교환·반품": "사이즈를 잘못 주문했는데 교환되나요?",
        "주문 취소": "방금 주문했는데 취소하고 싶어요. 연락처는 010-1234-5678입니다.",
        "소비기한": "유통기한이 어떻게 되나요?",
    }
    cols = st.columns(2)
    for i, (label, q) in enumerate(example_questions.items()):
        if cols[i % 2].button(f"👉 {label}", key=f"example_{label}", use_container_width=True):
            st.session_state["pending_question"] = q
            st.rerun()

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant" and msg.get("meta"):
            render_checks(msg["meta"])

typed_question = st.chat_input("고객 문의를 붙여넣으세요 (이어지는 질문도 가능)")
question = typed_question or pending_question

if question:
    handle_question(question)
