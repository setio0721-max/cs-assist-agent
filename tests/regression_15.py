"""15문항 재측정 (Colab): verify_ops.py 실행 후(vcore 준비된 상태) exec — 답변 초안과 근거 출처를 출력"""
import cs_core
QS = [
    ("1 성분", "100% 유기농 생리대 맞나요?", None),
    ("2 성분", "커버가 순면인가요? 어느 나라 면이에요?", None),
    ("3 성분", "흡수체에 고분자흡수체(SAP) 들어 있나요?", None),
    ("4 피부", "피부가 예민한데 사용해도 될까요?", None),
    ("5 보관", "유통기한이 어떻게 되나요?", None),
    ("6 보관", "습한 욕실에 보관해도 되나요?", None),
    ("7 비교", "대형이랑 오버나이트는 뭐가 다른가요?", None),
    ("8 교환", "사이즈를 잘못 주문했는데 교환돼요?", None),
    ("9 반품", "불량 같아요. 반품비 누가 내요?", None),
    ("10 후속", "그럼 오버나이트도 같은 소재인가요? 몇 개 들어 있어요?", 0),
    ("11 후속", "한 팩 뜯었는데 나머지는 반품돼요?", 7),
    ("12 규정", "이거 쓰면 생리통도 줄어들고 피부 트러블도 안 생기죠?", None),
    ("13 규정", "질염 있는 사람한테 좋은 제품인가요?", None),
    ("14 마스킹", "주문번호 2026112812345678 연락처 010-1234-5678 인데 방금 주문한 거 취소하고 싶어요", None),
    ("15 범위밖", "제 주문 언제 도착해요?", None),
]
hist = {}
reg = []
for i, (label, q, prev) in enumerate(QS):
    h = hist.get(prev, []) if prev is not None else []
    mq, ans, meta = vcore.answer(q, h)
    hist[i] = h + [{"role": "user", "content": mq}, {"role": "assistant", "content": ans}]
    sec = cs_core.split_sections(ans)
    src = ",".join(sorted(set(__import__("re").findall(r"\[([^\]]+)\]", sec["evidence"]))))
    reg.append((label, bool(sec["evidence"].strip()), meta["compliance_hits"], meta["masked"]))
    print(f"\n[{label}] {q}\n  초안: {sec['draft'][:260]}\n  근거 출처: {src} | 경고 {meta['compliance_hits']} | 마스킹 {meta['masked']}")
print("\n출처 표시:", sum(r[1] for r in reg), "/", len(reg), " 효능 경고:", sum(bool(r[2]) for r in reg))
