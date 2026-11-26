"""
CS 답변 보조 챗봇 - 데스크톱 프로그램 (Windows 포터블 exe로 빌드)

화면 한쪽에 '항상 위'로 띄워 두고 사용한다.
  고객 문의 붙여넣기 → [추천 받기] → 답변 초안·근거·규정 검사 확인 → [답변 복사] → 스마트스토어에 붙여넣기

설정(API 키) 읽는 순서
  1) 프로그램(exe)과 같은 폴더의 .env  — KEY=VALUE 또는 Streamlit secrets.toml 형식(KEY = "VALUE") 모두 가능
  2) %APPDATA%\\CSAssistant\\config.json  — [설정] 화면에서 입력·저장한 값
키는 exe·zip·GitHub에 포함하지 않는다.

검색·답변 로직은 웹 앱과 같은 cs_core.py를 사용한다.
"""

import json
import os
import re
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from cs_core import BRAND_NAME, REQUIRED_SETTINGS, CSAssistant, mask_pii, split_sections

APP_NAME = "CSAssistant"
APP_TITLE = f"{BRAND_NAME} CS 답변 추천"
VERSION = "1.0"

SETTING_LABELS = {
    "ANTHROPIC_API_KEY": "Anthropic API 키",
    "NEO4J_URI": "Neo4j URI (neo4j+s://...)",
    "NEO4J_USER": "Neo4j 사용자",
    "NEO4J_PASSWORD": "Neo4j 비밀번호",
    "NEO4J_DATABASE": "Neo4j 데이터베이스",
    "QDRANT_URL": "Qdrant URL",
    "QDRANT_API_KEY": "Qdrant API 키",
    "CF_ACCOUNT_ID": "Cloudflare 계정 ID",
    "CF_API_TOKEN": "Cloudflare API 토큰",
}
SECRET_KEYS = {"ANTHROPIC_API_KEY", "NEO4J_PASSWORD", "QDRANT_API_KEY", "CF_API_TOKEN"}


# =====================================================================
# 설정 파일
# =====================================================================
def app_dir() -> str:
    """exe로 실행하면 exe가 있는 폴더, 스크립트로 실행하면 이 파일이 있는 폴더"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_NAME, "config.json")


def parse_env_text(text: str) -> dict:
    """KEY=VALUE, KEY = "VALUE"(secrets.toml) 형식을 모두 읽음. 주석(#)·빈 줄 무시"""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("[") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] in "\"'":
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = re.split(r"\s+#", value)[0].strip()
        if key in REQUIRED_SETTINGS:
            values[key] = value
    return values


def load_settings():
    """(설정값, 출처 설명) 반환. .env가 있으면 우선"""
    env_file = os.path.join(app_dir(), ".env")
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8-sig") as f:
            env_values = parse_env_text(f.read())
        if settings_complete(env_values):          # .env가 비어 있거나 일부만 있으면 설정 화면 저장값 사용
            return env_values, f".env ({env_file})"
    path = config_path()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f), "설정 화면 저장값"
    return {}, "없음"


def save_settings(values: dict):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(values, f, ensure_ascii=False, indent=2)
    return path


def settings_complete(values: dict) -> bool:
    return all((values.get(k) or "").strip() for k in REQUIRED_SETTINGS if k != "NEO4J_DATABASE")


# =====================================================================
# 메인 창
# =====================================================================
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.core = None
        self.messages = []          # 대화 기록(웹 앱과 같은 형식)
        self.busy = False

        root.title(APP_TITLE)
        root.geometry("440x760")
        root.minsize(360, 560)
        self.font = ("Malgun Gothic", 10) if sys.platform.startswith("win") else ("Noto Sans CJK KR", 10)
        self.font_b = (self.font[0], 10, "bold")

        self.topmost = tk.BooleanVar(value=True)
        root.attributes("-topmost", True)

        self._build_ui()
        root.bind("<Control-Return>", lambda e: self.on_recommend())
        root.after(200, self.init_core)

    # ---- 화면 구성 ----
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", **pad)
        ttk.Checkbutton(bar, text="항상 위", variable=self.topmost, command=self.toggle_topmost).pack(side="left")
        ttk.Button(bar, text="설정", width=6, command=self.open_settings).pack(side="right")
        ttk.Button(bar, text="새 상담", width=8, command=self.new_session).pack(side="right", padx=4)

        ttk.Label(self.root, text="고객 문의 (붙여넣기 → Ctrl+Enter 또는 [추천 받기])", font=self.font_b).pack(anchor="w", **pad)
        self.input = ScrolledText(self.root, height=6, wrap="word", font=self.font)
        self.input.pack(fill="x", **pad)

        row = ttk.Frame(self.root)
        row.pack(fill="x", **pad)
        ttk.Button(row, text="클립보드 붙여넣기", command=self.paste_clipboard).pack(side="left")
        self.btn_recommend = ttk.Button(row, text="추천 받기", command=self.on_recommend)
        self.btn_recommend.pack(side="right")

        self.status = tk.StringVar(value="연결 준비 중...")
        ttk.Label(self.root, textvariable=self.status, foreground="#555").pack(anchor="w", **pad)

        head = ttk.Frame(self.root)
        head.pack(fill="x", **pad)
        ttk.Label(head, text="답변 초안 (수정 가능)", font=self.font_b).pack(side="left")
        self.btn_copy = ttk.Button(head, text="답변 복사", command=self.copy_draft)
        self.btn_copy.pack(side="right")
        self.draft = ScrolledText(self.root, height=10, wrap="word", font=self.font, background="#fffdf3")
        self.draft.pack(fill="both", expand=True, **pad)

        self.check_label = tk.Label(self.root, text="", anchor="w", justify="left", wraplength=410, font=self.font)
        self.check_label.pack(fill="x", **pad)

        ttk.Label(self.root, text="근거 · 확인 필요", font=self.font_b).pack(anchor="w", **pad)
        self.detail = ScrolledText(self.root, height=9, wrap="word", font=self.font, background="#f5f5f5")
        self.detail.pack(fill="both", expand=True, **pad)
        self.detail.configure(state="disabled")

        ttk.Label(self.root, text=f"v{VERSION} · 초안은 반드시 검토 후 발송하세요", foreground="#888").pack(anchor="e", padx=8, pady=(0, 6))

    # ---- 연결 ----
    def init_core(self):
        values, source = load_settings()
        if not settings_complete(values):
            self.status.set("API 키 설정이 필요합니다. [설정]을 눌러 입력하세요.")
            self.open_settings()
            return
        self.status.set(f"연결 중... (설정: {source})")
        self.set_busy(True)

        def work():
            try:
                core = CSAssistant(values)
                st = core.connect()
                self.root.after(0, self.on_connected, core, st)
            except Exception as ex:
                self.root.after(0, self.on_error, f"연결 실패: {ex}")

        threading.Thread(target=work, daemon=True).start()

    def on_connected(self, core, st):
        self.core = core
        self.set_busy(False)
        parts = [f"지식 {len(core.collections)}/3"]
        parts.append("그래프 연결" if core.neo4j_ok else "그래프 연결 실패(벡터 검색만)")
        if st.get("qdrant") != "ok":
            parts.append(f"Qdrant: {st.get('qdrant')}")
        self.status.set("준비 완료 · " + " · ".join(parts))

    # ---- 추천 ----
    def on_recommend(self):
        if self.busy:
            return
        if self.core is None:
            messagebox.showinfo(APP_TITLE, "아직 연결되지 않았습니다. 설정을 확인하세요.")
            return
        question = self.input.get("1.0", "end").strip()
        if not question:
            self.status.set("고객 문의를 먼저 붙여넣으세요.")
            return
        masked, _ = mask_pii(question)
        self.input.delete("1.0", "end")
        self.input.insert("1.0", masked)     # 화면에도 마스킹된 문의만 남김
        self.set_busy(True)
        self.status.set("근거를 찾아 답변 초안을 작성하는 중... (보통 10~30초)")

        history = list(self.messages)

        def work():
            try:
                masked_q, answer, meta = self.core.answer(question, history)
                self.root.after(0, self.on_answer, masked_q, answer, meta)
            except Exception as ex:
                self.root.after(0, self.on_error, f"답변 생성 오류: {ex}")

        threading.Thread(target=work, daemon=True).start()

    def on_answer(self, masked_q, answer, meta):
        self.set_busy(False)
        self.messages.append({"role": "user", "content": masked_q})
        self.messages.append({"role": "assistant", "content": answer})
        sec = split_sections(answer)

        self.draft.delete("1.0", "end")
        self.draft.insert("1.0", self._plain(sec["draft"]))

        detail = ""
        if sec["evidence"]:
            detail += "[근거]\n" + self._plain(sec["evidence"]) + "\n\n"
        if sec["check"]:
            detail += "[확인 필요]\n" + self._plain(sec["check"]) + "\n"
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", detail.strip() or "(근거 없음)")
        self.detail.configure(state="disabled")

        notes, color = [], "#1b7f3b"
        if meta.get("masked"):
            notes.append("🔒 개인정보(전화번호·주문번호 등)를 가린 뒤 처리했습니다.")
        if meta.get("compliance_hits"):
            notes.append(f"⚠ 규정 검사: 주의 표현({', '.join(meta['compliance_hits'])}) — 효능 주장인지 확인 후 발송")
            color = "#b3261e"
        elif meta.get("negated_hits"):
            notes.append(f"🔎 부정문 안 주의 단어({', '.join(meta['negated_hits'])}) — 한 번 확인 권장")
            color = "#8a5a00"
        else:
            notes.append("✅ 규정 검사 통과(효능 표현 없음)")
        self.check_label.configure(text="\n".join(notes), foreground=color)
        self.status.set(f"완료 · 대화 {len(self.messages) // 2}건째 (이어지는 문의는 그대로 입력)")

    def on_error(self, msg):
        self.set_busy(False)
        self.status.set(msg[:150])
        messagebox.showerror(APP_TITLE, msg)

    # ---- 보조 기능 ----
    @staticmethod
    def _plain(text: str) -> str:
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        return re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.M).strip()

    def copy_draft(self):
        text = self.draft.get("1.0", "end").strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status.set("답변 초안을 복사했습니다. 스마트스토어 답변 칸에 붙여넣으세요.")

    def paste_clipboard(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return
        self.input.delete("1.0", "end")
        self.input.insert("1.0", text)

    def new_session(self):
        self.messages = []
        for w in (self.input, self.draft):
            w.delete("1.0", "end")
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.configure(state="disabled")
        self.check_label.configure(text="")
        self.status.set("새 상담을 시작했습니다.")

    def toggle_topmost(self):
        self.root.attributes("-topmost", self.topmost.get())

    def set_busy(self, busy: bool):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.btn_recommend.configure(state=state)
        self.btn_copy.configure(state=state)

    # ---- 설정 화면 ----
    def open_settings(self):
        values, source = load_settings()
        win = tk.Toplevel(self.root)
        win.title("설정 - API 키")
        win.attributes("-topmost", True)
        win.transient(self.root)
        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text=f"현재 설정 출처: {source}", foreground="#555").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frm, text="웹 앱(Streamlit)의 Secrets와 같은 값을 입력하세요.\n"
                            "exe 옆에 .env 파일을 두면 그 값이 우선 사용됩니다.",
                  foreground="#555", justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))
        entries = {}
        for i, key in enumerate(REQUIRED_SETTINGS, start=2):
            ttk.Label(frm, text=SETTING_LABELS[key]).grid(row=i, column=0, sticky="w", pady=2)
            e = ttk.Entry(frm, width=38, show="•" if key in SECRET_KEYS else "")
            e.insert(0, values.get(key, ""))
            e.grid(row=i, column=1, pady=2)
            entries[key] = e

        def on_save():
            new = {k: e.get().strip() for k, e in entries.items()}
            if not settings_complete(new):
                messagebox.showwarning("설정", "비어 있는 항목이 있습니다.", parent=win)
                return
            path = save_settings(new)
            messagebox.showinfo("설정", f"저장했습니다.\n{path}\n\n이 PC에만 저장되며 다른 PC로 옮겨지지 않습니다.", parent=win)
            win.destroy()
            self.core = None
            self.init_core()

        btns = ttk.Frame(frm)
        btns.grid(row=len(REQUIRED_SETTINGS) + 2, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="취소", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="저장 후 연결", command=on_save).pack(side="right", padx=6)


def selftest() -> int:
    """빌드 검증용(--selftest): exe 안에 필요한 모듈·인증서가 모두 들어갔는지 확인하고 결과를 selftest.log에 기록"""
    lines, code = [], 0
    try:
        import anthropic, certifi, httpx, requests  # noqa: F401
        anthropic.Anthropic(api_key="selftest")
        assert os.path.exists(certifi.where()), "certifi 인증서 파일 없음"
        assert parse_env_text('ANTHROPIC_API_KEY = "a"')["ANTHROPIC_API_KEY"] == "a"
        assert mask_pii("010-1234-5678")[1]
        lines.append("OK")
    except Exception as ex:
        lines.append(f"FAIL: {ex!r}")
        code = 1
    with open(os.path.join(app_dir(), "selftest.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return code


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform.startswith("win") else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
