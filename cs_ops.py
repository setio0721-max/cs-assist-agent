"""
운영 정보·알림 (데스크톱 프로그램의 [설정 → 운영 설정]에서 관리)

쇼핑몰을 운영하다 보면 바뀌는 정보(재고 소비기한, 상품 규격·구성, 명절 배송 공지)를
지식 재적재(Colab) 없이 프로그램에서 바로 고치고, 기한이 다가오면 알림 배너로 알려 준다.

- 저장: Neo4j 노드 (:OpsConfig {key: 'main'}) 하나에 JSON 문자열로 보관 → 어느 PC에서 고쳐도 같은 값
- 답변 반영: 질문할 때마다 최신 운영 정보를 읽어 시스템 프롬프트에 붙임(지식 문서보다 우선)
- 알림: 오늘 날짜(한국 시간) 기준으로 계산
이 파일은 외부 통신 없이 순수 계산만 한다(저장·조회는 cs_core.CSAssistant).
"""

import copy
import json
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# 명절 연휴(대체공휴일 포함) — 출처: 우주항공청 월력요항(2027), 한국천문연구원 달력자료(2028). 해마다 추가
HOLIDAYS = [
    {"name": "2027 설 연휴", "start": "2027-02-06", "end": "2027-02-09"},
    {"name": "2027 추석 연휴", "start": "2027-09-14", "end": "2027-09-16"},
    {"name": "2028 설 연휴", "start": "2028-01-26", "end": "2028-01-28"},
    {"name": "2028 추석 연휴", "start": "2028-10-02", "end": "2028-10-05"},
]

PRODUCT_FIELDS = ["name", "size", "length", "absorb", "count", "note"]
PRODUCT_LABELS = {"name": "제품명", "size": "구분", "length": "길이", "absorb": "흡수량(5단계)",
                  "count": "포장 수량", "note": "비고"}
NOTICE_FIELDS = ["title", "start", "end", "lead_days", "order_cutoff", "office_closed", "delivery_closed", "notes"]
NOTICE_LABELS = {"title": "공지 이름", "start": "명절 시작일", "end": "명절 종료일",
                 "lead_days": "며칠 전부터 안내", "order_cutoff": "택배 주문 마감",
                 "office_closed": "업체 휴무일", "delivery_closed": "배송 휴무일", "notes": "주의 사항"}

# 처음 실행 시 기본값(상품 정보 문서 products.md와 같은 값)
DEFAULT_OPS = {
    "version": 1,
    "expiry": {"date": "2028-12-29", "shelf_life": "제조일로부터 3년", "updated": "2026-11-26"},
    "products": [
        {"name": "온솜결 퓨어 롱 라이너", "size": "롱 라이너", "length": "175mm", "absorb": "1/5", "count": "36개입", "note": ""},
        {"name": "온솜결 퓨어 슬림날개 중형", "size": "중형", "length": "260mm", "absorb": "3/5", "count": "16개입", "note": "날개형"},
        {"name": "온솜결 퓨어 슬림날개 대형", "size": "대형", "length": "290mm", "absorb": "4/5", "count": "14개입", "note": "날개형"},
        {"name": "온솜결 퓨어 슬림날개 오버나이트", "size": "오버나이트", "length": "340mm", "absorb": "5/5", "count": "10개입", "note": "날개형"},
    ],
    "products_updated": "2026-11-26",
    "products_edited_at": "",          # 설정 화면에서 상품 정보를 저장한 날(최근 수정 알림용)
    "notices": [],
    "alert": {"expiry_months": 6, "review_days": 90, "holiday_remind_days": 14},
    "acks": [],
}


def today_kst() -> date:
    return datetime.now(KST).date()


def parse_date(text: str) -> date:
    """'2027-02-06' 형식만 허용(설정 화면 입력 검증에도 사용)"""
    return datetime.strptime(str(text).strip(), "%Y-%m-%d").date()


def merge_defaults(ops: dict) -> dict:
    """저장된 값에 빠진 항목을 기본값으로 채움(버전이 올라가도 기존 값 유지)"""
    out = copy.deepcopy(DEFAULT_OPS)
    for k, v in (ops or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def notice_window(n: dict):
    """(안내 시작일, 명절 종료일)"""
    start, end = parse_date(n["start"]), parse_date(n["end"])
    return start - timedelta(days=int(n.get("lead_days") or 7)), end


def active_notices(ops: dict, today: date = None):
    today = today or today_kst()
    out = []
    for n in ops.get("notices", []):
        try:
            show_from, end = notice_window(n)
        except (KeyError, ValueError):
            continue
        if show_from <= today <= end:
            out.append(n)
    return out


def _fmt(d: date) -> str:
    return f"{d.year}년 {d.month}월 {d.day}일"


# =====================================================================
# 답변 반영: 시스템 프롬프트에 붙일 운영 정보
# =====================================================================
def ops_context(ops: dict, today: date = None) -> str:
    today = today or today_kst()
    ex = ops["expiry"]
    lines = [
        "",
        "[운영 정보 — 판매자가 프로그램에서 직접 관리하는 최신 값. 검색된 지식 문서·과거 문의 이력과 다르면 이 값을 따른다]",
        f"- 오늘 날짜: {_fmt(today)}",
        f"- 소비기한: {ex.get('shelf_life', '')} / 현재 판매 중인 재고의 소비기한: {_fmt(parse_date(ex['date']))}까지 "
        f"(운영 정보 수정일 {ex.get('updated', '')})",
        f"- 제품 규격(운영 정보 수정일 {ops.get('products_updated', '')}):",
    ]
    for p in ops.get("products", []):
        extra = f", {p['note']}" if p.get("note") else ""
        lines.append(f"  · {p['name']}: {p['size']}, 길이 {p['length']}, 흡수량 표시 {p['absorb']}, {p['count']}{extra}")
    act = active_notices(ops, today)
    if act:
        lines.append("- 현재 적용 중인 배송 공지(배송·주문·도착·취소 관련 문의에는 아래 내용을 답변 초안에 반드시 포함):")
        for n in act:
            lines.append(f"  · {n['title']} (명절 기간 {n['start']} ~ {n['end']})")
            for key in ["order_cutoff", "office_closed", "delivery_closed", "notes"]:
                if n.get(key):
                    lines.append(f"    - {NOTICE_LABELS[key]}: {n[key]}")
    lines.append("- 운영 정보를 근거로 쓰면 근거 절에 [운영 정보] 또는 [배송 공지]로 표기한다.")
    return "\n".join(lines)


# =====================================================================
# 알림: (id, 수준, 메시지) — 수준 info/warn/notice
# =====================================================================
def compute_alerts(ops: dict, today: date = None, include_acked: bool = False):
    today = today or today_kst()
    cfg = ops.get("alert", {})
    alerts = []
    ex = ops["expiry"]

    # 1) 재고 소비기한이 가까움
    try:
        exp = parse_date(ex["date"])
        left = (exp - today).days
        if left < 0:
            alerts.append((f"expiry-passed-{ex['date']}", "warn",
                           f"재고 소비기한({ex['date']})이 지났습니다. 새 재고의 소비기한으로 바꿔 주세요."))
        elif left <= int(cfg.get("expiry_months", 6)) * 30:
            alerts.append((f"expiry-near-{ex['date']}", "warn",
                           f"재고 소비기한({ex['date']})이 {left}일 남았습니다. 새 재고가 들어오면 소비기한을 바꿔 주세요."))
    except (KeyError, ValueError):
        alerts.append(("expiry-invalid", "warn", "재고 소비기한 날짜 형식이 올바르지 않습니다."))

    # 2) 소비기한 정보를 오래 확인하지 않음
    try:
        upd = parse_date(ex.get("updated", ""))
        days = (today - upd).days
        if days >= int(cfg.get("review_days", 90)):
            alerts.append((f"expiry-review-{ex.get('updated')}", "info",
                           f"소비기한 정보를 {days}일째 확인하지 않았습니다. 현재 재고와 맞는지 확인하세요."))
    except ValueError:
        pass

    # 3) 명절이 다가오는데 배송 공지가 없음 / 공지 적용 중
    act = active_notices(ops, today)
    for n in act:
        alerts.append((f"notice-active-{n['title']}", "notice",
                       f"📢 {n['title']} 적용 중 ({n['start']} ~ {n['end']}) — 배송 문의 답변에 자동 포함됩니다."))
    remind = int(cfg.get("holiday_remind_days", 14))
    for h in HOLIDAYS:
        hs, he = parse_date(h["start"]), parse_date(h["end"])
        if not (today <= he and (hs - today).days <= remind):
            continue
        covered = False
        for n in ops.get("notices", []):
            try:
                ns, ne = parse_date(n["start"]), parse_date(n["end"])
            except (KeyError, ValueError):
                continue
            if ns <= he and hs <= ne:
                covered = True
        if not covered:
            d = (hs - today).days
            when = f"{d}일 후" if d > 0 else "진행 중"
            alerts.append((f"holiday-{h['name']}", "warn",
                           f"{h['name']}({h['start']} ~ {h['end']})가 {when}입니다. [운영 설정 → 배송 공지]에 택배 마감일·휴무일을 등록하세요."))

    # 4) 상품 정보를 최근 수정함 → 모범 답변 점검 권유
    try:
        pu = parse_date(ops.get("products_edited_at", ""))
        if 0 <= (today - pu).days <= 3:
            alerts.append((f"products-updated-{ops['products_edited_at']}", "info",
                           "상품 정보가 최근 수정되었습니다. 예전 문의 이력의 모범 답변과 다르면 답변 초안을 꼭 검토하세요."))
    except ValueError:
        pass

    if not include_acked:
        acks = set(ops.get("acks", []))
        alerts = [a for a in alerts if a[0] not in acks]
    return alerts


def dumps(ops: dict) -> str:
    return json.dumps(ops, ensure_ascii=False)
