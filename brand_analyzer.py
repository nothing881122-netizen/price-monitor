#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
월간 브랜드 트렌드 분석기

각 키워드에서 알구몬에 자주 등장하는 브랜드를 찾아 sub_brands 갱신 제안.
매월 1일 자동 실행 → 결과 HTML 리포트 + PWA 알림.

- 현재 sub_brands 빈도 + 평균 단가
- 잠재 신규 브랜드 후보 (사전 + 자동 발견)
- 변경 권장 사항
"""

from __future__ import annotations
import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

# price_monitor 와 같은 폴더에서 import
sys.path.insert(0, str(Path(__file__).parent))
from price_monitor import (
    HEADERS, ALGUMON_DATA_URL, _resolve_ref, _build_deal,
    load_keywords, deploy_to_github, CONFIG_FILE,
)

try:
    from push_notify import send_push
except ImportError:
    send_push = None

REPORT_FILE = Path(__file__).parent / "brand-analysis.html"

# 카테고리별 알려진 브랜드 사전 (한·영)
BRAND_DICT: dict[str, list[tuple[str, list[str]]]] = {
    "tuna":    [("동원", ["동원"]), ("사조", ["사조"]), ("한성", ["한성"]),
                ("오뚜기", ["오뚜기"]), ("CJ", ["cj "])],
    "granola": [("켈로그", ["켈로그", "kellogg"]), ("동서", ["동서"]),
                ("포스트", ["포스트", "post "]), ("산과들에", ["산과들에"]),
                ("뮤즐리", ["뮤즐리"]), ("닥터엘릭서", ["닥터엘릭서"])],
    "spam":    [("CJ", ["cj ", "스팸"])],
}

# 단어 빈도 추출 — 일반 단어 외에 브랜드일 가능성 있는 토큰
_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]{2,}")
_STOPWORDS = {
    "그래놀라", "참치", "스팸", "프로틴", "쌀", "10kg", "쇼핑", "할인", "특가", "쿠팡",
    "g마켓", "옥션", "11번가", "톡딜", "롯데온", "네이버", "쇼핑", "무료", "무배",
    "박스", "묶음", "세트", "골라담기", "기획", "단일", "단위", "1팩", "2팩", "3팩",
    "당일", "도정", "햅쌀", "햅쌀", "고시히카리", "버즈4", "프로", "갤럭시",
    "마스터", "master", "mx", "포러너", "forerunner",
}


def fetch_deals_for_keyword(kw: dict) -> list[dict]:
    """키워드의 search_queries 로 알구몬 검색 (sub_brands 있어도 부모 키워드 검색 기준 — 트렌드 보기)."""
    queries = kw.get("search_queries") or [kw.get("name", kw["id"])]
    # sub_brands 있는 키워드는 일반 키워드 (예: "참치", "그래놀라") 로 다시 검색
    if kw.get("sub_brands"):
        # 키워드 이름의 핵심 단어로 검색 (예: 참치캔 → 참치, 그래놀라 → 그래놀라)
        name = kw.get("name", kw["id"])
        # "캔", "(10kg)" 같은 부속 제거
        base = re.sub(r"\(.*?\)|캔|10kg|10 kg", "", name).strip()
        queries = [base]
    all_deals = []
    seen_ids = set()
    for q in queries:
        try:
            url = ALGUMON_DATA_URL.format(kw=urllib.parse.quote(q.encode("utf-8")))
            data = json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=HEADERS), timeout=15
            ).read())
            pool = data.get("nodes", [{}, {}])[1].get("data", [])
        except Exception as e:
            print(f"  [WARN] '{q}' 실패: {e}", flush=True)
            continue
        if not isinstance(pool, list):
            continue
        for item in pool:
            if not isinstance(item, dict): continue
            if not all(k in item for k in ("storeName", "price", "title")): continue
            resolved = {k: (_resolve_ref(v, pool) if isinstance(v, int) else v) for k, v in item.items()}
            info = _build_deal(resolved)
            if info and not info.get("ended") and info["id"] not in seen_ids:
                seen_ids.add(info["id"])
                all_deals.append(info)
    return all_deals


def analyze_keyword(kw: dict) -> dict:
    """단일 키워드 분석 — 현재 sub_brands 빈도 + 새 후보 발견."""
    deals = fetch_deals_for_keyword(kw)
    kid = kw["id"]
    dict_brands = BRAND_DICT.get(kid, [])
    # 현재 sub_brands 에 등록된 것 (set)
    current_sub_ids = {sb.get("id", "").lower() for sb in kw.get("sub_brands", [])}
    current_sub_names = {sb.get("name", "").lower() for sb in kw.get("sub_brands", [])}

    # 사전 브랜드 카운트
    dict_count: Counter = Counter()
    dict_price: dict[str, list[int]] = {b: [] for b, _ in dict_brands}
    for d in deals:
        title = d["title"].lower()
        for brand, patterns in dict_brands:
            if any(p in title for p in patterns):
                dict_count[brand] += 1
                if d.get("price_per_unit"):
                    dict_price[brand].append(d["price_per_unit"])
                break

    # 미매칭 게시물에서 토큰 빈도 — 잠재 신규 브랜드 후보
    dict_pat_all = [p for _, ps in dict_brands for p in ps]
    unmatched = [d for d in deals if not any(p in d["title"].lower() for p in dict_pat_all)]
    token_count: Counter = Counter()
    for d in unmatched:
        for token in _TOKEN_RE.findall(d["title"]):
            t = token.lower()
            if len(t) >= 2 and t not in _STOPWORDS:
                token_count[t] += 1

    return {
        "id": kid,
        "name": kw.get("name", kid),
        "emoji": kw.get("emoji", ""),
        "total_deals": len(deals),
        "dict_brand_count": dict_count,
        "dict_brand_avg": {b: int(sum(ps)/len(ps)) if ps else 0
                           for b, ps in dict_price.items()},
        "current_sub_ids": current_sub_ids,
        "current_sub_names": current_sub_names,
        "potential_tokens": token_count.most_common(15),
    }


def render_report(analyses: list[dict], checked_at: str) -> str:
    sections = []
    for a in analyses:
        rows = ""
        for brand, count in a["dict_brand_count"].most_common():
            avg = a["dict_brand_avg"].get(brand, 0)
            avg_s = f"{avg:,}원/단위" if avg else "—"
            in_sub = "✅" if brand.lower() in a["current_sub_names"] else "⚪"
            rec = ""
            if brand.lower() not in a["current_sub_names"] and count >= 3:
                rec = ' <span class="rec">+ 추가 권장</span>'
            elif brand.lower() in a["current_sub_names"] and count < 2:
                rec = ' <span class="warn">↓ 활동 약함</span>'
            rows += f"""<tr>
  <td>{in_sub}</td><td>{brand}</td>
  <td class="num">{count}</td><td class="num">{avg_s}</td>
  <td>{rec}</td>
</tr>"""

        tokens_html = ""
        if a["potential_tokens"]:
            top = [f'<span class="token">{t} <em>{c}</em></span>'
                   for t, c in a["potential_tokens"][:10]]
            tokens_html = f'<p class="tokens"><b>미매칭 자주 등장 토큰</b>: {" ".join(top)}</p>'

        sections.append(f"""<section class="kw">
  <h2>{a['emoji']} {a['name']} <span class="count">{a['total_deals']}건</span></h2>
  <table>
    <thead><tr><th></th><th>브랜드</th><th>빈도</th><th>평균 단가</th><th>제안</th></tr></thead>
    <tbody>{rows or '<tr><td colspan=5 class="note">사전 등록 브랜드 없음</td></tr>'}</tbody>
  </table>
  {tokens_html}
</section>""")

    css = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#FAF7F2;color:#2C1810;padding-bottom:48px}
.header{background:linear-gradient(135deg,#D0663C,#B0502C);color:white;padding:48px 20px 24px;text-align:center}
.header h1{font-size:22px;font-weight:700;margin-bottom:6px}
.subtitle{font-size:13px;opacity:.85}
.main{max-width:680px;margin:0 auto;padding:16px}
.intro{background:#FAE2D4;border-left:3px solid #D0663C;border-radius:8px;padding:10px 12px;font-size:13px;color:#7C4530;margin-bottom:14px;line-height:1.5}
.kw{background:white;border-radius:16px;padding:18px;margin-bottom:14px;box-shadow:0 1px 4px rgba(140,70,20,.08)}
.kw h2{font-size:18px;font-weight:700;margin-bottom:10px}
.kw .count{font-size:13px;color:#8B7355;font-weight:500;margin-left:6px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:8px}
th{background:#FAE2D4;color:#7C4530;padding:6px 8px;text-align:left;font-size:12px}
td{padding:6px 8px;border-top:1px solid #FAF0E8}
.num{text-align:right;font-variant-numeric:tabular-nums}
.note{color:#8B7355;text-align:center;font-style:italic}
.rec{color:#B0502C;font-weight:700;font-size:12px}
.warn{color:#A89070;font-style:italic;font-size:12px}
.tokens{font-size:12px;color:#8B7355;background:#FAF0E8;border-radius:8px;padding:8px 10px;margin-top:6px}
.token{display:inline-block;margin:2px 6px 2px 0}
.token em{color:#B0502C;font-weight:700;font-style:normal}
.footer{text-align:center;padding:24px 16px;font-size:12px;color:#B8986A}
"""
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>브랜드 트렌드 분석</title>
<style>{css}</style>
</head>
<body>
<div class="header">
  <h1>📈 브랜드 트렌드 분석</h1>
  <p class="subtitle">{checked_at} · 월간 분석 리포트</p>
</div>
<div class="main">
  <p class="intro">현재 등록된 sub_brand 의 빈도/평균 단가 + 잠재 신규 브랜드 후보를 보여드립니다. <b>+ 추가 권장</b> 표시된 브랜드는 keywords.json 에 sub_brand 로 등록 검토하세요.</p>
  {"".join(sections)}
</div>
<div class="footer">매월 1일 자동 분석 · 알구몬 기반</div>
</body>
</html>"""


def main():
    cfg = load_keywords()
    keywords = [k for k in cfg.get("keywords", []) if k.get("default", True)]

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 브랜드 분석 시작 — {len(keywords)} 키워드", flush=True)
    analyses = []
    for kw in keywords:
        if not (kw.get("sub_brands") or kw["id"] in BRAND_DICT):
            continue  # 분석 대상 아님
        print(f"\n[{kw['id']}] {kw.get('name', kw['id'])} ...", flush=True)
        a = analyze_keyword(kw)
        analyses.append(a)
        for brand, count in a["dict_brand_count"].most_common():
            print(f"  {brand:12s} {count:3d}건", flush=True)

    if not analyses:
        print("[INFO] 분석 대상 키워드 없음")
        return

    html = render_report(analyses, datetime.now().strftime("%Y-%m-%d %H:%M"))
    REPORT_FILE.write_text(html, encoding="utf-8")
    print(f"\n[HTML] {REPORT_FILE}", flush=True)

    # GitHub 배포
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    gh_token = os.environ.get("GH_TOKEN") or config.get("github_token", "")
    gh_owner = os.environ.get("GH_OWNER") or config.get("github_owner", "")
    gh_repo  = os.environ.get("GH_REPO")  or config.get("github_repo", "")
    if gh_token and gh_owner and gh_repo:
        try:
            url = deploy_to_github(html, gh_token, gh_owner, gh_repo, "brand-analysis.html")
            print(f"[URL] {url}", flush=True)
        except Exception as e:
            print(f"[ERROR] GitHub 배포 실패: {e}", flush=True)

    # PWA 알림 — 첫번째 활성 토픽으로 (사용자가 보기만 하면 됨)
    if send_push:
        msg = f"이번 달 sub_brand 추가 권장 / 활동 약화 브랜드 확인하세요"
        report_url = f"https://{gh_owner}.github.io/{gh_repo}/brand-analysis.html"
        try:
            send_push("spam", "📈 월간 브랜드 트렌드", msg, url=report_url,
                      actions=[{"action": "view", "title": "📋 리포트", "url": report_url}])
        except Exception as e:
            print(f"[WARN] push 실패: {e}")

    print(f"\n[완료]  ({datetime.now().strftime('%H:%M:%S')})", flush=True)


if __name__ == "__main__":
    main()
