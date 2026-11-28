"""운영 설정 기능 검증 시나리오 (Colab에서 실행)
Colab Secrets로 settings를 만든 뒤 실행: 운영 정보를 바꾸고 답변에 반영되는지 확인하고, 끝나면 기본값으로 되돌린다.
사용: settings(dict)와 (선택) embed_fn이 정의된 상태에서 exec
"""
import importlib, sys, requests, re
BASE = f"https://raw.githubusercontent.com/setio0721-max/cs-assist-agent/{globals().get('REF', 'main')}/"
for name in ["cs_ops", "cs_core"]:
    open(f"{name}.py", "w", encoding="utf-8").write(requests.get(BASE + f"{name}.py", timeout=20).text)
sys.path.insert(0, ".")
for name in ["cs_ops", "cs_core"]:
    if name in sys.modules:
        importlib.reload(sys.modules[name])
import cs_ops, cs_core

if "settings" not in globals():          # Colab Secrets에서 읽기
    from google.colab import userdata
    def _get(k):
        try:
            return (userdata.get(k) or "").strip()
        except Exception:
            return ""
    settings = {k: _get(k) for k in cs_core.REQUIRED_SETTINGS}
if not (settings.get("CF_ACCOUNT_ID") and settings.get("CF_API_TOKEN")) and not globals().get("embed_fn"):
    settings["CF_ACCOUNT_ID"] = settings["CF_API_TOKEN"] = "local"
    from sentence_transformers import SentenceTransformer
    _m = SentenceTransformer("BAAI/bge-m3")
    embed_fn = lambda text: _m.encode([text], normalize_embeddings=True)[0].tolist()

vcore = cs_core.CSAssistant(settings)
if globals().get("embed_fn"):
    vcore.embed_query = embed_fn
print("연결:", vcore.connect())

def ask(q):
    _, ans, meta = vcore.answer(q, [])
    sec = cs_core.split_sections(ans)
    print(f"\n👤 {q}\n📝 {sec['draft']}\n📎 {sec['evidence'][:400]}\n   meta notices={meta['notices']} 경고={meta['compliance_hits']}")
    return sec

results = []
def check(label, sec, pattern):
    ok = bool(re.search(pattern, sec["draft"]))
    results.append((label, ok))
    print(("✅" if ok else "❌"), label)

base = cs_ops.merge_defaults({})
vcore.save_ops(base)
print("기본 운영 정보 저장, 알림:", cs_ops.compute_alerts(vcore.load_ops()))

# 1) 기본값
check("기본 소비기한 2028.12.29", ask("유통기한이 언제까지예요?"), r"2028")
# 2) 소비기한 변경
ops = vcore.load_ops(); ops["expiry"]["date"] = "2029-06-30"; ops["expiry"]["updated"] = cs_ops.today_kst().isoformat(); vcore.save_ops(ops)
check("변경 소비기한 2029.6.30 반영", ask("유통기한이 언제까지예요?"), r"2029")
# 3) 상품 정보 변경(중형 16 → 18개입)
ops = vcore.load_ops(); ops["products"][1]["count"] = "18개입"; vcore.save_ops(ops)
check("중형 포장 수량 18개입 반영", ask("중형은 한 팩에 몇 개 들어 있어요?"), r"18")
# 4) 배송 공지(오늘 적용되도록 테스트 공지)
t = cs_ops.today_kst()
ops = vcore.load_ops(); ops["notices"] = [{"title": "테스트 연휴 배송 안내", "start": (t + cs_ops.timedelta(days=3)).isoformat(),
    "end": (t + cs_ops.timedelta(days=5)).isoformat(), "lead_days": 7, "order_cutoff": "연휴 2일 전 낮 11시 결제 건까지 연휴 전 출고",
    "office_closed": "연휴 기간 전체", "delivery_closed": "연휴 기간 택배 집하·배송 중단", "notes": "연휴 직전 물량 증가로 1~2일 지연 가능"}]
vcore.save_ops(ops)
print("알림:", cs_ops.compute_alerts(vcore.load_ops()))
check("배송 공지 자동 포함(배송 문의)", ask("지금 주문하면 언제쯤 받을 수 있나요?"), r"마감|휴무|연휴")
check("배송 무관 문의엔 공지 미포함", ask("커버가 순면인가요?"), r"^(?![\s\S]*(연휴|휴무))")

vcore.save_ops(cs_ops.merge_defaults({}))
print("\n기본값으로 복원 완료")
print("결과:", sum(ok for _, ok in results), "/", len(results))
for label, ok in results:
    print(" ", "✅" if ok else "❌", label)
