#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
뽐뿌 스팸/참치 핫딜 모니터
- 뽐뿌 1, 2페이지에서 참치/스팸 게시물 감지
- pricewagon 기준 대박급 이하 가격 시 ntfy 알림 + GitHub Pages 리포트
- 2시간마다 실행 (09:00 ~ 20:00)
"""

import base64
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, date
from html.parser import HTMLParser
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "config.json"
SEEN_FILE   = Path(__file__).parent / "seen_ids.json"
REPORT_FILE = Path(__file__).parent / "deals.html"

# ─────────────────────────────────────────────
# 가격 기준 (pricewagon.net 기준)
# ─────────────────────────────────────────────

PRODUCTS = {
    "참치": {
        "keywords": ["참치", "동원", "사조"],
        "unit_g":   150,
        "역대급": 1500,
        "대박급": 1650,
        "중박급": 1800,
        "soso":   2100,
    },
    "스팸": {
        "keywords": ["스팸", "spam"],
        "unit_g":   340,
        "역대급": 2890,
        "대박급": 3230,
        "중박급": 3740,
        "soso":   4250,
    },
}

ALERT_GRADES = {"역대급", "대박급"}  # 이 등급 이하만 알림

PPOMPPU_URL = "https://www.ppomppu.co.kr/zboard/zboard.php?id=ppomppu&page={page}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
}

# ─────────────────────────────────────────────
# HTML 파서
# ─────────────────────────────────────────────

class PpomppuParser(HTMLParser):
    """뽐뿌 게시판 목록 파서 — 제목 링크만 추출"""

    def __init__(self):
        super().__init__()
        self.posts: list[dict] = []
        self._capturing = False
        self._current: dict = {}
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attr = dict(attrs)
        href = attr.get("href", "")
        if "view.php" in href and "id=ppomppu" in href:
            m = re.search(r"no=(\d+)", href)
            if m:
                self._capturing = True
                self._buf = []
                self._current = {
                    "id":  m.group(1),
                    "url": "https://www.ppomppu.co.kr/zboard/" + href,
                }

    def handle_endtag(self, tag):
        if tag == "a" and self._capturing:
            self._capturing = False
            text = "".join(self._buf).strip()
            if text:
                self._current["title"] = text
                self.posts.append(self._current)
            self._current = {}
            self._buf = []

    def handle_data(self, data):
        if self._capturing:
            self._buf.append(data)


def fetch_posts(page: int) -> list[dict]:
    url = PPOMPPU_URL.format(page=page)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"  [ERROR] 페이지 {page} 로드 실패: {e}", flush=True)
        return []

    html = raw.decode("euc-kr", errors="replace")
    parser = PpomppuParser()
    parser.feed(html)
    return parser.posts


# ─────────────────────────────────────────────
# 가격 / 수량 파싱
# ─────────────────────────────────────────────

def parse_total_price(title: str) -> int | None:
    """제목에서 총 가격 추출. '(17,900/무료)' 또는 '17,900원' 패턴"""
    # (가격/배송) 패턴 우선
    m = re.search(r"\(\s*([\d,]+)\s*/", title)
    if m:
        return int(m.group(1).replace(",", ""))
    # 원 단위 숫자 — 마지막(총가격)
    prices = [int(x.replace(",", "")) for x in re.findall(r"([\d,]+)원", title)]
    if prices:
        return prices[-1]
    return None


def parse_quantity(title: str, product: str) -> int | None:
    """제목에서 수량(캔 기준) 추출"""
    # 박스 단위 → 캔 수로 변환 (동원참치 24캔/박스, 스팸 6캔/박스 기본)
    box_qty = {"참치": 24, "스팸": 6}

    patterns = [
        (r"(\d+)\s*캔",          1),
        (r"(\d+)\s*개",          1),
        (r"[x×\*]\s*(\d+)",      1),
        (r"(\d+)\s*[xX×]",       1),
        (r"(\d+)\s*(?:box|박스)", box_qty.get(product, 1)),
    ]
    for pat, multiplier in patterns:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            qty = int(m.group(1)) * multiplier
            if 1 < qty <= 500:
                return qty
    return None


def get_grade(price_per_unit: int, product: str) -> str:
    t = PRODUCTS[product]
    if price_per_unit <= t["역대급"]: return "역대급"
    if price_per_unit <= t["대박급"]: return "대박급"
    if price_per_unit <= t["중박급"]: return "중박급"
    if price_per_unit <= t["soso"]:   return "soso"
    return "none"


# ─────────────────────────────────────────────
# 게시물 분류
# ─────────────────────────────────────────────

def classify_post(post: dict) -> dict | None:
    title_lower = post["title"].lower()

    for product, info in PRODUCTS.items():
        if not any(k.lower() in title_lower for k in info["keywords"]):
            continue

        total_price = parse_total_price(post["title"])
        quantity    = parse_quantity(post["title"], product)

        result = {**post, "product": product}

        if total_price and quantity:
            ppu = total_price // quantity
            result.update({
                "total_price":    total_price,
                "quantity":       quantity,
                "price_per_unit": ppu,
                "grade":          get_grade(ppu, product),
            })
        else:
            result.update({
                "total_price":    total_price,
                "quantity":       quantity,
                "price_per_unit": None,
                "grade":          "unknown",
            })
        return result

    return None


# ─────────────────────────────────────────────
# 중복 방지
# ─────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(seen: set) -> None:
    ids = sorted(seen)[-1000:]
    SEEN_FILE.write_text(json.dumps(ids), encoding="utf-8")


# ─────────────────────────────────────────────
# HTML 리포트
# ─────────────────────────────────────────────

GRADE_COLOR = {
    "역대급": ("#D0663C", "#FAE2D4"),
    "대박급": ("#B0502C", "#F5DDD0"),
    "중박급": ("#8B7355", "#F5EDE0"),
    "soso":   ("#A89070", "#FAF0E8"),
    "unknown":("#9AA0A6", "#F1F3F4"),
    "none":   ("#9AA0A6", "#F1F3F4"),
}

GRADE_EMOJI = {
    "역대급": "🥇", "대박급": "🥈", "중박급": "🥉",
    "soso": "😐", "unknown": "❓", "none": "—",
}


def deal_card_html(d: dict) -> str:
    fg, bg = GRADE_COLOR.get(d["grade"], ("#9AA0A6", "#F1F3F4"))
    emoji  = GRADE_EMOJI.get(d["grade"], "")
    grade_label = f"{emoji} {d['grade']}" if d["grade"] not in ("unknown","none") else emoji

    if d["price_per_unit"]:
        t = PRODUCTS[d["product"]]
        threshold_str = f"대박급 기준 {t['대박급']:,}원/{t['unit_g']}g"
        price_html = f"""
      <div class="price-line">
        <span class="price">{d['price_per_unit']:,}원</span>
        <span class="discount" style="background:{bg};color:{fg}">{grade_label}</span>
      </div>
      <p class="saving">총 {d['total_price']:,}원 / {d['quantity']}캔 · {threshold_str}</p>"""
    else:
        price_html = f"""
      <div class="price-line">
        <span class="price-unknown">가격 파싱 불가</span>
      </div>
      <p class="saving">제목을 직접 확인하세요</p>"""

    return f"""<div class="card deal-card" style="border-left-color:{fg}">
      <p class="card-route">{d['product']} · 뽐뿌</p>
      <p class="card-title">{d['title']}</p>
      {price_html}
      <a href="{d['url']}" class="btn-book" style="background:{fg}" target="_blank">뽐뿌 게시물 보기 →</a>
    </div>"""


def generate_html(all_deals: list[dict], checked_at: str) -> str:
    alert_deals = [d for d in all_deals if d["grade"] in ALERT_GRADES]
    other_deals = [d for d in all_deals if d["grade"] not in ALERT_GRADES and d["grade"] != "none"]

    if alert_deals:
        top_html = f"""<section class="section">
    <h2 class="section-title">🔥 대박급 이상 특가 <span class="badge badge-deal">{len(alert_deals)}건</span></h2>
    {"".join(deal_card_html(d) for d in alert_deals)}
  </section>"""
    else:
        top_html = """<section class="section">
    <div class="card no-deal-card">
      <p class="no-deal-emoji">🛒</p>
      <p class="no-deal-text">지금은 대박급 이상 없음</p>
      <p class="no-deal-sub">2시간마다 자동 확인 중</p>
    </div>
  </section>"""

    other_html = ""
    if other_deals:
        other_html = f"""<section class="section">
    <h2 class="section-title">📋 기타 발견 ({len(other_deals)}건)</h2>
    {"".join(deal_card_html(d) for d in other_deals)}
  </section>"""

    ref_rows = ""
    for pname, info in PRODUCTS.items():
        ref_rows += f"""<tr>
        <td>{pname} ({info['unit_g']}g)</td>
        <td class="grade-col" style="color:#D0663C">🥇 {info['역대급']:,}원</td>
        <td class="grade-col" style="color:#B0502C">🥈 {info['대박급']:,}원</td>
        <td class="grade-col" style="color:#8B7355">🥉 {info['중박급']:,}원</td>
      </tr>"""

    css = """
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#FAF7F2;color:#2C1810;min-height:100vh}
    .header{background:linear-gradient(135deg,#D0663C 0%,#B0502C 100%);color:white;padding:52px 20px 28px;text-align:center}
    .header h1{font-size:22px;font-weight:700;margin-bottom:6px}
    .subtitle{font-size:13px;opacity:.85;margin-bottom:16px}
    .chips{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
    .chip{background:rgba(255,255,255,.2);border-radius:20px;padding:5px 14px;font-size:13px;font-weight:500}
    .chip-deal{background:rgba(255,210,120,.3)}
    .main{max-width:600px;margin:0 auto;padding:0 0 48px}
    .section{padding:20px 16px 4px}
    .section-title{font-size:17px;font-weight:700;margin-bottom:14px;display:flex;align-items:center;gap:8px}
    .badge{border-radius:12px;padding:2px 10px;font-size:13px;font-weight:600}
    .badge-deal{background:#F8D8C4;color:#6B3828}
    .card{background:white;border-radius:16px;padding:16px;margin-bottom:12px;box-shadow:0 1px 4px rgba(140,70,20,.1)}
    .deal-card{border-left:4px solid #D0663C}
    .card-route{font-size:12px;color:#8B7355;margin-bottom:4px}
    .card-title{font-size:14px;font-weight:600;color:#2C1810;margin-bottom:10px;line-height:1.4}
    .price-line{display:flex;align-items:center;gap:12px;margin-bottom:6px}
    .price{font-size:28px;font-weight:700;color:#2C1810}
    .price-unknown{font-size:16px;color:#8B7355}
    .discount{border-radius:10px;padding:4px 12px;font-size:14px;font-weight:700}
    .saving{font-size:12px;color:#8B7355;margin-bottom:14px}
    .btn-book{display:block;color:white;text-align:center;text-decoration:none;padding:12px 16px;border-radius:12px;font-size:14px;font-weight:600}
    .no-deal-card{text-align:center;padding:32px 16px}
    .no-deal-emoji{font-size:40px;margin-bottom:8px}
    .no-deal-text{font-size:17px;font-weight:600;margin-bottom:4px}
    .no-deal-sub{font-size:14px;color:#8B7355}
    .ref-section{padding:4px 16px 20px}
    .ref-title{font-size:17px;font-weight:700;margin-bottom:12px}
    .ref-table{width:100%;background:white;border-radius:16px;overflow:hidden;box-shadow:0 1px 4px rgba(140,70,20,.08);border-collapse:collapse}
    .ref-table th{background:#FAE2D4;color:#7C4530;font-size:13px;padding:10px 12px;text-align:center}
    .ref-table td{font-size:13px;padding:10px 12px;border-top:1px solid #FAF0E8;color:#2C1810}
    .grade-col{text-align:center;font-weight:600}
    .footer{text-align:center;padding:20px;font-size:12px;color:#B8986A}
    """

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="핫딜 알림">
  <meta name="theme-color" content="#D0663C">
  <title>참치/스팸 핫딜 모니터</title>
  <style>{css}</style>
</head>
<body>
  <div class="header">
    <h1>🥫 참치 / 스팸 핫딜</h1>
    <p class="subtitle">최근 확인 {checked_at}</p>
    <div class="chips">
      <span class="chip">뽐뿌 1·2페이지</span>
      <span class="chip chip-deal">대박급 이상 {len(alert_deals)}건</span>
      <span class="chip">2시간마다 자동</span>
    </div>
  </div>
  <div class="main">
    {top_html}
    {other_html}
    <div class="ref-section">
      <h2 class="ref-title">📊 pricewagon 기준가</h2>
      <table class="ref-table">
        <tr>
          <th>상품</th><th>🥇 역대급</th><th>🥈 대박급</th><th>🥉 중박급</th>
        </tr>
        {ref_rows}
      </table>
    </div>
  </div>
  <div class="footer">뽐뿌 핫딜 자동 모니터 · pricewagon.net 기준</div>
</body>
</html>"""


# ─────────────────────────────────────────────
# GitHub Pages 배포
# ─────────────────────────────────────────────

def deploy_to_github(html: str, token: str, owner: str, repo: str, path: str) -> str:
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    sha = None
    try:
        req = urllib.request.Request(api_url, headers=headers)
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        sha = resp.get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    payload: dict = {
        "message": f"deals update {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "content": base64.b64encode(html.encode("utf-8")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha

    req = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="PUT",
    )
    urllib.request.urlopen(req, timeout=30)
    page_path = path.replace("index.html", "").rstrip("/")
    return f"https://{owner}.github.io/{repo}/{page_path}"


# ─────────────────────────────────────────────
# ntfy 알림
# ─────────────────────────────────────────────

def send_ntfy(topic: str, deals: list[dict], report_url: str = "") -> None:
    lines = [f"참치/스팸 대박급 {len(deals)}건 발견!\n"]
    for d in deals:
        if d["price_per_unit"]:
            lines.append(
                f"[{d['grade']}] {d['product']} {d['price_per_unit']:,}원/캔\n"
                f"  {d['title'][:40]}\n"
            )
    if report_url:
        lines.append(f"\n리포트: {report_url}")

    actions = []
    if report_url:
        actions.append({"action": "view", "label": "리포트 보기", "url": report_url})

    payload = json.dumps({
        "topic":    topic,
        "title":    f"🥫 핫딜 {len(deals)}건 — 참치/스팸",
        "message":  "\n".join(lines),
        "tags":     ["canned_food"],
        "priority": 4,
        "click":    report_url or "https://www.ppomppu.co.kr/zboard/zboard.php?id=ppomppu",
        "actions":  actions,
    }, ensure_ascii=False).encode("utf-8")

    try:
        req = urllib.request.Request(
            "https://ntfy.sh",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        urllib.request.urlopen(req, timeout=10)
        print("  [OK] ntfy 알림 전송 완료", flush=True)
    except Exception as e:
        print(f"  [ERROR] ntfy 전송 실패: {e}", flush=True)


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────

def main() -> None:
    # 운영 시간 체크 (09:00 ~ 20:00) — FORCE_RUN=1 이면 무시 (수동 테스트용)
    now = datetime.now()
    force = os.environ.get("FORCE_RUN", "").lower() in ("1", "true", "yes")
    if not force and not (9 <= now.hour < 20):
        print(f"[SKIP] 운영 시간 외 ({now.strftime('%H:%M')}). 09:00~20:00에만 실행.", flush=True)
        sys.exit(0)

    config = {}
    if CONFIG_FILE.exists():
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    # 환경변수 우선 (GitHub Actions), 없으면 config.json 사용
    ntfy_topic = os.environ.get("NTFY_TOPIC") or config.get("ntfy_topic", "")
    gh_token   = os.environ.get("GH_TOKEN")   or config.get("github_token", "")
    gh_owner   = os.environ.get("GH_OWNER")   or config.get("github_owner", "")
    gh_repo    = os.environ.get("GH_REPO")    or config.get("github_repo", "")
    gh_path    = os.environ.get("GH_PATH")    or config.get("github_deals_path", "deals.html")

    print(f"\n[{now.strftime('%Y-%m-%d %H:%M')}] 뽐뿌 스캔 시작", flush=True)

    # 1. 게시물 수집 (1, 2페이지)
    all_posts: list[dict] = []
    for page in (1, 2):
        posts = fetch_posts(page)
        print(f"  페이지 {page}: {len(posts)}개 게시물", flush=True)
        all_posts.extend(posts)

    # 2. 참치/스팸 분류
    classified = [r for p in all_posts if (r := classify_post(p))]
    print(f"  참치/스팸 게시물: {len(classified)}개", flush=True)
    for d in classified:
        ppu = f"{d['price_per_unit']:,}원/캔" if d["price_per_unit"] else "가격미상"
        print(f"    [{d['grade']}] {d['product']} {ppu} — {d['title'][:50]}", flush=True)

    # 3. 중복 제거 — 이미 알림 보낸 ID 제외
    seen = load_seen()
    new_alert_deals = [
        d for d in classified
        if d["grade"] in ALERT_GRADES and d["id"] not in seen
    ]

    # 4. HTML 리포트 생성 (매번 갱신)
    checked_at = now.strftime("%Y-%m-%d %H:%M")
    html = generate_html(classified, checked_at)
    REPORT_FILE.write_text(html, encoding="utf-8")
    print(f"  [HTML] 리포트 저장: {REPORT_FILE}", flush=True)

    # 5. GitHub Pages 배포
    report_url = ""
    if gh_token and gh_owner and gh_repo:
        try:
            report_url = deploy_to_github(html, gh_token, gh_owner, gh_repo, gh_path)
            print(f"  [URL] {report_url}", flush=True)
        except Exception as e:
            print(f"  [ERROR] GitHub 배포 실패: {e}", flush=True)

    # 6. 새 대박급 딜 알림
    if new_alert_deals:
        print(f"  새 대박급 딜 {len(new_alert_deals)}건 발견 — 알림 전송", flush=True)
        if ntfy_topic:
            send_ntfy(ntfy_topic, new_alert_deals, report_url)
        # seen에 추가
        seen.update(d["id"] for d in new_alert_deals)
        save_seen(seen)
    else:
        print("  새 대박급 딜 없음", flush=True)

    print(f"[완료] {datetime.now().strftime('%H:%M:%S')}", flush=True)


if __name__ == "__main__":
    main()
