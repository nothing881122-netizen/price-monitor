#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
알구몬 기반 키워드 핫딜 모니터

- keywords.json 의 각 키워드를 algumon.com 검색에 넣어 deal 수집
  (JSON endpoint __data.json 사용 — HTML 보다 3배+ 데이터, 만료 여부 포함)
- 알구몬이 이미 계산한 단가(N x M원 표기)를 활용 → 자체 수량 파싱 불필요
- 단가 통계(IQR outlier 제거 → P25)에서 알림 임계값 자동 산출
- 외부 기준가 의존 없음 — 시장 변동에 자동 적응
- 핫딜 발견 시 PWA Web Push 알림 + GitHub Pages 리포트 갱신
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
from pathlib import Path

try:
    from push_notify import send_push
except ImportError:
    send_push = None

# ─────────────────────────────────────────────
# 경로 / 상수
# ─────────────────────────────────────────────

ROOT          = Path(__file__).parent
KEYWORDS_FILE = ROOT / "keywords.json"
SEEN_FILE     = ROOT / "seen_ids.json"
REPORT_FILE   = ROOT / "deals.html"
CONFIG_FILE   = ROOT / "config.json"

ALGUMON_DATA_URL = "https://www.algumon.com/n/deal/__data.json?keyword={kw}"
ALGUMON_DEAL_URL = "https://www.algumon.com/n/deal/{id}"
REPORT_BASE_URL  = "https://nothing881122-netizen.github.io/flight-deals/deals.html"
SUPER_PAGE_URL   = "https://nothing881122-netizen.github.io/flight-deals/super-deals.html"
SUPER_FILE       = Path(__file__).parent / "super-deals.html"
DEFAULT_SUPER_ALERT_PCT = 60   # 평소 가격의 60% 이하면 "찐 특가"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.algumon.com/",
}

# 단가 sanity 기본 범위 (식품 기준; keywords.json 에서 키워드별 override)
DEFAULT_PPU_MIN = 200
DEFAULT_PPU_MAX = 30_000

# "18 x 2,607원" 식 단가 표기 — 알구몬이 perPriceText 에 넣어줌
_PERPRICE_RE = re.compile(r"(\d{1,4})\s*[x×]\s*([\d,]+)\s*원")
_PRICE_RE    = re.compile(r"([\d,]+)\s*원")


# ─────────────────────────────────────────────
# 알구몬 JSON endpoint 파서
# ─────────────────────────────────────────────

def _resolve_ref(idx, pool: list, _stack: set | None = None):
    """SvelteKit 참조(int 인덱스) 풀기 — 재귀 + 순환 방지."""
    if not isinstance(idx, int):
        return idx
    if _stack is None:
        _stack = set()
    if idx in _stack or idx < 0 or idx >= len(pool):
        return None
    _stack = _stack | {idx}
    val = pool[idx]
    if isinstance(val, dict):
        return {k: _resolve_ref(v, pool, _stack) for k, v in val.items()}
    if isinstance(val, list):
        return [_resolve_ref(v, pool, _stack) for v in val]
    return val


def _parse_iso_date(s: str | None) -> datetime | None:
    """알구몬 createdAt ISO 문자열 → datetime."""
    if not isinstance(s, str):
        return None
    try:
        iso = s.rstrip("Z").split(".")[0]  # "...Z" 또는 ".sss" 제거
        return datetime.fromisoformat(iso)
    except Exception:
        return None


def _build_deal(deal: dict) -> dict | None:
    """알구몬 deal dict(참조 풀린 상태) → 표준 핫딜 항목."""
    deal_id = deal.get("id")
    if deal_id is None:
        return None
    title = (deal.get("title") or "").strip()
    if not title:
        return None

    # 가격 / 단가
    price_text = str(deal.get("price") or "")
    per_text   = str(deal.get("perPriceText") or "")
    m_total = _PRICE_RE.search(price_text)
    total_price = int(m_total.group(1).replace(",", "")) if m_total else None
    qty, ppu = None, None
    for src in (per_text, price_text):
        m = _PERPRICE_RE.search(src)
        if m:
            qty = int(m.group(1))
            ppu = int(m.group(2).replace(",", ""))
            break

    return {
        "id":             str(deal_id),
        "title":          title,
        "url":            ALGUMON_DEAL_URL.format(id=deal_id),
        "external":       (deal.get("originalUrl") or "").strip() or ALGUMON_DEAL_URL.format(id=deal_id),
        "source":         (deal.get("siteName")  or "").strip(),  # 출처 커뮤니티 사이트명
        "store":          (deal.get("storeName") or "").strip(),  # 쇼핑몰 (CJ더마켓 등)
        "total_price":    total_price,
        "quantity":       qty,
        "price_per_unit": ppu,
        "date":           _parse_iso_date(deal.get("createdAt")),
        "ended":          bool(deal.get("ended", False)),
    }


def fetch_search(query: str, timeout: int = 15) -> list[dict]:
    """알구몬 JSON endpoint 에서 한 키워드 검색 결과 가져오기."""
    url = ALGUMON_DATA_URL.format(kw=urllib.parse.quote(query.encode("utf-8")))
    req = urllib.request.Request(url, headers={**HEADERS, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  [ERROR] '{query}' 검색 실패: {e}", flush=True)
        return []

    try:
        pool = data["nodes"][1]["data"]
    except (KeyError, IndexError, TypeError):
        return []
    if not isinstance(pool, list):
        return []

    deals: list[dict] = []
    seen_ids: set[str] = set()
    for item in pool:
        if not isinstance(item, dict):
            continue
        if not all(k in item for k in ("storeName", "price", "title")):
            continue
        # 값이 int 인덱스면 pool 에서 풀어서 실제 값으로
        resolved = {k: (_resolve_ref(v, pool) if isinstance(v, int) else v) for k, v in item.items()}
        info = _build_deal(resolved)
        if info and info["id"] not in seen_ids:
            seen_ids.add(info["id"])
            deals.append(info)
    return deals


def fetch_search_multi(queries: list[str], timeout: int = 15, delay: float = 1.0) -> list[dict]:
    """여러 검색어 결과 합치기 + ID 중복 제거 (한·영 혼용 키워드용)."""
    seen_ids: set[str] = set()
    merged: list[dict] = []
    for i, q in enumerate(queries):
        if i > 0:
            time.sleep(delay)
        for d in fetch_search(q, timeout=timeout):
            if d["id"] not in seen_ids:
                seen_ids.add(d["id"])
                merged.append(d)
    return merged


# ─────────────────────────────────────────────
# 통계 — IQR outlier 제거 + 백분위
# ─────────────────────────────────────────────

def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    pos = q * (n - 1)
    lo, hi = int(pos), min(int(pos) + 1, n - 1)
    return sorted_values[lo] * (1 - (pos - lo)) + sorted_values[hi] * (pos - lo)


def compute_stats(unit_prices: list[int], iqr_k: float = 1.5) -> dict:
    """단가 리스트 → IQR outlier 제거 후 P25/중간값/최저 + 표본 수."""
    if len(unit_prices) < 4:
        clean = sorted(unit_prices)
    else:
        s = sorted(unit_prices)
        q1 = _quantile(s, 0.25)
        q3 = _quantile(s, 0.75)
        iqr = q3 - q1
        clean = [v for v in s if (q1 - iqr_k * iqr) <= v <= (q3 + iqr_k * iqr)]
    if not clean:
        return {"clean": [], "median": 0, "p25": 0, "min": 0,
                "n_raw": len(unit_prices), "n_clean": 0}
    return {
        "clean":   clean,
        "median":  int(statistics.median(clean)),
        "p25":     int(_quantile(clean, 0.25)),
        "min":     int(min(clean)),
        "n_raw":   len(unit_prices),
        "n_clean": len(clean),
    }


# ─────────────────────────────────────────────
# 핫딜 후처리 (sanity / 시간 표시)
# ─────────────────────────────────────────────

def apply_sanity(deal: dict, *, single_item: bool, ppu_min: int, ppu_max: int) -> None:
    """단일 상품 처리 + 단가 sanity 범위. 비현실적이면 ppu=None 로 통계 제외."""
    if single_item:
        deal["quantity"] = 1
        deal["price_per_unit"] = deal.get("total_price")
    ppu = deal.get("price_per_unit")
    if ppu is not None and not (ppu_min <= ppu <= ppu_max):
        deal["price_per_unit"] = None


def humanize_age(dt: datetime | None, ref: datetime | None = None) -> str:
    """datetime → '3시간 전' / '2일 전' 같은 표현."""
    if dt is None:
        return ""
    ref = ref or datetime.now()
    s = int((ref - dt).total_seconds())
    if s < 0:
        return "방금"
    if s < 3600:
        return f"{max(1, s // 60)}분 전"
    if s < 86400:
        return f"{s // 3600}시간 전"
    return f"{s // 86400}일 전"


# ─────────────────────────────────────────────
# 만료 링크 추가 검증 (algumon ended 만으론 놓치는 경우 보완)
# ─────────────────────────────────────────────

_DEAD_MARKERS = ("존재하지 않", "찾을 수 없", "삭제된", "Not Found")


def verify_alive(deal_url: str, timeout: int = 8) -> bool:
    """알구몬 상세 페이지 살아있는지 GET. 응답 < 20KB 또는 dead marker → False."""
    try:
        with urllib.request.urlopen(urllib.request.Request(deal_url, headers=HEADERS), timeout=timeout) as r:
            raw = r.read()
    except Exception as e:
        print(f"    [DEAD?] {deal_url[-30:]}: {e}", flush=True)
        return False
    if len(raw) < 20_000:
        print(f"    [DEAD?] 응답 작음 ({len(raw)} bytes)", flush=True)
        return False
    try:
        html = raw.decode("utf-8", errors="replace")
    except Exception:
        return True
    for marker in _DEAD_MARKERS:
        if marker in html:
            print(f"    [DEAD] '{marker}' 마커", flush=True)
            return False
    return True


# ─────────────────────────────────────────────
# seen_ids — 알림 중복 방지
# ─────────────────────────────────────────────

def load_seen() -> set:
    if not SEEN_FILE.exists():
        return set()
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen)[-2000:]), encoding="utf-8")


# ─────────────────────────────────────────────
# HTML 리포트
# ─────────────────────────────────────────────

def _deal_card(deal: dict, threshold: int) -> str:
    badge = ""
    ppu = deal.get("price_per_unit")
    if ppu and threshold and ppu <= threshold:
        badge = '<span class="badge-deal">🔥 알림 기준 이하</span>'
    if ppu:
        unit = deal.get("unit", "개")
        qty = deal.get("quantity") or 1
        total = deal.get("total_price") or 0
        price_line = (
            f'<span class="price">{ppu:,}원/{unit}</span> '
            f'<span class="meta">(총 {total:,}원 / {qty}{unit})</span>'
        )
    else:
        price_line = '<span class="price-unknown">가격 파싱 불가</span>'
    age = humanize_age(deal.get("date"))
    age_html = f'<span class="age">{age}</span>' if age else ""
    src = deal.get("source") or ""
    src_html = f'<span class="src">📍 {src}</span>' if src else ""
    link = deal.get("url") or deal.get("external")
    return f"""<div class="card">
  <p class="card-title">{deal['title'][:80]} {age_html} {src_html}</p>
  <p class="card-price">{price_line} {badge}</p>
  <div class="card-actions">
    <a href="{link}" target="_blank" class="btn btn-primary">딜 보러 가기 →</a>
  </div>
</div>"""


def _pick_representative(kw: dict, brand_results: list[dict]) -> dict | None:
    """대표 brand 선택 — representative_brand_id 명시 우선, 없으면 표본 가장 많은 brand."""
    if not brand_results:
        return None
    rep_id = kw.get("representative_brand_id")
    if rep_id:
        for br in brand_results:
            if br["brand"].get("brand_id") == rep_id:
                return br
    # fallback: 통계 산출된 것 중 표본 가장 많은 brand
    valid = [br for br in brand_results
             if br["stats"]["n_clean"] >= br["brand"].get("min_samples", 6)]
    if valid:
        return max(valid, key=lambda r: r["stats"]["n_clean"])
    return max(brand_results, key=lambda r: r["stats"]["n_clean"])


def _keyword_section(kw: dict, brand_results: list[dict]) -> str:
    """키워드 섹션 — UI는 통합 (대표 brand 기준 stats + 모든 brand 의 hits/recent 통합)."""
    has_brands = bool(kw.get("sub_brands"))
    rep = _pick_representative(kw, brand_results)
    unit = kw.get("unit", "개")

    # 대표 brand 의 stats / threshold 표시
    if rep is None or rep["stats"]["n_clean"] < rep["brand"].get("min_samples", 6):
        body = '<p class="note">데이터를 모으는 중입니다</p>'
    else:
        stats = rep["stats"]
        threshold = rep["threshold"]
        rep_name = rep["brand"].get("brand_name", "") or rep["brand"].get("name", "")
        body  = (f'<p class="stats">평소 가격 <b>{stats["p25"]:,}원</b> · '
                 f'중간값 {stats["median"]:,}원 · 최저 {stats["min"]:,}원</p>')
        if has_brands and rep_name and rep_name != kw["name"]:
            body += f'<p class="rep-note">📌 {rep_name} 기준</p>'
        body += (f'<p class="threshold">대박 기준: <b>{threshold:,}원/{unit} 이하</b></p>')

    # 모든 brand 의 hits + super_hits 통합 — 가격 낮은 순 정렬
    all_hits = []
    for br in brand_results:
        all_hits.extend(br.get("hits", []))
        all_hits.extend(br.get("super_hits", []))
    all_hits.sort(key=lambda d: d.get("price_per_unit") or 9_999_999)
    if all_hits:
        # 대표 threshold (없으면 0) 로 카드 배지 표시 — 카드 내 자체 threshold 비교는 본 brand 기준
        body += f'<h4>🔥 핫딜 ({len(all_hits)}건)</h4>'
        body += "".join(_deal_card(d, d.get("_brand_threshold", 0)) for d in all_hits)

    # 모든 brand 의 최근 게시물 (30일 이내) 통합 — 시간순, 최대 2건
    cutoff = datetime.now() - timedelta(days=30)
    all_recent = []
    for br in brand_results:
        for d in br["deals"]:
            if d.get("price_per_unit") and d.get("date") and d["date"] >= cutoff:
                all_recent.append(d)
    # 핫딜로 이미 표시된 ID 제외
    hit_ids = {d["id"] for d in all_hits}
    all_recent = [d for d in all_recent if d["id"] not in hit_ids]
    all_recent.sort(key=lambda d: d["date"], reverse=True)
    top = all_recent[:2]
    if top:
        body += '<h4>최근 게시물</h4>'
        body += "".join(_deal_card(d, d.get("_brand_threshold", 0)) for d in top)

    return f"""<section class="keyword" id="kw-{kw['id']}">
  <h2>{kw['emoji']} {kw['name']}</h2>
  <p class="kw-desc">{kw.get('description', '')}</p>
  {body}
</section>"""


def _summary_table(rows: list[dict], categories: list[dict] | None = None) -> str:
    """헤더 아래 종합 기준가 표 — 카테고리별 그룹 헤더 + 키워드 행, 3 컬럼."""
    if not rows:
        return ""
    # 카테고리별 그룹핑
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        cid = r.get("category") or "other"
        by_cat.setdefault(cid, []).append(r)
    cat_map = {c["id"]: c for c in (categories or [])}
    cat_order = [c["id"] for c in (categories or [])]
    for cid in by_cat:
        if cid not in cat_order:
            cat_order.append(cid)

    tr_html = ""
    for cid in cat_order:
        rs = by_cat.get(cid)
        if not rs:
            continue
        meta = cat_map.get(cid)
        if meta and meta.get("name"):
            tr_html += (f'<tr class="ref-cat-header" data-cat="{cid}">'
                        f'<td colspan="3">{meta.get("emoji","")} {meta["name"]}</td></tr>')
        for r in rs:
            min_samples = r.get("min_samples", 6)
            if r["n_clean"] >= min_samples:
                p25_s = f'{r["p25"]:,}원'
                thr_s = f'{r["threshold"]:,}원'
            else:
                p25_s = thr_s = "—"
            tr_html += f"""<tr data-cat="{cid}">
  <td class="ref-cat"><a href="#kw-{r['id']}">{r['emoji']}<br>{r['name']}</a></td>
  <td class="ref-num"><span class="ref-label">평소 가격</span>{p25_s}</td>
  <td class="ref-num ref-thr"><span class="ref-label">대박 기준</span>{thr_s}</td>
</tr>"""
    return f"""<section class="ref-section">
  <h2 class="ref-title">📊 기준가 종합</h2>
  <p class="ref-source">평소보다 싸진 순간을 절대 놓치지 않고 알려드립니다.</p>
  <table class="ref-table">
    <tbody>{tr_html}</tbody>
  </table>
</section>"""


_CSS = """
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
.card-price{font-size:13px;color:#2C1810;margin-bottom:8px}
.card-price .price{font-weight:700;color:#B0502C;font-size:15px}
.card-price .meta{color:#8B7355;font-size:12px}
.card-price .price-unknown{color:#8B7355;font-style:italic}
.badge-deal{display:inline-block;background:#FAE2D4;color:#7C4530;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;margin-left:6px}
.age{display:inline-block;color:#A89070;font-size:11px;font-weight:400;margin-left:4px}
.src{display:inline-block;color:#7C4530;font-size:11px;font-weight:500;margin-left:4px;background:#FAE2D4;padding:1px 6px;border-radius:8px}
.card-actions{display:flex;gap:6px;margin-top:6px}
.btn{display:inline-block;color:#B0502C;text-decoration:none;font-size:12px;padding:5px 10px;border:1px solid #D0663C;border-radius:6px}
.btn-primary{background:#D0663C;color:white;border:none}
.footer{text-align:center;padding:24px 16px;font-size:12px;color:#B8986A}
.rep-note{font-size:11px;color:#A89070;font-weight:500;margin-top:-4px;margin-bottom:8px}
.ref-section{background:white;border-radius:16px;padding:18px;margin-bottom:14px;box-shadow:0 1px 4px rgba(140,70,20,.08)}
.ref-title{font-size:17px;font-weight:700;margin-bottom:6px;color:#7C4530}
.ref-source{font-size:12px;color:#8B7355;background:#FAE2D4;border-left:3px solid #D0663C;border-radius:6px;padding:8px 10px;margin-bottom:12px;line-height:1.5}
.ref-table{width:100%;border-collapse:separate;border-spacing:0 6px;background:transparent;table-layout:fixed}
.ref-table td{padding:10px 8px;font-size:13px;vertical-align:middle;background:#FFFCF8}
.ref-table tr td:first-child{border-radius:8px 0 0 8px}
.ref-table tr td:last-child{border-radius:0 8px 8px 0}
.ref-cat{width:34%;text-align:center;background:#FAE2D4 !important}
.ref-cat a{color:#B0502C;text-decoration:none;font-weight:700;font-size:13px;line-height:1.3;display:inline-block}
.ref-cat a:hover{text-decoration:underline}
.ref-num{text-align:right;font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap}
.ref-label{display:block;font-size:10px;font-weight:500;color:#A89070;letter-spacing:0.5px;margin-bottom:2px}
.ref-thr{color:#B0502C}
.ref-thr .ref-label{color:#D0663C}
.cat-divider{font-size:14px;font-weight:700;color:#7C4530;background:#FAE2D4;padding:10px 14px;border-radius:10px;margin:18px 0 10px;letter-spacing:0.3px}
.cat-divider:first-child{margin-top:0}
.ref-cat-header td{background:#FAE2D4 !important;color:#7C4530;font-weight:700;font-size:13px;padding:8px 10px;border-radius:8px !important;text-align:center;letter-spacing:0.3px}
"""


def render_super_html(super_items: list[dict], checked_at: str) -> str:
    """찐 특가 전용 페이지. URL 의 ?id= 가 있으면 그 카드를 맨 위 강조.
    활성 찐 특가가 0건이면 메인 리포트로 redirect (희귀 개념 유지)."""
    if not super_items:
        # 활성 찐 특가 없음 — 페이지 진입 시 메인 리포트로 즉시 이동
        return """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="0;url=deals.html">
  <title>알리미</title>
</head>
<body></body>
</html>"""
    if True:
        cards = []
        for s in super_items:
            d = s["deal"]
            kw = s["kw"]
            brand = s["brand"]
            ppu = d.get("price_per_unit") or 0
            unit = kw.get("unit", "개")
            p25 = s["p25"]
            saved_pct = int((1 - ppu / p25) * 100) if p25 else 0
            link = d.get("url") or d.get("external")
            cards.append(f"""<article class="super-card" data-id="{d['id']}">
  <div class="badge-row">
    <span class="badge-emoji">🚨🔥⚡</span>
    <span class="badge-text">역대급 찐 특가</span>
    <span class="badge-pct">-{saved_pct}%</span>
  </div>
  <p class="kw-line">{kw['emoji']} {kw['name']}{' · ' + brand.get('brand_name', '') if brand.get('brand_name') and brand.get('brand_name') != kw['name'] else ''}</p>
  <h2 class="title">{d['title'][:90]}</h2>
  <div class="price-row">
    <span class="big-price">{ppu:,}<span class="unit">원/{unit}</span></span>
  </div>
  <p class="compare">평소 <s>{p25:,}원</s> → 지금 <b>{ppu:,}원</b></p>
  <a class="cta" href="{link}" target="_blank">🛒 지금 사러 가기 →</a>
</article>""")
        body = "\n".join(cards)

    css = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:linear-gradient(180deg,#7A1A1A 0%,#2C0808 100%);color:white;min-height:100vh;padding-bottom:48px}
.header{padding:48px 20px 20px;text-align:center}
.header h1{font-size:28px;font-weight:900;text-shadow:0 2px 12px rgba(255,200,0,.4);animation:pulse 2s ease-in-out infinite}
.subtitle{font-size:13px;opacity:.7;margin-top:8px}
.main{max-width:600px;margin:0 auto;padding:16px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.7}}
.super-card{background:linear-gradient(135deg,#FFE066 0%,#FF6B35 100%);color:#2C0808;border-radius:24px;padding:24px;margin-bottom:18px;box-shadow:0 12px 40px rgba(255,140,0,.4);position:relative;overflow:hidden;transition:transform .2s}
.super-card.highlight{box-shadow:0 0 0 4px #FFD700, 0 12px 40px rgba(255,200,0,.6);transform:scale(1.02)}
.badge-row{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.badge-emoji{font-size:24px}
.badge-text{font-weight:900;font-size:14px;letter-spacing:.5px;background:#8B0000;color:white;padding:5px 12px;border-radius:14px}
.badge-pct{font-size:22px;font-weight:900;color:#8B0000;margin-left:auto}
.kw-line{font-size:14px;font-weight:600;color:#5C1A00;margin-bottom:6px}
.title{font-size:17px;font-weight:700;line-height:1.4;margin-bottom:18px;color:#2C0808}
.price-row{display:flex;align-items:baseline;margin-bottom:6px}
.big-price{font-size:54px;font-weight:900;color:#8B0000;line-height:1;letter-spacing:-1px}
.big-price .unit{font-size:18px;font-weight:600;margin-left:6px;color:#5C1A00}
.compare{font-size:14px;color:#5C1A00;margin-bottom:18px}
.compare s{opacity:.6}
.compare b{color:#8B0000;font-size:16px}
.cta{display:block;background:#2C0808;color:#FFE066;text-align:center;padding:16px;border-radius:14px;text-decoration:none;font-weight:800;font-size:16px;letter-spacing:.5px}
.cta:active{background:#000}
.empty-card{background:rgba(255,255,255,.08);border-radius:24px;padding:48px 24px;text-align:center;backdrop-filter:blur(10px)}
.empty-emoji{font-size:48px;margin-bottom:14px}
.empty-text{font-size:18px;font-weight:700;margin-bottom:8px}
.empty-sub{font-size:13px;opacity:.7;line-height:1.5}
.footer{text-align:center;padding:24px;font-size:11px;opacity:.5}
"""

    # 도착 즉시 자동 TTS — user gesture 한 번 (알림 탭) 후라 페이지 진입 시 허용됨
    tts_script = """
<script>
(function(){
  const params = new URLSearchParams(location.search);
  const targetId = params.get('id');
  // 강조 처리
  if (targetId) {
    document.querySelectorAll('.super-card').forEach(c => {
      if (c.dataset.id === targetId) {
        c.classList.add('highlight');
        c.scrollIntoView({behavior:'smooth', block:'center'});
      }
    });
  }
  // TTS 자동 재생 (찐 특가 1건만)
  const tts_card = document.querySelector('.super-card.highlight') || document.querySelector('.super-card');
  if (tts_card && 'speechSynthesis' in window) {
    const kw = tts_card.querySelector('.kw-line')?.textContent.trim() || '';
    const price = tts_card.querySelector('.big-price')?.textContent.trim() || '';
    const text = `찐 특가 발견! ${kw}, ${price}`;
    setTimeout(() => {
      try {
        const u = new SpeechSynthesisUtterance(text);
        u.lang = 'ko-KR';
        u.rate = 1.05;
        speechSynthesis.speak(u);
      } catch(e){}
    }, 400);
  }
})();
</script>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>🚨 찐 특가</title>
  <style>{css}</style>
</head>
<body>
  <div class="header">
    <h1>🚨🔥 찐 특가 🔥🚨</h1>
    <p class="subtitle">{checked_at} · 평소 가격의 60% 이하만</p>
  </div>
  <div class="main">
    {body}
  </div>
  <div class="footer">🚨 평소 가격 대비 -40% 이상 핫딜만 표시</div>
  {tts_script}
</body>
</html>"""


_TOGGLE_SYNC_SCRIPT = """
<script>
// PWA 와 같은 origin 이라 localStorage 공유. 사용자가 PWA 설정에서 OFF 한 토픽은 리포트에서도 숨김.
(function(){
  let prefs = {};
  try { prefs = JSON.parse(localStorage.getItem('alimi.topics') || '{}'); } catch(e){}
  // 사용자가 PWA 를 한 번도 안 본 경우 prefs 비어있음 → 모두 표시 (기본)
  if (!prefs || Object.keys(prefs).length === 0) return;
  // 키워드 섹션 숨김
  document.querySelectorAll('section.keyword').forEach(sec => {
    const id = sec.id.replace(/^kw-/, '');
    if (prefs[id] === false) sec.style.display = 'none';
  });
  // 종합표 row 숨김
  document.querySelectorAll('.ref-table tbody tr').forEach(tr => {
    if (tr.classList.contains('ref-cat-header')) return;
    const a = tr.querySelector('a[href^="#kw-"]');
    if (!a) return;
    const id = a.getAttribute('href').replace('#kw-', '');
    if (prefs[id] === false) tr.style.display = 'none';
  });
  // 빈 카테고리 divider 도 숨김 (해당 카테고리 키워드가 전부 OFF 인 경우)
  const dividers = [...document.querySelectorAll('.cat-divider')];
  dividers.forEach((div, idx) => {
    const next = dividers[idx + 1] || null;
    let n = div.nextElementSibling;
    let visible = 0;
    while (n && n !== next) {
      if (n.classList && n.classList.contains('keyword') && n.style.display !== 'none') visible++;
      n = n.nextElementSibling;
    }
    if (visible === 0) div.style.display = 'none';
  });
  // 종합표 카테고리 헤더도 같은 방식
  const catHeaders = [...document.querySelectorAll('.ref-cat-header')];
  catHeaders.forEach((h, idx) => {
    const next = catHeaders[idx + 1] || null;
    let n = h.nextElementSibling;
    let visible = 0;
    while (n && n !== next) {
      if (n.tagName === 'TR' && !n.classList.contains('ref-cat-header') && n.style.display !== 'none') visible++;
      n = n.nextElementSibling;
    }
    if (visible === 0) h.style.display = 'none';
  });
})();
</script>
"""


def render_html(sections: list[dict], checked_at: str, summary_rows: list[dict],
                categories: list[dict] | None = None) -> str:
    # 카테고리별 그룹핑 — sections 는 {category, id, html} 형태의 dict 리스트
    by_cat: dict[str, list[dict]] = {}
    for s in sections:
        cid = s.get("category") or "other"
        by_cat.setdefault(cid, []).append(s)
    cat_map = {c["id"]: c for c in (categories or [])}
    cat_order = [c["id"] for c in (categories or [])]
    for cid in by_cat:
        if cid not in cat_order:
            cat_order.append(cid)

    body_parts: list[str] = []
    for cid in cat_order:
        ss = by_cat.get(cid)
        if not ss:
            continue
        meta = cat_map.get(cid)
        if meta and meta.get("name"):
            body_parts.append(
                f'<h2 class="cat-divider" data-cat="{cid}">'
                f'{meta.get("emoji","")} {meta["name"]}</h2>'
            )
        for s in ss:
            body_parts.append(s["html"])
    sections_html = "".join(body_parts)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>핫딜 모니터</title>
  <style>{_CSS}</style>
</head>
<body>
  <div class="header">
    <h1>🛒 키워드 핫딜 모니터</h1>
    <p class="subtitle">{checked_at} · 알구몬 통합 검색</p>
  </div>
  <div class="main">
    {_summary_table(summary_rows, categories)}
    {sections_html}
  </div>
  <div class="footer">알구몬 통합 핫딜 · 키워드별 P25 자체 산출</div>
  {_TOGGLE_SYNC_SCRIPT}
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
        sha = json.loads(urllib.request.urlopen(req, timeout=10).read()).get("sha")
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
# 메인
# ─────────────────────────────────────────────

def load_keywords() -> dict:
    if not KEYWORDS_FILE.exists():
        sys.exit(f"[FATAL] {KEYWORDS_FILE} 없음")
    return json.loads(KEYWORDS_FILE.read_text(encoding="utf-8-sig"))


def _dedup_cross_source(deals: list[dict]) -> list[dict]:
    """동일 (쇼핑몰, 총가, 단가) 인 카드는 같은 핫딜로 간주, 가장 먼저 올라온 것만 유지.
    여러 커뮤니티(뽐뿌·아카라이브·클리앙 등)에 같은 딜이 공유될 때 중복 알림 방지."""
    seen: dict[tuple, dict] = {}
    others: list[dict] = []  # 키 생성 불가한 deal (store/총가 None)
    for d in deals:
        store = d.get("store") or ""
        total = d.get("total_price")
        ppu = d.get("price_per_unit")
        if not store or total is None:
            others.append(d)
            continue
        key = (store, total, ppu)
        existing = seen.get(key)
        if existing is None:
            seen[key] = d
        else:
            # 더 먼저 올라온 게 원본일 가능성 높음 → 그것 유지
            d_date = d.get("date")
            e_date = existing.get("date")
            if d_date and e_date and d_date < e_date:
                seen[key] = d
    return list(seen.values()) + others


def _resolve_brands(kw: dict) -> list[dict]:
    """sub_brands 있으면 각 brand 를 독립 처리 단위로. 없으면 kw 자체를 단일 brand로."""
    if not kw.get("sub_brands"):
        return [kw]
    brands = []
    parent_excludes = list(kw.get("exclude", []))
    for sb in kw["sub_brands"]:
        merged = {**kw, **sb}  # sb 가 우선
        merged.pop("sub_brands", None)
        # exclude 는 부모 + 자식 결합
        merged["exclude"] = parent_excludes + list(sb.get("exclude", []))
        # brand 의 id 는 키워드 id 와 합쳐서 unique
        merged["brand_id"] = sb.get("id", "")
        merged["brand_name"] = sb.get("name", sb.get("id", ""))
        brands.append(merged)
    return brands


def _filter_deals(deals: list[dict], kw: dict, now: datetime) -> list[dict]:
    """키워드 설정에 따라 deals 필터링 (require_any / exclude / 만료 / 날짜 / 중복)."""
    require_any = [t.lower() for t in kw.get("require_any", [])]
    excludes    = [e.lower() for e in kw.get("exclude", [])]
    stale_days  = kw.get("stale_days", 30)

    before = len(deals)
    if require_any:
        deals = [d for d in deals if any(t in d["title"].lower() for t in require_any)]
        if len(deals) != before:
            print(f"  카드 {before} → {len(deals)}건 (require_any 적용)", flush=True)
            before = len(deals)
    if excludes:
        deals = [d for d in deals if not any(e in d["title"].lower() for e in excludes)]
        if len(deals) != before:
            print(f"  카드 → {len(deals)}건 (제외어 적용)", flush=True)
    if not require_any and not excludes:
        print(f"  카드 {len(deals)}건 수집", flush=True)

    # 만료(ended=True) 제외
    before = len(deals)
    deals = [d for d in deals if not d.get("ended", False)]
    if len(deals) != before:
        print(f"  만료 제외: {before} → {len(deals)}건", flush=True)

    # 날짜 필터
    cutoff = now - timedelta(days=stale_days)
    fresh = [d for d in deals if d.get("date") is None or d["date"] >= cutoff]
    if len(fresh) != len(deals):
        print(f"  날짜 필터 ({stale_days}일 이내): {len(deals)} → {len(fresh)}건", flush=True)

    # 동일 핫딜 중복 제거 (여러 사이트 공유)
    before = len(fresh)
    fresh = _dedup_cross_source(fresh)
    if len(fresh) != before:
        print(f"  교차 사이트 중복 제거: {before} → {len(fresh)}건", flush=True)
    return fresh


def _send_super_push(kid: str, kw: dict, super_hits: list[dict]) -> bool:
    """찐 특가 전용 푸시 — 화려한 본문 + 전용 페이지 deep link + 자동 음성용 플래그."""
    if not send_push or not super_hits:
        return True
    unit = kw.get("unit", "개")
    d0 = super_hits[0]
    ppu = d0.get("price_per_unit") or 0
    brand = d0.get("brand_name") or kw.get("name", "")
    title = f"🚨🔥⚡ 찐 특가! {brand} {ppu:,}원/{unit}"
    lines = ["⚡⚡⚡ 역대급 가격! ⚡⚡⚡"]
    for d in super_hits[:3]:
        p = d.get("price_per_unit") or 0
        b = d.get("brand_name") or ""
        b_s = f"[{b}] " if b and b != kw.get("name") else ""
        lines.append(f"{b_s}{p:,}원/{unit} — {d['title'][:25]}")
    body = "\n".join(lines)
    # 알림 클릭 → 전용 페이지 (그 핫딜 강조)
    first_id = super_hits[0]["id"]
    super_url = f"{SUPER_PAGE_URL}?id={first_id}"
    actions = [{"action": "view", "title": "🚨 찐 특가 보기", "url": super_url}]
    try:
        ok, fail = send_push(
            "super", title, body, url=super_url, actions=actions,
            require_interaction=True,
        )
        return (ok > 0) or (ok + fail == 0)
    except Exception as e:
        print(f"  [WARN] 찐 특가 push 예외: {e}", flush=True)
        return False


def _send_push_for_keyword(kid: str, kw: dict, hits: list[dict]) -> bool:
    """키워드별 PWA Web Push 발송. 성공 시 True. sub_brands 있으면 본문에 brand 라벨 포함."""
    if not send_push or not hits:
        return True
    unit = kw.get("unit", "개")
    title = f"{kw['emoji']} {kw['name']} 핫딜 {len(hits)}건"
    body_lines = []
    for d in hits[:5]:
        if d.get("price_per_unit"):
            age = humanize_age(d.get("date"))
            src = d.get("source") or ""
            brand = d.get("brand_name") or ""
            age_s = f" · {age}" if age else ""
            src_s = f" · {src}" if src else ""
            brand_s = f"[{brand}] " if brand and brand != kw.get("name") else ""
            body_lines.append(f"{brand_s}{d['price_per_unit']:,}원/{unit}{age_s}{src_s} — {d['title'][:25]}")
    body = "\n".join(body_lines) or "새 핫딜"

    actions = []
    if len(hits) >= 2:
        actions.append({"action": "deal2", "title": "🛒 #2", "url": hits[1].get("external") or hits[1]["url"]})
    actions.append({"action": "report", "title": "📋 리포트", "url": f"{REPORT_BASE_URL}#kw-{kid}"})

    first_url = hits[0].get("external") or hits[0]["url"]
    try:
        ok, fail = send_push(kid, title, body, url=first_url, actions=actions)
        return (ok > 0) or (ok + fail == 0)
    except Exception as e:
        print(f"  [WARN] push 예외: {e}", flush=True)
        return False


def main() -> None:
    now = datetime.now()
    force = os.environ.get("FORCE_RUN", "").lower() in ("1", "true", "yes")
    if not force and not (9 <= now.hour < 20):
        print(f"[SKIP] 운영 시간 외 ({now.strftime('%H:%M')})", flush=True)
        sys.exit(0)

    cfg_data = load_keywords()
    keywords = cfg_data.get("keywords", [])
    settings = cfg_data.get("scan_settings", {})
    timeout   = settings.get("request_timeout_sec", 15)
    inter_delay = settings.get("delay_between_keywords_sec", 1.5)
    max_posts = settings.get("max_posts_per_keyword", 50)

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    gh_token = os.environ.get("GH_TOKEN") or config.get("github_token", "")
    gh_owner = os.environ.get("GH_OWNER") or config.get("github_owner", "")
    gh_repo  = os.environ.get("GH_REPO")  or config.get("github_repo", "")
    gh_path  = os.environ.get("GH_PATH")  or config.get("github_deals_path", "deals.html")

    print(f"\n[{now.strftime('%Y-%m-%d %H:%M')}] 알구몬 키워드 모니터 시작 — {len(keywords)} 키워드", flush=True)

    seen = load_seen()
    categories = cfg_data.get("categories", [])
    sections: list[dict] = []
    summary_rows: list[dict] = []
    super_collect: list[dict] = []   # 찐 특가 전용 페이지용 — 모든 키워드의 super_hits 누적
    total_new = 0

    for i, kw in enumerate(keywords):
        kid = kw["id"]
        print(f"\n[{kid}] {kw.get('name', kid)} ...", flush=True)
        brands = _resolve_brands(kw)
        brand_results: list[dict] = []   # 각 brand 의 {brand, deals, stats, threshold, hits}
        all_hits: list[dict] = []        # 키워드 단위 통합 hits (알림용)

        for brand in brands:
            bid = brand.get("brand_id") or kid
            bname = brand.get("brand_name") or brand.get("name", kid)
            queries = brand.get("search_queries") or [brand.get("search_query", "")]
            queries = [q for q in queries if q]
            single_item = brand.get("single_item", False)
            ppu_min = brand.get("ppu_min", DEFAULT_PPU_MIN)
            ppu_max = brand.get("ppu_max", DEFAULT_PPU_MAX)
            min_samples = brand.get("min_samples", 6)
            alert_pct = brand.get("alert_pct", 80)
            iqr_k = brand.get("outlier_iqr_k", 1.5)
            verify_links = brand.get("verify_links", True)
            unit = brand.get("unit", "개")

            label = f"  ↳ {bname}" if len(brands) > 1 else ""
            if label:
                print(label, flush=True)
            deals = fetch_search_multi(queries, timeout=timeout, delay=1.0)[:max_posts]
            deals = _filter_deals(deals, brand, now)
            for d in deals:
                apply_sanity(d, single_item=single_item, ppu_min=ppu_min, ppu_max=ppu_max)
                d["unit"] = unit
                d["brand_name"] = bname

            unit_prices = [d["price_per_unit"] for d in deals if d.get("price_per_unit")]
            stats = compute_stats(unit_prices, iqr_k=iqr_k)
            print(f"  단가 {len(unit_prices)}건, outlier 제거 후 {stats['n_clean']}건", flush=True)

            super_pct = brand.get("super_alert_pct", DEFAULT_SUPER_ALERT_PCT)
            threshold = 0
            super_threshold = 0
            hits: list[dict] = []
            super_hits: list[dict] = []
            # 모든 deal 에 본 brand 의 threshold 기록 (UI 통합 시 카드 배지 표시용)
            for d in deals:
                d["_brand_threshold"] = 0
            if stats["n_clean"] >= min_samples:
                threshold = int(stats["p25"] * alert_pct / 100)
                super_threshold = int(stats["p25"] * super_pct / 100)
                for d in deals:
                    d["_brand_threshold"] = threshold
                print(f"  P25={stats['p25']:,} → 핫딜 ≤{threshold:,} · 찐특가 ≤{super_threshold:,}원/{unit}", flush=True)
                candidates = [d for d in deals
                              if d.get("price_per_unit") and d["price_per_unit"] <= threshold and d["id"] not in seen]
                for d in candidates:
                    d["is_super"] = d["price_per_unit"] <= super_threshold
                    (super_hits if d["is_super"] else hits).append(d)
                if super_hits:
                    print(f"  🚨 찐 특가 {len(super_hits)}건 / 일반 핫딜 {len(hits)}건", flush=True)
                else:
                    print(f"  후보 핫딜 {len(hits)}건", flush=True)
                if verify_links and (hits or super_hits):
                    def filter_alive(lst):
                        out = []
                        for d in lst:
                            if verify_alive(d["url"]):
                                out.append(d)
                            time.sleep(0.5)
                        return out
                    n_before = len(hits) + len(super_hits)
                    hits = filter_alive(hits)
                    super_hits = filter_alive(super_hits)
                    n_after = len(hits) + len(super_hits)
                    if n_after != n_before:
                        print(f"  링크 검증: {n_before} → {n_after}건 살아있음", flush=True)
            else:
                print(f"  샘플 부족 ({stats['n_clean']} < {min_samples}) — 알림 보류", flush=True)

            brand_results.append({
                "brand": brand, "deals": deals, "stats": stats,
                "threshold": threshold, "super_threshold": super_threshold,
                "hits": hits, "super_hits": super_hits,
            })
            all_hits.extend(hits)
            all_hits.extend(super_hits)

            if len(brands) > 1:
                time.sleep(0.5)  # brand 사이 짧은 delay

        sections.append({
            "category": kw.get("category", "other"),
            "id": kid,
            "html": _keyword_section(kw, brand_results),
        })

        # super_hits 수집 (찐 특가 전용 페이지용)
        for br in brand_results:
            for d in br.get("super_hits", []):
                super_collect.append({
                    "kw": kw, "brand": br["brand"], "deal": d,
                    "threshold": br["threshold"], "super_threshold": br["super_threshold"],
                    "p25": br["stats"]["p25"],
                })

        # 종합표 — sub_brands 있으면 가장 잘 산출된 brand 기준, 없으면 단일 brand
        best_br = max(brand_results, key=lambda r: r["stats"]["n_clean"]) if brand_results else None
        if best_br:
            summary_rows.append({
                "id": kid, "emoji": kw.get("emoji", ""), "name": kw.get("name", kid),
                "category": kw.get("category", "other"),
                "p25": best_br["stats"]["p25"],
                "n_clean": best_br["stats"]["n_clean"],
                "min_samples": best_br["brand"].get("min_samples", 6),
                "threshold": best_br["threshold"],
                "has_brands": len(brands) > 1,
            })

        # 알림 분리 — 찐 특가는 별도 강조 push, 일반은 기존 방식
        super_in_kw = [d for d in all_hits if d.get("is_super")]
        regular_in_kw = [d for d in all_hits if not d.get("is_super")]
        push_ok_all = True
        if super_in_kw:
            ok = _send_super_push(kid, kw, super_in_kw)
            push_ok_all = push_ok_all and ok
        if regular_in_kw:
            ok = _send_push_for_keyword(kid, kw, regular_in_kw)
            push_ok_all = push_ok_all and ok
        if all_hits:
            if push_ok_all:
                seen.update(d["id"] for d in all_hits)
                total_new += len(all_hits)
            else:
                print("  [WARN] 일부 push 실패 — seen 미갱신, 다음 실행에서 재시도", flush=True)

        if i < len(keywords) - 1:
            time.sleep(inter_delay)

    checked_at = now.strftime("%Y-%m-%d %H:%M")
    html = render_html(sections, checked_at, summary_rows, categories)
    REPORT_FILE.write_text(html, encoding="utf-8")
    print(f"\n[HTML] 리포트 저장: {REPORT_FILE}", flush=True)

    # 찐 특가 전용 페이지
    super_html = render_super_html(super_collect, checked_at)
    SUPER_FILE.write_text(super_html, encoding="utf-8")
    print(f"[HTML] 찐 특가 페이지: {SUPER_FILE}", flush=True)

    if gh_token and gh_owner and gh_repo:
        try:
            url = deploy_to_github(html, gh_token, gh_owner, gh_repo, gh_path)
            print(f"[URL] {url}", flush=True)
            url2 = deploy_to_github(super_html, gh_token, gh_owner, gh_repo, "super-deals.html")
            print(f"[URL] {url2}", flush=True)
        except Exception as e:
            print(f"[ERROR] GitHub 배포 실패: {e}", flush=True)

    save_seen(seen)
    print(f"\n[완료] 새 핫딜 총 {total_new}건  ({datetime.now().strftime('%H:%M:%S')})", flush=True)


if __name__ == "__main__":
    main()
