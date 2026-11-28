"""
CS 답변 보조 챗봇 - 데스크톱 프로그램 (Windows 포터블 exe로 빌드)

화면 한쪽에 '항상 위'로 띄워 두고 사용한다.
  고객 문의 붙여넣기 → [추천 받기] → 답변 초안·근거·규정 검사 확인 → [답변 복사] → 스마트스토어에 붙여넣기

설정(API 키) 읽는 순서
  1) 프로그램(exe)과 같은 폴더의 .env  — KEY=VALUE 또는 KEY = "VALUE" 형식 모두 가능
  2) %APPDATA%\\CSAssistant\\config.json  — [설정] 화면에서 입력·저장한 값
키는 exe·zip·GitHub에 포함하지 않는다.

운영 설정([설정] 창의 탭)
  재고·소비기한 / 상품 정보 / 배송 공지(명절) / 알림 — 저장하면 다음 답변부터 바로 반영
  기한이 다가오면 메인 창 위쪽에 알림 배너 표시(cs_ops.py)
"""

import json
import os
import re
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import cs_ops
from cs_core import BRAND_NAME, REQUIRED_SETTINGS, CSAssistant, mask_pii, split_sections

APP_NAME = "CSAssistant"
APP_TITLE = f"{BRAND_NAME} CS 답변 추천"
VERSION = "1.1"

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
        self.messages = []          # 대화 기록(Claude messages 형식)
        self.busy = False
        self.alerts = []
        self.banner_hidden = False

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

        # 알림 배너(알림이 있을 때만 표시)
        self.banner = tk.Frame(self.root, background="#fff4ce")
        self.banner_label = tk.Label(self.banner, text="", anchor="w", justify="left", wraplength=300,
                                     background="#fff4ce", font=self.font)
        self.banner_label.pack(side="left", fill="x", expand=True, padx=6, pady=4)
        ttk.Button(self.banner, text="닫기", width=5, command=self.hide_banner).pack(side="right", padx=(0, 6))
        ttk.Button(self.banner, text="자세히", width=7, command=lambda: self.open_settings("알림")).pack(side="right", padx=2)
        self.banner_anchor = bar

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
        self.refresh_alerts()

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
        if meta.get("notices"):
            notes.append("📢 적용 중인 배송 공지: " + ", ".join(meta["notices"]))
            self.check_label.configure(text="\n".join(notes))
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

    # ---- 알림 배너 ----
    def refresh_alerts(self, schedule=True):
        if self.core is None or not self.core.neo4j_ok:
            return

        def work():
            try:
                ops = self.core.load_ops()
                alerts = cs_ops.compute_alerts(ops)
                self.root.after(0, self.show_alerts, alerts)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()
        if schedule:
            self.root.after(60 * 60 * 1000, self.refresh_alerts)   # 1시간마다 다시 확인(날짜가 바뀌는 경우)

    def show_alerts(self, alerts):
        self.alerts = alerts
        if not alerts or self.banner_hidden:
            self.banner.pack_forget()
            return
        colors = {"warn": "#fde2e1", "notice": "#dbeafe", "info": "#fff4ce"}
        bg = colors.get(alerts[0][1], "#fff4ce")
        text = alerts[0][2] + (f"  (외 {len(alerts) - 1}건)" if len(alerts) > 1 else "")
        for w in (self.banner, self.banner_label):
            w.configure(background=bg)
        self.banner_label.configure(text=text)
        self.banner.pack(fill="x", padx=8, pady=(0, 4), after=self.banner_anchor)

    def hide_banner(self):
        self.banner_hidden = True       # 이번 실행 동안만 숨김(확인 처리는 [설정 → 알림])
        self.banner.pack_forget()

    # ---- 설정 화면(연결 설정 + 운영 설정 탭) ----
    def open_settings(self, tab: str = None):
        win = tk.Toplevel(self.root)
        win.title("설정")
        win.attributes("-topmost", True)
        win.transient(self.root)
        win.geometry("620x560")
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        tabs = {}

        conn = ttk.Frame(nb, padding=10)
        nb.add(conn, text="연결 설정")
        tabs["연결 설정"] = conn
        self._build_conn_tab(conn, win)

        ops = None
        if self.core is not None and self.core.neo4j_ok:
            try:
                ops = self.core.load_ops()
            except Exception as ex:
                messagebox.showwarning("설정", f"운영 정보를 불러오지 못했습니다: {ex}", parent=win)
        for name, builder in [("재고·소비기한", self._build_expiry_tab), ("상품 정보", self._build_products_tab),
                              ("배송 공지", self._build_notice_tab), ("알림", self._build_alert_tab)]:
            frm = ttk.Frame(nb, padding=10)
            nb.add(frm, text=name)
            tabs[name] = frm
            if ops is None:
                ttk.Label(frm, text="연결된 뒤 사용할 수 있습니다.\n[연결 설정]에서 키를 저장하고 연결하세요.",
                          foreground="#555", justify="left").pack(anchor="w")
            else:
                builder(frm, win, ops)
        if tab in tabs:
            nb.select(tabs[tab])

    def _save_ops(self, ops, win, msg="저장했습니다. 다음 답변부터 반영됩니다."):
        try:
            self.core.save_ops(ops)
        except Exception as ex:
            messagebox.showerror("설정", f"저장 실패: {ex}", parent=win)
            return False
        messagebox.showinfo("설정", msg, parent=win)
        self.banner_hidden = False
        self.refresh_alerts(schedule=False)
        return True

    # 연결 설정 탭(API 키)
    def _build_conn_tab(self, frm, win):
        values, source = load_settings()
        ttk.Label(frm, text=f"현재 설정 출처: {source}", foreground="#555").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frm, text="exe 옆에 .env 파일을 두면 그 값이 우선 사용됩니다.\n"
                            "여기서 저장한 값은 이 PC에만 저장됩니다.",
                  foreground="#555", justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))
        entries = {}
        for i, key in enumerate(REQUIRED_SETTINGS, start=2):
            ttk.Label(frm, text=SETTING_LABELS[key]).grid(row=i, column=0, sticky="w", pady=2)
            e = ttk.Entry(frm, width=42, show="•" if key in SECRET_KEYS else "")
            e.insert(0, values.get(key, ""))
            e.grid(row=i, column=1, pady=2, sticky="w")
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

        ttk.Button(frm, text="저장 후 연결", command=on_save).grid(row=len(REQUIRED_SETTINGS) + 2, column=1, sticky="e", pady=(10, 0))

    # 재고·소비기한 탭
    def _build_expiry_tab(self, frm, win, ops):
        ex, cfg = ops["expiry"], ops["alert"]
        ttk.Label(frm, text="새 재고가 들어와 소비기한이 바뀌면 여기서 고치세요. 저장하면 다음 답변부터 반영됩니다.",
                  foreground="#555", wraplength=560, justify="left").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        fields = [("현재 재고 소비기한 (YYYY-MM-DD)", ex.get("date", "")),
                  ("소비기한 기준", ex.get("shelf_life", "")),
                  ("소비기한 몇 개월 전부터 알림", str(cfg.get("expiry_months", 6))),
                  ("며칠 동안 확인 안 하면 알림", str(cfg.get("review_days", 90))),
                  ("명절 며칠 전부터 공지 등록 알림", str(cfg.get("holiday_remind_days", 14)))]
        es = []
        for i, (label, val) in enumerate(fields, start=1):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=4)
            e = ttk.Entry(frm, width=24)
            e.insert(0, val)
            e.grid(row=i, column=1, sticky="w", pady=4)
            es.append(e)
        ttk.Label(frm, text=f"마지막 확인일: {ex.get('updated', '')}", foreground="#555").grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))

        def on_save():
            try:
                d = cs_ops.parse_date(es[0].get())
                nums = [int(es[k].get()) for k in (2, 3, 4)]
            except ValueError:
                messagebox.showwarning("설정", "날짜는 2028-12-29처럼, 알림 기준은 숫자로 입력하세요.", parent=win)
                return
            ops["expiry"] = {"date": d.isoformat(), "shelf_life": es[1].get().strip() or "제조일로부터 3년",
                             "updated": cs_ops.today_kst().isoformat()}
            ops["alert"].update({"expiry_months": nums[0], "review_days": nums[1], "holiday_remind_days": nums[2]})
            self._save_ops(ops, win)

        ttk.Button(frm, text="저장 (내용 그대로면 '확인함'으로 기록)", command=on_save).grid(row=7, column=0, columnspan=2, sticky="w", pady=12)

    # 상품 정보 탭
    def _build_products_tab(self, frm, win, ops):
        ttk.Label(frm, text="규격·포장 수량 등이 바뀌면 행을 더블클릭해 고치고 [저장]을 누르세요.",
                  foreground="#555").pack(anchor="w", pady=(0, 6))
        cols = cs_ops.PRODUCT_FIELDS
        tree = ttk.Treeview(frm, columns=cols, show="headings", height=10)
        widths = {"name": 190, "size": 70, "length": 60, "absorb": 80, "count": 70, "note": 80}
        for c in cols:
            tree.heading(c, text=cs_ops.PRODUCT_LABELS[c])
            tree.column(c, width=widths[c], anchor="w")
        tree.pack(fill="both", expand=True)
        rows = [dict(p) for p in ops["products"]]

        def redraw():
            tree.delete(*tree.get_children())
            for i, p in enumerate(rows):
                tree.insert("", "end", iid=str(i), values=[p.get(c, "") for c in cols])

        def edit(idx=None):
            data = rows[idx] if idx is not None else {c: "" for c in cols}
            res = self._form_dialog(win, "상품 정보", cols, cs_ops.PRODUCT_LABELS, data)
            if res is None:
                return
            if not res["name"]:
                messagebox.showwarning("상품 정보", "제품명을 입력하세요.", parent=win)
                return
            if idx is None:
                rows.append(res)
            else:
                rows[idx] = res
            redraw()

        def selected():
            sel = tree.selection()
            return int(sel[0]) if sel else None

        def delete():
            idx = selected()
            if idx is not None and messagebox.askyesno("상품 정보", f"'{rows[idx]['name']}'을(를) 목록에서 뺄까요?", parent=win):
                rows.pop(idx)
                redraw()

        def on_save():
            today = cs_ops.today_kst().isoformat()
            ops["products"] = rows
            ops["products_updated"] = today
            ops["products_edited_at"] = today
            self._save_ops(ops, win)

        tree.bind("<Double-1>", lambda e: edit(selected()) if selected() is not None else None)
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=8)
        ttk.Button(btns, text="추가", command=lambda: edit(None)).pack(side="left")
        ttk.Button(btns, text="수정", command=lambda: edit(selected()) if selected() is not None else None).pack(side="left", padx=4)
        ttk.Button(btns, text="삭제", command=delete).pack(side="left")
        ttk.Button(btns, text="저장", command=on_save).pack(side="right")
        ttk.Label(frm, text=f"마지막 수정일: {ops.get('products_updated', '')}", foreground="#555").pack(anchor="w")
        redraw()

    # 배송 공지 탭
    def _build_notice_tab(self, frm, win, ops):
        ttk.Label(frm, text="명절·연휴 배송 공지를 등록하면 명절 시작 며칠 전(기본 7일)부터 종료일까지\n"
                            "배송·주문 문의 답변에 자동으로 들어가고, 메인 창에 배너가 표시됩니다.",
                  foreground="#555", justify="left").pack(anchor="w", pady=(0, 6))
        cols = ["title", "start", "end", "lead_days", "order_cutoff"]
        tree = ttk.Treeview(frm, columns=cols + ["state"], show="headings", height=8)
        widths = {"title": 150, "start": 85, "end": 85, "lead_days": 60, "order_cutoff": 120, "state": 60}
        for c in cols + ["state"]:
            tree.heading(c, text=cs_ops.NOTICE_LABELS.get(c, "상태"))
            tree.column(c, width=widths[c], anchor="w")
        tree.pack(fill="both", expand=True)
        rows = [dict(n) for n in ops["notices"]]

        def state(n):
            try:
                show_from, end = cs_ops.notice_window(n)
            except (KeyError, ValueError):
                return "날짜 오류"
            t = cs_ops.today_kst()
            return "적용 중" if show_from <= t <= end else ("예정" if t < show_from else "종료")

        def redraw():
            tree.delete(*tree.get_children())
            for i, n in enumerate(rows):
                tree.insert("", "end", iid=str(i), values=[n.get(c, "") for c in cols] + [state(n)])

        def edit(idx=None, preset=None):
            data = rows[idx] if idx is not None else (preset or {c: "" for c in cs_ops.NOTICE_FIELDS})
            data.setdefault("lead_days", 7)
            res = self._form_dialog(win, "배송 공지", cs_ops.NOTICE_FIELDS, cs_ops.NOTICE_LABELS, data, multiline={"notes"})
            if res is None:
                return
            try:
                cs_ops.parse_date(res["start"]), cs_ops.parse_date(res["end"])
                res["lead_days"] = int(res["lead_days"] or 7)
            except ValueError:
                messagebox.showwarning("배송 공지", "날짜는 2027-02-06처럼, '며칠 전부터'는 숫자로 입력하세요.", parent=win)
                return
            if not res["title"]:
                messagebox.showwarning("배송 공지", "공지 이름을 입력하세요.", parent=win)
                return
            if idx is None:
                rows.append(res)
            else:
                rows[idx] = res
            redraw()

        def next_holiday():
            t = cs_ops.today_kst()
            for h in cs_ops.HOLIDAYS:
                if cs_ops.parse_date(h["end"]) >= t:
                    edit(None, {"title": h["name"] + " 배송 안내", "start": h["start"], "end": h["end"], "lead_days": 7,
                                "order_cutoff": "", "office_closed": "", "delivery_closed": "", "notes": ""})
                    return
            messagebox.showinfo("배송 공지", "등록된 명절 목록이 없습니다. [추가]로 직접 입력하세요.", parent=win)

        def selected():
            sel = tree.selection()
            return int(sel[0]) if sel else None

        def delete():
            idx = selected()
            if idx is not None and messagebox.askyesno("배송 공지", f"'{rows[idx]['title']}'을(를) 삭제할까요?", parent=win):
                rows.pop(idx)
                redraw()

        def on_save():
            ops["notices"] = rows
            self._save_ops(ops, win)

        tree.bind("<Double-1>", lambda e: edit(selected()) if selected() is not None else None)
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=8)
        ttk.Button(btns, text="다가오는 명절로 추가", command=next_holiday).pack(side="left")
        ttk.Button(btns, text="직접 추가", command=lambda: edit(None)).pack(side="left", padx=4)
        ttk.Button(btns, text="수정", command=lambda: edit(selected()) if selected() is not None else None).pack(side="left")
        ttk.Button(btns, text="삭제", command=delete).pack(side="left", padx=4)
        ttk.Button(btns, text="저장", command=on_save).pack(side="right")
        redraw()

    # 알림 탭
    def _build_alert_tab(self, frm, win, ops):
        ttk.Label(frm, text="확인한 알림은 선택 후 [확인 처리]를 누르면 배너에 다시 나오지 않습니다.\n"
                            "(내용이 바뀌면 새 알림으로 다시 표시)", foreground="#555", justify="left").pack(anchor="w", pady=(0, 6))
        tree = ttk.Treeview(frm, columns=["level", "msg"], show="headings", height=12)
        tree.heading("level", text="구분")
        tree.heading("msg", text="내용")
        tree.column("level", width=60, anchor="w")
        tree.column("msg", width=500, anchor="w")
        tree.pack(fill="both", expand=True)
        names = {"warn": "주의", "notice": "공지", "info": "안내"}
        acks = set(ops.get("acks", []))
        alerts = cs_ops.compute_alerts(ops, include_acked=True)
        for aid, level, msg in alerts:
            tree.insert("", "end", iid=aid, values=[names.get(level, level) + (" ✓" if aid in acks else ""), msg])
        if not alerts:
            ttk.Label(frm, text="현재 알림이 없습니다.").pack(anchor="w", pady=6)

        def ack():
            sel = list(tree.selection())
            if not sel:
                return
            ops["acks"] = sorted(acks | set(sel))[-200:]
            if self._save_ops(ops, win, "확인 처리했습니다."):
                for aid in sel:
                    vals = tree.item(aid, "values")
                    tree.item(aid, values=[vals[0].rstrip(" ✓") + " ✓", vals[1]])

        ttk.Button(frm, text="확인 처리", command=ack).pack(anchor="e", pady=8)

    # 입력 대화상자(상품·공지 공용)
    def _form_dialog(self, parent, title, fields, labels, data, multiline=frozenset()):
        dlg = tk.Toplevel(parent)
        dlg.title(title)
        dlg.attributes("-topmost", True)
        dlg.transient(parent)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=10)
        frm.pack(fill="both", expand=True)
        widgets, result = {}, {}
        for i, f in enumerate(fields):
            ttk.Label(frm, text=labels[f]).grid(row=i, column=0, sticky="nw", pady=3, padx=(0, 8))
            if f in multiline:
                w = tk.Text(frm, width=40, height=4, wrap="word", font=self.font)
                w.insert("1.0", str(data.get(f, "")))
            else:
                w = ttk.Entry(frm, width=42)
                w.insert(0, str(data.get(f, "")))
            w.grid(row=i, column=1, sticky="w", pady=3)
            widgets[f] = w

        def ok():
            for f, w in widgets.items():
                result[f] = (w.get("1.0", "end") if f in multiline else w.get()).strip()
            dlg.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=len(fields), column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="취소", command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text="확인", command=ok).pack(side="right", padx=6)
        parent.wait_window(dlg)
        return result or None


def selftest() -> int:
    """빌드 검증용(--selftest): exe 안에 필요한 모듈·인증서가 모두 들어갔는지 확인하고 결과를 selftest.log에 기록"""
    lines, code = [], 0
    try:
        import anthropic, certifi, httpx, requests  # noqa: F401
        anthropic.Anthropic(api_key="selftest")
        assert os.path.exists(certifi.where()), "certifi 인증서 파일 없음"
        assert parse_env_text('ANTHROPIC_API_KEY = "a"')["ANTHROPIC_API_KEY"] == "a"
        assert mask_pii("010-1234-5678")[1]
        assert cs_ops.ops_context(cs_ops.merge_defaults({}))
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
