#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
category_scout.py — 신규 카테고리 후보 스카우터.

매일 아침 KST 07:17 GitHub Actions cron 으로 실행.
scout_candidates.json 의 후보 풀을 알구몬에서 스캔하고
"유명 브랜드의 유명 제품 + 평소 가격보다 낮은 특가 잡기" 컨셉 적합도를 점수화.
상위 N개를 scout.html 로 deploy + PWA 푸시 알림.

추가 결정은 사람이. 마음에 드는 후보는 keywords.json 에 직접 옮겨 등록.
"""

import base64
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# price_monitor.py 의 함수 재사용 (같은 폴더에 있음)
try:
    from price_monitor import fetch_search_multi, load_keywords, deploy_to_github
except ImportError:
    sys.exit("price_monitor.py 를 import 못 함 — 같은 폴더에서 실행하세요")

try:
    from push_notify import send_push
except ImportError:
    send_push = None


ROOT = Path(__file__).parent
CANDIDATES_FILE = ROOT / "scout_candidates.json"
CONFIG_FILE     = ROOT / "config.json"
SCOUT_HTML      = ROOT / "scout.html"
SCOUT_PAGE_URL  = "https://nothing881122-netizen.github.io/flight-deals/scout.html"


# ─────────────────────────────────────────────
# 점수화
# ─────────────────────────────────────────────

def score_candidate(c: dict, deals: list[dict]) -> dict | None:
    """점수 = N × B × D
        N = 게시물 수
        B = 유명 브랜드 매칭률 (제목에 known_brand 등장)
        D = 활성 핫딜 비율 (ended=False)
    min_posts 미만 → None.
    """
    min_posts = c.get("min_posts", 5)
    n = len(deals)
    if n < min_posts:
        return None

    brands = [b.lower() for b in c.get("known_brands", [])]
    brand_hits = 0
    for d in deals:
        title = (d.get("title") or "").lower()
        if any(b in title for b in brands):
            brand_hits += 1
    b_rate = brand_hits / n if n else 0

    active_n = sum(1 for d in deals if not d.get("ended", False))
    d_rate = active_n / n if n else 0

    # 점수 — 게시물 많음 × 브랜드 비중 × 활성 비율 × 10 (가독성 위해)
    score = round(n * b_rate * d_rate * 10, 1)

    # 대표 핫딜 — 활성 + 가격 있는 것 중 단가 낮은 순 3건
    representatives = [d for d in deals
                       if not d.get("ended", False) and d.get("price_per_unit")]
    representatives.sort(key=lambda d: d.get("price_per_unit") or 9_999_999)
    representatives = representatives[:3]

    return {
        "id": c["id"],
        "name": c["name"],
        "emoji": c["emoji"],
        "category": c["category"],
        "score": score,
        "n": n,
        "brand_hits": brand_hits,
        "b_rate": round(b_rate, 2),
        "active_n": active_n,
        "d_rate": round(d_rate, 2),
        "known_brands": c.get("known_brands", []),
        "representatives": representatives,
    }


# ─────────────────────────────────────────────
# HTML 렌더 (다크 톤 — 개발/분석 페이지 컨셉)
# ─────────────────────────────────────────────

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI',Roboto,'JetBrains Mono',monospace;background:#1A1F2E;color:#D5DAE5;min-height:100vh;padding-bottom:48px}
.header{padding:32px 20px 16px;border-bottom:1px solid #2D3447}
.header h1{font-size:20px;font-weight:700;color:#FFF;letter-spacing:0.3px}
.header .subtitle{font-size:12px;color:#8B95A8;margin-top:6px;font-family:monospace}
.header .legend{font-size:11px;color:#6B7280;margin-top:8px}
.main{max-width:680px;margin:0 auto;padding:20px}
.empty{background:#252B3D;border:1px dashed #3A4258;border-radius:12px;padding:36px;text-align:center;color:#8B95A8}
.cand{background:#252B3D;border:1px solid #3A4258;border-radius:12px;padding:16px;margin-bottom:14px}
.cand.top{border-color:#4F8FD9;box-shadow:0 0 0 1px #4F8FD9 inset}
.cand-head{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;margin-bottom:10px}
.cand-title{font-size:16px;font-weight:600;color:#FFF;line-height:1.4}
.cand-cat{display:inline-block;font-size:10px;color:#8B95A8;background:#1A1F2E;padding:2px 7px;border-radius:10px;margin-left:6px;vertical-align:middle;letter-spacing:0.5px}
.score-badge{flex-shrink:0;background:#3A4258;color:#FFF;padding:6px 12px;border-radius:8px;font-family:monospace;font-size:14px;font-weight:700;letter-spacing:0.3px}
.cand.top .score-badge{background:#4F8FD9}
.metrics{display:flex;gap:14px;font-family:monospace;font-size:12px;color:#8B95A8;margin-bottom:12px;flex-wrap:wrap}
.metric{display:flex;gap:4px;align-items:center}
.metric b{color:#D5DAE5;font-weight:600}
.brands{font-size:11px;color:#6B7280;line-height:1.6;margin-bottom:10px;font-family:monospace}
.brands code{background:#1A1F2E;padding:1px 6px;border-radius:4px;color:#8B95A8;margin-right:2px}
.rep-block{border-top:1px solid #2D3447;padding-top:10px;margin-top:6px}
.rep-label{font-size:10px;color:#6B7280;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px}
.rep{font-size:12px;color:#B8BFCE;line-height:1.5;margin-bottom:4px}
.rep .price{color:#7EB66B;font-weight:600}
.rep .src{color:#6B7280;font-size:11px;margin-left:4px}
.rep a{color:#B8BFCE;text-decoration:none}
.rep a:hover{color:#4F8FD9}
.actions{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap}
.btn{display:inline-block;font-size:11px;padding:5px 10px;border:1px solid #3A4258;border-radius:6px;text-decoration:none;color:#B8BFCE;font-family:monospace;letter-spacing:0.3px}
.btn:hover{border-color:#4F8FD9;color:#4F8FD9}
.dropped{margin-top:24px;font-size:11px;color:#6B7280;font-family:monospace;line-height:1.7}
.dropped h3{font-size:11px;color:#8B95A8;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;font-weight:600}
.dropped code{background:#252B3D;padding:1px 6px;border-radius:4px;color:#8B95A8;margin-right:4px}
.footer{text-align:center;padding:24px;font-size:10px;color:#4A5670;font-family:monospace;letter-spacing:0.3px}
.note{font-size:11px;color:#8B95A8;background:#252B3D;border-left:3px solid #4F8FD9;padding:10px 12px;border-radius:6px;margin:14px 0;line-height:1.6}
"""


def _algumon_search_url(query: str) -> str:
    import urllib.parse
    return f"https://www.algumon.com/n/deal?keyword={urllib.parse.quote(query)}"


def render_scout_html(top_picks: list[dict], dropped: list[dict],
                      excluded_ids: list[str], checked_at: str) -> str:
    if not top_picks:
        body = """<div class="empty">
          오늘은 추천 후보가 없습니다.<br>
          후보 풀에서 충분한 게시물이 잡힌 카테고리가 없거나 모두 점수가 낮습니다.
        </div>"""
    else:
        cards = []
        for i, p in enumerate(top_picks):
            top_class = " top" if i == 0 else ""
            brands_html = " ".join(f'<code>{b}</code>' for b in p["known_brands"][:8])
            rep_html = ""
            for r in p["representatives"]:
                ppu = r.get("price_per_unit") or 0
                unit = r.get("unit") or "개"
                src = r.get("source") or r.get("store") or ""
                url = r.get("external") or r.get("url") or "#"
                rep_html += (f'<div class="rep">'
                             f'<a href="{url}" target="_blank">'
                             f'<span class="price">{ppu:,}원/{unit}</span> '
                             f'{r.get("title", "")[:60]}'
                             f'</a><span class="src">· {src}</span>'
                             f'</div>')
            search_url = _algumon_search_url(p["name"])
            cards.append(f"""<div class="cand{top_class}">
  <div class="cand-head">
    <div class="cand-title">
      {p['emoji']} {p['name']}
      <span class="cand-cat">{p['category']}</span>
    </div>
    <div class="score-badge">{p['score']}</div>
  </div>
  <div class="metrics">
    <span class="metric">N <b>{p['n']}</b></span>
    <span class="metric">브랜드 <b>{p['brand_hits']}/{p['n']}</b> ({int(p['b_rate']*100)}%)</span>
    <span class="metric">활성 <b>{p['active_n']}/{p['n']}</b> ({int(p['d_rate']*100)}%)</span>
  </div>
  <div class="brands">등록 브랜드: {brands_html}</div>
  {('<div class="rep-block"><div class="rep-label">대표 핫딜 (단가 낮은 순)</div>' + rep_html + '</div>') if rep_html else ''}
  <div class="actions">
    <a class="btn" href="{search_url}" target="_blank">알구몬 검색 →</a>
  </div>
</div>""")
        body = "\n".join(cards)

    # 점수 미달 (min_posts 미만) 후보들
    dropped_html = ""
    if dropped:
        items = " ".join(f'<code>{d["emoji"]} {d["name"]}({d["n"]})</code>' for d in dropped)
        dropped_html = f"""<div class="dropped">
          <h3>오늘 점수 미달 (게시물 부족)</h3>
          {items}
        </div>"""

    excluded_html = ""
    if excluded_ids:
        items = " ".join(f'<code>{x}</code>' for x in excluded_ids)
        excluded_html = f"""<div class="dropped">
          <h3>이미 등록되어 있어 제외</h3>
          {items}
        </div>"""

    note = """<div class="note">
      <b>점수 산식</b>: N × B × D × 10 &nbsp;&nbsp;
      <code>N</code>=게시물 수, <code>B</code>=유명 브랜드 매칭률, <code>D</code>=활성 핫딜 비율<br>
      마음에 드는 후보는 keywords.json 에 정식 등록 → 다음 cron 부터 정규 모니터링.
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>🔍 카테고리 스카우터</title>
  <style>{_CSS}</style>
</head>
<body>
  <div class="header">
    <h1>🔍 카테고리 스카우터</h1>
    <p class="subtitle">{checked_at} · 알구몬 신규 카테고리 후보 분석</p>
    <p class="legend">관리/분석 페이지 — 본인 검토 후 keywords.json 에 등록</p>
  </div>
  <div class="main">
    {note}
    {body}
    {dropped_html}
    {excluded_html}
  </div>
  <div class="footer">internal · 매일 07:17 KST 자동 분석</div>
</body>
</html>"""


# ─────────────────────────────────────────────
# 푸시 알림
# ─────────────────────────────────────────────

def send_scout_push(top_picks: list[dict]) -> bool:
    if not send_push or not top_picks:
        return True
    title = f"🔍 오늘의 카테고리 후보 {len(top_picks)}개"
    lines = []
    for i, p in enumerate(top_picks[:5], 1):
        lines.append(f"#{i} {p['emoji']} {p['name']} (점수 {p['score']})")
    body = "\n".join(lines)
    actions = [{"action": "scout", "title": "📋 분석 리포트", "url": SCOUT_PAGE_URL}]
    try:
        ok, fail = send_push("scout", title, body, url=SCOUT_PAGE_URL, actions=actions)
        print(f"  push: ok={ok}, fail={fail}", flush=True)
        return (ok > 0) or (ok + fail == 0)
    except Exception as e:
        print(f"  [WARN] push 예외: {e}", flush=True)
        return False


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────

def main() -> None:
    now = datetime.now()
    print(f"\n[{now.strftime('%Y-%m-%d %H:%M')}] 카테고리 스카우터 시작", flush=True)

    data = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8-sig"))
    candidates = data.get("candidates", [])
    settings = data.get("scan_settings", {})
    timeout     = settings.get("request_timeout_sec", 15)
    delay       = settings.get("delay_between_candidates_sec", 1.5)
    max_posts   = settings.get("max_posts_per_candidate", 50)
    top_n       = settings.get("top_n_recommend", 5)

    # 기존 keywords.json 의 id 와 중복되는 후보는 제외
    existing_ids = {kw["id"] for kw in load_keywords().get("keywords", [])}
    excluded_ids = [c["id"] for c in candidates if c["id"] in existing_ids]
    candidates = [c for c in candidates if c["id"] not in existing_ids]
    print(f"후보 {len(candidates)}개 (이미 등록 제외 {len(excluded_ids)}개)", flush=True)

    scored = []
    dropped = []
    for i, c in enumerate(candidates):
        print(f"\n[{c['id']}] {c['name']} ...", flush=True)
        deals = fetch_search_multi(c["search_queries"], timeout=timeout, delay=1.0)[:max_posts]
        if not deals:
            print(f"  게시물 0건 — skip", flush=True)
            if i < len(candidates) - 1:
                time.sleep(delay)
            continue

        result = score_candidate(c, deals)
        if result is None:
            print(f"  점수 미달 (게시물 {len(deals)} < {c.get('min_posts', 5)})", flush=True)
            dropped.append({"id": c["id"], "name": c["name"], "emoji": c["emoji"], "n": len(deals)})
        else:
            print(f"  점수 {result['score']} (N={result['n']}, B={result['b_rate']}, D={result['d_rate']})", flush=True)
            scored.append(result)

        if i < len(candidates) - 1:
            time.sleep(delay)

    # 점수 높은 순 정렬, 상위 N개
    scored.sort(key=lambda r: r["score"], reverse=True)
    top_picks = scored[:top_n]

    print(f"\n[결과] 상위 {len(top_picks)}개:", flush=True)
    for p in top_picks:
        print(f"  {p['emoji']} {p['name']:<20} 점수 {p['score']}", flush=True)

    # HTML 렌더 + 로컬 저장
    checked_at = now.strftime("%Y-%m-%d %H:%M")
    html = render_scout_html(top_picks, dropped, excluded_ids, checked_at)
    SCOUT_HTML.write_text(html, encoding="utf-8")
    print(f"\n[HTML] {SCOUT_HTML}", flush=True)

    # GitHub Pages deploy
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    gh_token = os.environ.get("GH_TOKEN") or config.get("github_token", "")
    gh_owner = os.environ.get("GH_OWNER") or config.get("github_owner", "")
    gh_repo  = os.environ.get("GH_REPO")  or config.get("github_repo", "")
    if gh_token and gh_owner and gh_repo:
        try:
            url = deploy_to_github(html, gh_token, gh_owner, gh_repo, "scout.html")
            print(f"[URL] {url}", flush=True)
        except Exception as e:
            print(f"[ERROR] deploy 실패: {e}", flush=True)

    # 푸시 알림
    if top_picks:
        send_scout_push(top_picks)

    print(f"\n[완료] {datetime.now().strftime('%H:%M:%S')}", flush=True)


if __name__ == "__main__":
    main()
