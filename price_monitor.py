#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
뽐뿌 키워드 기반 핫딜 모니터 (v2)

- keywords.json 의 각 키워드를 뽐뿌 검색에 넣어 게시물 수집
- 캔당/개당 단가의 통계(IQR outlier 제거 → 25% 백분위)를 자체 산출
- 새 게시물 단가가 P25 × alert_pct / 100 이하면 알림 + HTML 리포트
- 외부 기준가(pricewagon 등) 의존 없음 — 시장 변동에 자동 적응
"""

from __future__ import annotations
import base64
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

try:
    from push_notify import send_push
except ImportError:
    send_push = None

ROOT = Path(__file__).parent
KEYWORDS_FILE = ROOT / "keywords.json"
SEEN_FILE     = ROOT / "seen_ids.json"
REPORT_FILE   = ROOT / "deals.html"
CONFIG_FILE   = ROOT / "config.json"

PPOMPPU_SEARCH_URL = (
    "https://www.ppomppu.co.kr/zboard/zboard.php"
    "?search_type=sub_memo&id=ppomppu&page_num=20&keyword={kw}"
)
PPOMPPU_VIEW_BASE  = "https://www.ppomppu.co.kr/zboard/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.ppomppu.co.kr/zboard/zboard.php?id=ppomppu",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# ─────────────────────────────────────────────
# HTML 파서
# ─────────────────────────────────────────────

class PpomppuParser(HTMLParser):
    """뽐뿌 게시판 / 검색 결과 목록 파서 — 제목 링크만 추출"""

    def __init__(self):
        super().__init__()
        self.posts: list[dict] = []
        self._capturing = False
        self._current: dict = {}
        self._buf: list[str] = []

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
                    "url": PPOMPPU_VIEW_BASE + href,
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


def _parse_dates_from_html(html: str) -> dict[str, datetime]:
    """검색 결과 HTML에서 게시물 ID → 작성일시 매핑 추출."""
    id_positions = [
        (m.start(), m.group(1))
        for m in re.finditer(r'baseList-title[^>]*href="view\.php\?[^"]*no=(\d+)"', html)
    ]
    date_re = re.compile(r'title="(\d{2})\.(\d{2})\.(\d{2})\s+(\d{2}):(\d{2}):(\d{2})"')
    out: dict[str, datetime] = {}
    for i, (pos, pid) in enumerate(id_positions):
        end = id_positions[i + 1][0] if i + 1 < len(id_positions) else len(html)
        m = date_re.search(html, pos, end)
        if m:
            yy, mm, dd, h, mi, s = (int(x) for x in m.groups())
            try:
                out[pid] = datetime(2000 + yy, mm, dd, h, mi, s)
            except ValueError:
                pass
    return out


def fetch_search(query: str, timeout: int = 15) -> list[dict]:
    """단일 검색어로 검색 페이지를 가져와 파싱된 게시물 리스트 반환 (date 포함)."""
    encoded = urllib.parse.quote(query.encode("euc-kr"))
    url = PPOMPPU_SEARCH_URL.format(kw=encoded)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"  [ERROR] '{query}' 검색 실패: {e}", flush=True)
        return []
    html = raw.decode("euc-kr", errors="replace")
    parser = PpomppuParser()
    parser.feed(html)
    date_map = _parse_dates_from_html(html)
    for p in parser.posts:
        p["date"] = date_map.get(p["id"])
    return parser.posts


def fetch_search_multi(queries: list[str], timeout: int = 15, delay: float = 1.0) -> list[dict]:
    """여러 검색어로 결과 합치기 + ID 중복 제거 (한글/영문/별칭 모두 커버)."""
    seen_ids: set = set()
    merged: list[dict] = []
    for i, q in enumerate(queries):
        if i > 0:
            time.sleep(delay)
        posts = fetch_search(q, timeout=timeout)
        for p in posts:
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                merged.append(p)
    return merged


# ─────────────────────────────────────────────
# 가격 / 수량 파싱 (기존 로직 + 박스 단위 추정)
# ─────────────────────────────────────────────

BOX_QTY_HINT = {
    "참치": 24,
    "스팸": 18,
}


def parse_total_price(title: str) -> int | None:
    """제목에서 총 가격 추출."""
    m = re.search(r"\(\s*([\d,]+)\s*[/,]", title)
    if m:
        v = int(m.group(1).replace(",", ""))
        if 500 <= v <= 1_000_000:
            return v
    prices = [int(x.replace(",", "")) for x in re.findall(r"([\d,]+)원", title)]
    prices = [p for p in prices if 500 <= p <= 1_000_000]
    if prices:
        return prices[-1]
    return None


def parse_quantity(title: str, query: str) -> int | None:
    """
    수량(캔/개 기준) 추출 — 보수적 전략.
    - "총 N개" 가 있으면 최우선
    - 그 외엔 마지막 "N캔/개/입" 매치
    - 곱셈은 의도적으로 사용 안 함 (오작동 위험)
    """
    # 1) "총 NN개" — 명시적 합계 우선
    m = re.search(r"총\s*(\d+)\s*(?:개|캔|입)", title)
    if m:
        v = int(m.group(1))
        if 1 < v <= 500:
            return v

    # 2) 단순 단위 매치 — 마지막 매치 사용 (보통 총합)
    candidates = re.findall(r"(\d+)\s*(?:캔|개|입)", title)
    if candidates:
        # 큰 숫자(>1)만 후보. 1, 2 같은 작은 숫자가 "박스 1개" 식이면 안 됨
        ints = [int(x) for x in candidates]
        # 마지막 매치 우선 (보통 묶음의 총합)
        for v in reversed(ints):
            if 1 < v <= 500:
                return v

    # 3) 박스 단위
    m = re.search(r"(\d+)\s*(?:박스|box)", title, re.IGNORECASE)
    if m:
        box = BOX_QTY_HINT.get(query, 6)
        v = int(m.group(1)) * box
        if 1 < v <= 500:
            return v

    return None


# 단가 sanity 기본값 (식품 기준; 키워드별로 override 가능)
DEFAULT_PPU_MIN = 200
DEFAULT_PPU_MAX = 30_000


def verify_alive(post_url: str, timeout: int = 8) -> bool:
    """게시물 페이지가 실제로 살아있는지 GET으로 검증.
    '존재하지 않' 문자열 또는 응답 < 40KB 면 죽은 페이지로 간주."""
    req = urllib.request.Request(post_url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except Exception as e:
        print(f"    [DEAD?] {post_url[-30:]}: {e}", flush=True)
        return False
    if len(raw) < 40_000:
        print(f"    [DEAD?] 응답 작음 ({len(raw)} bytes): {post_url[-30:]}", flush=True)
        return False
    try:
        html = raw.decode("euc-kr", errors="replace")
    except Exception:
        return True  # 디코드 실패해도 일단 살아있다 가정
    for marker in ("존재하지 않", "없는 게시물", "삭제된 게시물"):
        if marker in html:
            print(f"    [DEAD] '{marker}' 마커 검출: {post_url[-30:]}", flush=True)
            return False
    return True


def parse_post(
    post: dict,
    keyword_id: str,
    query: str,
    *,
    single_item: bool = False,
    ppu_min: int = DEFAULT_PPU_MIN,
    ppu_max: int = DEFAULT_PPU_MAX,
) -> dict:
    """
    single_item=True 면 수량 파싱 안 하고 총가격을 단가로 사용 (가전/전자제품 등).
    ppu_min/max 는 키워드별 단가 sanity 범위.
    """
    total = parse_total_price(post["title"])
    if single_item:
        qty = 1
        ppu = total
    else:
        qty = parse_quantity(post["title"], query)
        ppu = (total // qty) if (total and qty and qty > 0) else None
    # sanity
    if ppu is not None and not (ppu_min <= ppu <= ppu_max):
        ppu = None
    return {
        **post,
        "keyword_id":  keyword_id,
        "total_price": total,
        "quantity":    qty,
        "price_per_unit": ppu,
        # date 는 post에서 이미 들어있음
    }


def humanize_age(dt: datetime | None, ref: datetime | None = None) -> str:
    """작성일시 → '3시간 전', '2일 전' 같은 표현. None 이면 빈 문자열."""
    if dt is None:
        return ""
    if ref is None:
        ref = datetime.now()
    delta = ref - dt
    s = int(delta.total_seconds())
    if s < 0:
        return "방금"
    if s < 3600:
        return f"{max(1, s // 60)}분 전"
    if s < 86400:
        return f"{s // 3600}시간 전"
    return f"{s // 86400}일 전"


# ─────────────────────────────────────────────
# 통계 — IQR outlier 제거 + 백분위
# ─────────────────────────────────────────────

def quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    pos = q * (n - 1)
    lo, hi = int(pos), min(int(pos) + 1, n - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def stats_with_iqr(unit_prices: list[int], k: float = 1.5) -> dict:
    if len(unit_prices) < 4:
        clean = sorted(unit_prices)
    else:
        s = sorted(unit_prices)
        q1 = quantile(s, 0.25)
        q3 = quantile(s, 0.75)
        iqr = q3 - q1
        lo  = q1 - k * iqr
        hi  = q3 + k * iqr
        clean = [v for v in s if lo <= v <= hi]
    if not clean:
        return {"clean": [], "median": 0, "p25": 0, "min": 0, "n_raw": len(unit_prices), "n_clean": 0}
    return {
        "clean":   clean,
        "median":  int(statistics.median(clean)),
        "p25":     int(quantile(clean, 0.25)),
        "min":     int(min(clean)),
        "n_raw":   len(unit_prices),
        "n_clean": len(clean),
    }


# ─────────────────────────────────────────────
# 중복 방지
# ─────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            return set()
    return set()


def save_seen(seen: set) -> None:
    ids = sorted(seen)[-2000:]
    SEEN_FILE.write_text(json.dumps(ids), encoding="utf-8")


# ─────────────────────────────────────────────
# HTML 리포트
# ─────────────────────────────────────────────

def deal_card_html(d: dict, threshold: int) -> str:
    badge = ""
    if d["price_per_unit"] and threshold and d["price_per_unit"] <= threshold:
        badge = '<span class="badge-deal">🔥 알림 기준 이하</span>'
    if d["price_per_unit"]:
        price_line = (
            f'<span class="price">{d["price_per_unit"]:,}원/{d["unit"]}</span> '
            f'<span class="meta">(총 {d["total_price"]:,}원 / {d["quantity"]}{d["unit"]})</span>'
        )
    else:
        price_line = '<span class="price-unknown">가격 파싱 불가</span>'
    age = humanize_age(d.get("date"))
    age_html = f'<span class="age">{age}</span>' if age else ''
    return f"""<div class="card">
  <p class="card-title">{d['title'][:80]} {age_html}</p>
  <p class="card-price">{price_line} {badge}</p>
  <a href="{d['url']}" target="_blank" class="btn">뽐뿌 게시물 →</a>
</div>"""


def keyword_section_html(kw: dict, parsed: list[dict], stats: dict, threshold: int, hits: list[dict]) -> str:
    if stats["n_clean"] < kw.get("min_samples", 6):
        body = f'<p class="note">샘플 부족 ({stats["n_clean"]}건) — 통계 산출 보류</p>'
    else:
        body  = f'<p class="stats">P25 {stats["p25"]:,}원 · 중간값 {stats["median"]:,}원 · 최저 {stats["min"]:,}원 · 샘플 {stats["n_clean"]}/{stats["n_raw"]}건</p>'
        body += f'<p class="threshold">알림 기준: <b>{threshold:,}원/{kw.get("unit","개")} 이하</b> (P25의 {kw.get("alert_pct",80)}%)</p>'
        if hits:
            body += f'<h4>🔥 기준 이하 ({len(hits)}건)</h4>'
            body += "".join(deal_card_html(d, threshold) for d in hits)
        else:
            body += '<p class="note">현재 기준 이하 게시물 없음</p>'
        top = [p for p in parsed if p["price_per_unit"]][:5]
        if top:
            body += '<h4>최근 게시물</h4>'
            for d in top:
                body += deal_card_html(d, threshold)
    return f"""<section class="keyword">
  <h2>{kw['emoji']} {kw['name']}</h2>
  <p class="kw-desc">{kw.get('description','')}</p>
  {body}
</section>"""


def generate_html(sections: list[str], checked_at: str) -> str:
    css = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#FAF7F2;color:#2C1810;min-height:100vh}
.header{background:linear-gradient(135deg,#D0663C 0%,#B0502C 100%);color:white;padding:48px 20px 24px;text-align:center}
.header h1{font-size:22px;font-weight:700;margin-bottom:6px}
.subtitle{font-size:13px;opacity:.85}
.main{max-width:680px;margin:0 auto;padding:16px}
.keyword{background:white;border-radius:16px;padding:18px;margin-bottom:14px;box-shadow:0 1px 4px rgba(140,70,20,.08)}
.keyword h2{font-size:18px;font-weight:700;margin-bottom:4px}
.kw-desc{font-size:12px;color:#8B7355;margin-bottom:10px}
.stats{font-size:13px;color:#5C4030;background:#FAF0E8;padding:8px 10px;border-radius:8px;margin-bottom:8px}
.threshold{font-size:13px;color:#7C4530;margin-bottom:14px}
.threshold b{color:#B0502C}
.note{font-size:13px;color:#8B7355;padding:12px;text-align:center;background:#F5EDE0;border-radius:8px}
.keyword h4{font-size:14px;margin:14px 0 8px;color:#7C4530}
.card{border-left:3px solid #D0663C;background:#FFFCF8;padding:10px 12px;border-radius:6px;margin-bottom:8px}
.card-title{font-size:13px;font-weight:600;margin-bottom:4px;line-height:1.4}
.card-price{font-size:13px;color:#2C1810}
.card-price .price{font-weight:700;color:#B0502C;font-size:15px}
.card-price .meta{color:#8B7355;font-size:12px}
.card-price .price-unknown{color:#8B7355;font-style:italic}
.badge-deal{display:inline-block;background:#FAE2D4;color:#7C4530;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;margin-left:6px}
.age{display:inline-block;color:#A89070;font-size:11px;font-weight:400;margin-left:4px}
.btn{display:inline-block;color:#B0502C;text-decoration:none;font-size:12px;margin-top:6px}
.footer{text-align:center;padding:24px 16px;font-size:12px;color:#B8986A}
"""
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>핫딜 모니터</title>
  <style>{css}</style>
</head>
<body>
  <div class="header">
    <h1>🛒 키워드 핫딜 모니터</h1>
    <p class="subtitle">{checked_at} 기준 · 뽐뿌 검색 자체 통계</p>
  </div>
  <div class="main">
    {"".join(sections)}
  </div>
  <div class="footer">키워드별 P25 산출 → 알림 임계값 자동 결정</div>
</body>
</html>"""


# ─────────────────────────────────────────────
# GitHub 배포
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
    payload = {
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
    return f"https://{owner}.github.io/{repo}/{path}"


# ─────────────────────────────────────────────
# ntfy
# ─────────────────────────────────────────────

def send_ntfy(topic: str, keyword: dict, hits: list[dict], threshold: int, report_url: str = "") -> bool:
    lines = [f"{keyword['emoji']} {keyword['name']} 핫딜 {len(hits)}건!\n"]
    for d in hits[:5]:
        if d["price_per_unit"]:
            age = humanize_age(d.get("date"))
            age_s = f" · {age}" if age else ""
            lines.append(f"[{d['price_per_unit']:,}원/{d['unit']}{age_s}] {d['title'][:40]}")
    lines.append(f"\n알림 기준: ≤ {threshold:,}원/{keyword.get('unit','개')}")
    if report_url:
        lines.append(f"리포트: {report_url}")

    # deep link 액션 — 본문 탭은 1위 딜로, 액션 버튼 1~2개 추가
    actions = []
    if len(hits) >= 2:
        actions.append({"action": "view", "label": f"🛒 #2 게시물", "url": hits[1]["url"]})
    if report_url:
        actions.append({"action": "view", "label": "📋 전체 리포트", "url": report_url})
    click = hits[0]["url"] if hits else (report_url or "https://www.ppomppu.co.kr/")
    payload = json.dumps({
        "topic":    topic,
        "title":    f"{keyword['emoji']} {keyword['name']} 핫딜",
        "message":  "\n".join(lines),
        "tags":     ["fire"],
        "priority": 4,
        "click":    click,
        "actions":  actions,
    }, ensure_ascii=False).encode("utf-8")

    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                "https://ntfy.sh",
                data=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            urllib.request.urlopen(req, timeout=15)
            print(f"  [OK] ntfy 발송 ({keyword['id']})", flush=True)
            return True
        except Exception as e:
            print(f"  [WARN] ntfy 실패 시도 {attempt}/3 ({keyword['id']}): {e}", flush=True)
            if attempt < 3:
                time.sleep(2 ** attempt)
    return False


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────

def load_keywords() -> dict:
    if not KEYWORDS_FILE.exists():
        sys.exit(f"[FATAL] {KEYWORDS_FILE} 없음")
    return json.loads(KEYWORDS_FILE.read_text(encoding="utf-8"))


def main():
    now = datetime.now()
    force = os.environ.get("FORCE_RUN", "").lower() in ("1", "true", "yes")
    if not force and not (9 <= now.hour < 20):
        print(f"[SKIP] 운영 시간 외 ({now.strftime('%H:%M')})", flush=True)
        sys.exit(0)

    cfg_data = load_keywords()
    keywords = cfg_data.get("keywords", [])
    settings = cfg_data.get("scan_settings", {})
    timeout   = settings.get("request_timeout_sec", 15)
    delay     = settings.get("delay_between_keywords_sec", 1.5)
    max_posts = settings.get("max_posts_per_keyword", 30)

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    ntfy_topic = os.environ.get("NTFY_TOPIC") or config.get("ntfy_topic", "")
    gh_token   = os.environ.get("GH_TOKEN")   or config.get("github_token", "")
    gh_owner   = os.environ.get("GH_OWNER")   or config.get("github_owner", "")
    gh_repo    = os.environ.get("GH_REPO")    or config.get("github_repo", "")
    gh_path    = os.environ.get("GH_PATH")    or config.get("github_deals_path", "deals.html")

    print(f"\n[{now.strftime('%Y-%m-%d %H:%M')}] 키워드 모니터 시작 — {len(keywords)} 키워드", flush=True)

    seen = load_seen()
    sections: list[str] = []
    total_new_hits = 0

    for i, kw in enumerate(keywords):
        kid = kw["id"]
        # 단일 검색어 OR 검색어 리스트 둘 다 지원
        queries = kw.get("search_queries") or [kw.get("search_query", "")]
        queries = [q for q in queries if q]
        excludes = [e.lower() for e in kw.get("exclude", [])]
        single_item = kw.get("single_item", False)
        ppu_min = kw.get("ppu_min", DEFAULT_PPU_MIN)
        ppu_max = kw.get("ppu_max", DEFAULT_PPU_MAX)
        stale_days = kw.get("stale_days", 30)
        verify_links = kw.get("verify_links", True)
        print(f"\n[{kid}] 검색어 {queries}...", flush=True)
        posts = fetch_search_multi(queries, timeout=timeout, delay=1.0)[:max_posts]
        if excludes:
            before = len(posts)
            posts = [p for p in posts if not any(e in p["title"].lower() for e in excludes)]
            print(f"  게시물 {before} → {len(posts)}건 (제외어 적용)", flush=True)
        else:
            print(f"  게시물 {len(posts)}건 수집", flush=True)

        # 날짜 필터: stale_days 이상 된 건 통계에서 제외 (시장가 자동학습이 옛 가격에 끌리지 않게)
        cutoff = now - timedelta(days=stale_days)
        fresh = [p for p in posts if (p.get("date") is None) or (p["date"] >= cutoff)]
        if len(fresh) != len(posts):
            print(f"  날짜 필터 ({stale_days}일 이내): {len(posts)} → {len(fresh)}건", flush=True)
        posts = fresh

        # 첫 검색어를 query 인자로 (수량 파싱의 BOX_QTY_HINT 용)
        primary_query = queries[0] if queries else kid
        parsed = [
            parse_post(p, kid, primary_query, single_item=single_item, ppu_min=ppu_min, ppu_max=ppu_max)
            for p in posts
        ]
        for d in parsed:
            d["unit"] = kw.get("unit", "개")

        unit_prices = [d["price_per_unit"] for d in parsed if d["price_per_unit"]]
        stats = stats_with_iqr(unit_prices, k=kw.get("outlier_iqr_k", 1.5))
        print(f"  단가 파싱 {len(unit_prices)}건, outlier 제거 후 {stats['n_clean']}건", flush=True)

        threshold = 0
        hits: list[dict] = []
        if stats["n_clean"] >= kw.get("min_samples", 6):
            threshold = int(stats["p25"] * kw.get("alert_pct", 80) / 100)
            print(f"  P25={stats['p25']:,} → 임계값 {threshold:,}원/{kw.get('unit','개')}", flush=True)
            for d in parsed:
                if d["price_per_unit"] and d["price_per_unit"] <= threshold and d["id"] not in seen:
                    hits.append(d)
            print(f"  후보 핫딜 {len(hits)}건", flush=True)

            # 알림 직전 살아있는지 검증 (죽은 링크는 알림 제외)
            if verify_links and hits:
                alive: list[dict] = []
                for d in hits:
                    if verify_alive(d["url"]):
                        alive.append(d)
                    time.sleep(0.5)
                print(f"  링크 검증: {len(hits)} → {len(alive)}건 살아있음", flush=True)
                hits = alive
        else:
            print(f"  샘플 부족 ({stats['n_clean']} < {kw.get('min_samples', 6)}) — 알림 보류", flush=True)

        sections.append(keyword_section_html(kw, parsed, stats, threshold, hits))

        if hits:
            ntfy_ok = True
            if ntfy_topic:
                ntfy_ok = send_ntfy(ntfy_topic, kw, hits, threshold, "")
            push_ok = True
            if send_push:
                push_title = f"{kw['emoji']} {kw['name']} 핫딜 {len(hits)}건"
                lines = []
                for d in hits[:5]:
                    if d["price_per_unit"]:
                        age = humanize_age(d.get("date"))
                        age_s = f" · {age}" if age else ""
                        lines.append(f"{d['price_per_unit']:,}원/{kw.get('unit','개')}{age_s} — {d['title'][:30]}")
                push_body = "\n".join(lines) or "새 핫딜"
                # deep link 액션 (PWA Web Push 도 ntfy 와 같은 패턴)
                push_actions = []
                if len(hits) >= 2:
                    push_actions.append({"action": "deal2", "title": "🛒 #2", "url": hits[1]["url"]})
                push_actions.append({"action": "report", "title": "📋 리포트", "url": "https://nothing881122-netizen.github.io/flight-deals/deals.html"})
                first_url = hits[0]["url"]
                try:
                    ok, fail = send_push(kid, push_title, push_body, url=first_url, actions=push_actions)
                    push_ok = (ok > 0) or (ok + fail == 0)
                except Exception as e:
                    print(f"  [WARN] push 예외: {e}", flush=True)
                    push_ok = False

            if ntfy_ok or push_ok:
                seen.update(d["id"] for d in hits)
                total_new_hits += len(hits)
            else:
                print(f"  [WARN] 모든 알림 채널 실패 — seen 미갱신, 다음 실행에서 재시도", flush=True)

        if i < len(keywords) - 1:
            time.sleep(delay)

    checked_at = now.strftime("%Y-%m-%d %H:%M")
    html = generate_html(sections, checked_at)
    REPORT_FILE.write_text(html, encoding="utf-8")
    print(f"\n[HTML] 리포트 저장: {REPORT_FILE}", flush=True)

    if gh_token and gh_owner and gh_repo:
        try:
            report_url = deploy_to_github(html, gh_token, gh_owner, gh_repo, gh_path)
            print(f"[URL] {report_url}", flush=True)
        except Exception as e:
            print(f"[ERROR] GitHub 배포 실패: {e}", flush=True)

    save_seen(seen)
    print(f"\n[완료] 새 핫딜 총 {total_new_hits}건  ({datetime.now().strftime('%H:%M:%S')})", flush=True)


if __name__ == "__main__":
    main()
