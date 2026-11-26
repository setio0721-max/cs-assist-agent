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
import bcrypt
import requests

from cs_core import (BRAND_NAME, CS_COLLECTIONS, REQUIRED_SETTINGS, CSAssistant, mask_pii)

COLLECTION_PREFIX = "chunks_"

# 브랜드·회사명, 검색·에이전트 로직은 cs_core.py에서 관리(데스크톱 프로그램과 공유)

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


# ---- 핵심 로직(cs_core.CSAssistant)은 로그인 이후에만 만들고, 캐싱해서 한 번만 생성 ----
@st.cache_resource
def get_core():
    core = CSAssistant({k: get_secret(k) or "" for k in REQUIRED_SETTINGS})
    core.connect()
    return core


core = get_core()
neo4j_ok = core.neo4j_ok
if not neo4j_ok:
    st.warning("Neo4j 연결에 실패했습니다 (그래프 확장 기능 없이 벡터 검색만 동작합니다).")


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


# ---- 사이드바: 지식 현황 (Colab 적재 노트북이 만든 CS 컬렉션 3개만 검색) ----
available_collections = core.collections

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
    with st.chat_message("user"):
        st.markdown(mask_pii(question)[0])   # 화면에도 마스킹된 문의만 표시

    with st.chat_message("assistant"):
        with st.spinner("근거를 찾아 답변 초안을 작성하는 중..."):
            try:
                # 마스킹 → 에이전트(검색·답변) → 규정 검사 (cs_core.CSAssistant.answer)
                masked_q, answer, meta = core.answer(question, st.session_state.messages)
            except Exception as ex:
                masked_q, answer, meta = question, f"답변 생성 중 오류가 발생했습니다: {ex}", {}
        st.markdown(answer)
        render_checks(meta)

    st.session_state.messages.append({"role": "user", "content": masked_q})
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
