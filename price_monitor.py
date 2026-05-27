#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
알구몬 키워드 기반 핫딜 모니터 (v3)

- keywords.json 의 각 키워드를 algumon.com 검색에 넣어 카드 수집
- 알구몬이 이미 계산한 단가(N x M원 표기)를 활용 → 우리 수량 파싱 불필요
- 캔당/개당 단가 통계(IQR outlier 제거 → 25% 백분위) → 임계값 산출
- 외부 기준가 의존 없음, 알구몬이 뽐뿌·클리앙·어미새 등 통합 수집
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

ALGUMON_SEARCH_URL = "https://www.algumon.com/n/deal?keyword={kw}"
ALGUMON_DATA_URL   = "https://www.algumon.com/n/deal/__data.json?keyword={kw}"
ALGUMON_DEAL_URL   = "https://www.algumon.com/n/deal/{id}"
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

# ─────────────────────────────────────────────
# 알구몬 카드 파서 — 정규식 기반 (Svelte SSR HTML)
# ─────────────────────────────────────────────

# 알구몬은 카드 컨테이너로 deal-feed-card OR deal-row 두 클래스 사용 (UI 변종)
# 둘 다 매치해야 전체 카드 잡힘
_CARD_SPLIT_RE = re.compile(r'class="(?:deal-feed-card|deal-row)[^"]*"', re.IGNORECASE)
# 카드 내 정보 추출 패턴
_DEAL_ID_RE     = re.compile(r'/n/deal/(\d+)')
_REDIRECT_RE    = re.compile(r'href="(https?://(?:www\.)?algumon\.com/l/d/\d+\?[^"]+)"')
_TITLE_ALT_RE   = re.compile(r'<img[^>]*alt="([^"]+)"[^>]*class="w-full[^>]*"', re.IGNORECASE)
# 가격 텍스트: "46,941원 (18 x 2,607원)" 또는 "46,941원 (배송 무료)" 형태 등
# 단가 표기 "N x M원" 우선 추출, 없으면 총가만
_UNIT_RE        = re.compile(r'(\d{1,4})\s*[x×]\s*([\d,]+)\s*원')
_TOTAL_RE       = re.compile(r'([\d,]+)\s*원')
# 상대 시간 — "4시간 전", "2일 전" 등
_AGE_RE         = re.compile(r'(\d+)\s*(분|시간|일|주|개월|년)\s*전')
# 출처 — 뽐뿌, 클리앙, 어미새 등 사이트명. 보통 카드 헤더에 표시
_SOURCE_NAMES   = ["뽐뿌", "클리앙", "어미새", "쿨엔조이", "루리웹", "fmkorea", "에펨코리아",
                   "퀘이사존", "도탁스", "디시", "와이고수", "보드나라"]


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _parse_card(card_html: str) -> dict | None:
    """단일 카드 HTML에서 정보 추출."""
    m_id = _DEAL_ID_RE.search(card_html)
    if not m_id:
        return None
    deal_id = m_id.group(1)

    # 텍스트 일부 (앞 500자 정도)
    text = _strip_tags(card_html)

    # 제목 — img alt 우선
    m_title = _TITLE_ALT_RE.search(card_html)
    title = m_title.group(1).strip() if m_title else ""
    if not title:
        # fallback — 텍스트 첫 80자
        title = text[:80]

    # 외부 redirect URL (쇼핑몰로 이어짐, v=&t= 토큰 포함)
    m_redirect = _REDIRECT_RE.search(card_html)
    external_url = m_redirect.group(1).replace("&amp;", "&") if m_redirect else ""
    # 알구몬 상세 페이지 URL (만료 없음)
    detail_url = ALGUMON_DEAL_URL.format(id=deal_id)

    # 단가: "18 x 2,607원" — 알구몬이 직접 계산
    m_unit = _UNIT_RE.search(text)
    if m_unit:
        qty = int(m_unit.group(1))
        ppu = int(m_unit.group(2).replace(",", ""))
    else:
        qty = None
        ppu = None

    # 총가
    prices = [int(x.replace(",", "")) for x in _TOTAL_RE.findall(text)]
    prices = [p for p in prices if 500 <= p <= 5_000_000]
    total_price = prices[0] if prices else None  # 첫 큰 가격이 총가

    # 시간 — "4시간 전" 등
    m_age = _AGE_RE.search(text)
    age_text = m_age.group(0) if m_age else ""
    age_dt = _age_to_datetime(m_age) if m_age else None

    # 출처
    source = ""
    for s in _SOURCE_NAMES:
        if s in text:
            source = s
            break

    return {
        "id":          deal_id,
        "title":       title,
        "url":         detail_url,
        "external":    external_url,
        "source":      source,
        "total_price": total_price,
        "quantity":    qty,
        "price_per_unit": ppu,
        "age_text":    age_text,
        "date":        age_dt,
    }


def _age_to_datetime(m: re.Match) -> datetime:
    n = int(m.group(1))
    unit = m.group(2)
    now = datetime.now()
    delta = {
        "분":   timedelta(minutes=n),
        "시간": timedelta(hours=n),
        "일":   timedelta(days=n),
        "주":   timedelta(weeks=n),
        "개월": timedelta(days=30 * n),
        "년":   timedelta(days=365 * n),
    }[unit]
    return now - delta


def _resolve_ref(idx, pool: list, _stack: set | None = None):
    """SvelteKit 참조 풀기. idx 가 int면 pool[idx] 를 재귀 해석. 순환 방지."""
    if _stack is None:
        _stack = set()
    if not isinstance(idx, int):
        return idx
    if idx in _stack:
        return None
    _stack = _stack | {idx}
    if idx < 0 or idx >= len(pool):
        return None
    val = pool[idx]
    if isinstance(val, dict):
        return {k: _resolve_ref(v, pool, _stack) for k, v in val.items()}
    if isinstance(val, list):
        return [_resolve_ref(v, pool, _stack) for v in val]
    return val


_PERPRICE_RE = re.compile(r'(\d{1,4})\s*[x×]\s*([\d,]+)\s*원')


def _parse_deal_object(deal: dict) -> dict | None:
    """SvelteKit 참조가 풀린 deal dict 에서 표준 카드 정보 추출."""
    deal_id = deal.get("id")
    if deal_id is None:
        return None
    deal_id = str(deal_id)
    title = (deal.get("title") or "").strip()
    if not title:
        return None

    # 가격 — price 필드 또는 perPriceText 에서 추출
    price_text  = str(deal.get("price") or "")
    per_text    = str(deal.get("perPriceText") or "")
    # 총가
    total_match = re.search(r'([\d,]+)\s*원', price_text)
    total_price = int(total_match.group(1).replace(",", "")) if total_match else None
    # 단가 "(N x M원)" — perPriceText 우선
    qty, ppu = None, None
    for src in (per_text, price_text):
        m = _PERPRICE_RE.search(src)
        if m:
            qty = int(m.group(1))
            ppu = int(m.group(2).replace(",", ""))
            break

    # 출처 / 쇼핑몰
    source     = (deal.get("siteName") or "").strip()
    store_name = (deal.get("storeName") or "").strip()

    # URL — original 우선, cloak 없음
    original_url = (deal.get("originalUrl") or "").strip()
    detail_url   = ALGUMON_DEAL_URL.format(id=deal_id)

    # 만료
    ended = bool(deal.get("ended", False))

    # 날짜 — createdAt 은 ISO 형식
    age_dt = None
    created = deal.get("createdAt")
    if isinstance(created, str):
        try:
            # "2026-05-27T01:23:45.678Z" 또는 "...+09:00" 등
            iso = created.rstrip("Z").split(".")[0]
            age_dt = datetime.fromisoformat(iso)
        except Exception:
            age_dt = None

    return {
        "id":          deal_id,
        "title":       title,
        "url":         detail_url,
        "external":    original_url or detail_url,
        "source":      source,
        "store":       store_name,
        "total_price": total_price,
        "quantity":    qty,
        "price_per_unit": ppu,
        "date":        age_dt,
        "ended":       ended,
    }


def fetch_search(query: str, timeout: int = 15) -> list[dict]:
    """알구몬 JSON endpoint에서 검색 결과 추출 (HTML 보다 더 많은 deal + 만료 여부 포함)."""
    url = ALGUMON_DATA_URL.format(kw=urllib.parse.quote(query.encode("utf-8")))
    req = urllib.request.Request(url, headers={**HEADERS, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  [ERROR] '{query}' 검색 실패: {e}", flush=True)
        return []

    # nodes[1].data 가 deal 풀
    try:
        pool = data["nodes"][1]["data"]
    except (KeyError, IndexError, TypeError):
        return []
    if not isinstance(pool, list):
        return []

    cards: list[dict] = []
    seen_ids: set[str] = set()
    for item in pool:
        # deal 객체 식별: storeName + price + title 필드를 가짐
        if not isinstance(item, dict):
            continue
        if not all(k in item for k in ("storeName", "price", "title")):
            continue
        resolved = _resolve_ref(item, pool) if any(isinstance(v, int) for v in item.values()) else item
        # 위 _resolve_ref 가 item dict 자체에 동작 안 함 (값들이 int 인덱스라는 가정)
        # 다시: item의 각 필드 값(int 인덱스)을 풀어줌
        deal: dict = {}
        for k, v in item.items():
            deal[k] = _resolve_ref(v, pool) if isinstance(v, int) else v
        info = _parse_deal_object(deal)
        if info and info["id"] not in seen_ids:
            seen_ids.add(info["id"])
            cards.append(info)
    return cards


def fetch_search_multi(queries: list[str], timeout: int = 15, delay: float = 1.0) -> list[dict]:
    """여러 검색어로 결과 합치기 + ID 중복 제거."""
    seen_ids: set = set()
    merged: list[dict] = []
    for i, q in enumerate(queries):
        if i > 0:
            time.sleep(delay)
        for c in fetch_search(q, timeout=timeout):
            if c["id"] not in seen_ids:
                seen_ids.add(c["id"])
                merged.append(c)
    return merged


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
# 단가 sanity (식품 기본; 키워드별 override)
# ─────────────────────────────────────────────

DEFAULT_PPU_MIN = 200
DEFAULT_PPU_MAX = 30_000


def apply_sanity(card: dict, *, single_item: bool, ppu_min: int, ppu_max: int) -> dict:
    """single_item=True 이면 수량 무시, 총가를 단가로. sanity 범위 밖이면 ppu=None."""
    if single_item:
        card["quantity"] = 1
        card["price_per_unit"] = card.get("total_price")
    ppu = card.get("price_per_unit")
    if ppu is not None and not (ppu_min <= ppu <= ppu_max):
        card["price_per_unit"] = None
    return card


def humanize_age(dt: datetime | None, ref: datetime | None = None) -> str:
    if dt is None:
        return ""
    if ref is None:
        ref = datetime.now()
    s = int((ref - dt).total_seconds())
    if s < 0:
        return "방금"
    if s < 3600:
        return f"{max(1, s // 60)}분 전"
    if s < 86400:
        return f"{s // 3600}시간 전"
    return f"{s // 86400}일 전"


# ─────────────────────────────────────────────
# 링크 검증 (알구몬 상세 페이지)
# ─────────────────────────────────────────────

def verify_alive(deal_url: str, timeout: int = 8) -> bool:
    """알구몬 상세 페이지가 살아있는지 GET. '존재하지 않' 마커 또는 응답 너무 작으면 False."""
    req = urllib.request.Request(deal_url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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
    for marker in ("존재하지 않", "찾을 수 없", "삭제된", "Not Found"):
        if marker in html:
            print(f"    [DEAD] '{marker}' 마커 검출", flush=True)
            return False
    return True


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
    if d.get("price_per_unit") and threshold and d["price_per_unit"] <= threshold:
        badge = '<span class="badge-deal">🔥 알림 기준 이하</span>'
    if d.get("price_per_unit"):
        unit = d.get("unit", "개")
        qty = d.get("quantity") or 1
        total = d.get("total_price") or 0
        price_line = (
            f'<span class="price">{d["price_per_unit"]:,}원/{unit}</span> '
            f'<span class="meta">(총 {total:,}원 / {qty}{unit})</span>'
        )
    else:
        price_line = '<span class="price-unknown">가격 파싱 불가</span>'
    age = humanize_age(d.get("date"))
    age_html = f'<span class="age">{age}</span>' if age else ''
    src = d.get("source", "")
    src_html = f'<span class="src">📍 {src}</span>' if src else ''
    # 외부 redirect 우선, 없으면 알구몬 상세
    primary = d.get("external") or d.get("url")
    detail  = d.get("url")
    return f"""<div class="card">
  <p class="card-title">{d['title'][:80]} {age_html} {src_html}</p>
  <p class="card-price">{price_line} {badge}</p>
  <div class="card-actions">
    <a href="{primary}" target="_blank" class="btn btn-primary">🛒 쇼핑몰로</a>
    <a href="{detail}" target="_blank" class="btn">알구몬 상세</a>
  </div>
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
        top = [p for p in parsed if p.get("price_per_unit")][:5]
        if top:
            body += '<h4>최근 게시물</h4>'
            for d in top:
                body += deal_card_html(d, threshold)
    return f"""<section class="keyword" id="kw-{kw['id']}">
  <h2>{kw['emoji']} {kw['name']}</h2>
  <p class="kw-desc">{kw.get('description','')}</p>
  {body}
</section>"""


def reference_summary_html(rows: list[dict]) -> str:
    """헤더 아래 종합 기준가 표 — 모든 키워드의 P25 / 임계값 한 눈에."""
    if not rows:
        return ""
    tr_html = ""
    for r in rows:
        if r["n_clean"] >= r.get("min_samples", 6):
            p25  = f'{r["p25"]:,}원'
            thr  = f'{r["threshold"]:,}원'
            note = f'샘플 {r["n_clean"]}건'
        else:
            p25  = "—"
            thr  = "—"
            note = f'<span class="ref-pending">샘플 {r["n_clean"]}/{r.get("min_samples",6)} 부족</span>'
        anchor = f'#kw-{r["id"]}'
        tr_html += f"""<tr>
  <td><a href="{anchor}">{r['emoji']} {r['name']}</a></td>
  <td class="ref-num">{p25}</td>
  <td class="ref-num ref-thr">{thr}</td>
  <td class="ref-note">{note}</td>
</tr>"""
    return f"""<section class="ref-section">
  <h2 class="ref-title">📊 기준가 종합 (자체 산출)</h2>
  <p class="ref-source">알구몬 검색 결과를 자체 분석한 값입니다. pricewagon 등 외부 기준가 사용 안 함 — 시장 변동에 자동 적응.</p>
  <table class="ref-table">
    <thead>
      <tr>
        <th>카테고리</th>
        <th>P25 (시장 25% 구간)</th>
        <th>알림 기준</th>
        <th>상태</th>
      </tr>
    </thead>
    <tbody>{tr_html}</tbody>
  </table>
</section>"""


def generate_html(sections: list[str], checked_at: str, ref_rows: list[dict] | None = None) -> str:
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
.ref-section{background:white;border-radius:16px;padding:18px;margin-bottom:14px;box-shadow:0 1px 4px rgba(140,70,20,.08)}
.ref-title{font-size:17px;font-weight:700;margin-bottom:6px;color:#7C4530}
.ref-source{font-size:12px;color:#8B7355;background:#FAE2D4;border-left:3px solid #D0663C;border-radius:6px;padding:8px 10px;margin-bottom:12px;line-height:1.5}
.ref-table{width:100%;border-collapse:collapse;background:#FFFCF8;border-radius:8px;overflow:hidden}
.ref-table th{background:#FAE2D4;color:#7C4530;font-size:12px;padding:8px 10px;text-align:left;font-weight:600}
.ref-table td{padding:8px 10px;font-size:13px;border-top:1px solid #FAF0E8}
.ref-table a{color:#B0502C;text-decoration:none;font-weight:600}
.ref-table a:hover{text-decoration:underline}
.ref-num{text-align:right;font-variant-numeric:tabular-nums}
.ref-thr{color:#B0502C;font-weight:700}
.ref-note{font-size:12px;color:#8B7355}
.ref-pending{color:#A89070;font-style:italic}
"""
    ref_block = reference_summary_html(ref_rows or [])
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
    <p class="subtitle">{checked_at} · 알구몬 통합 검색</p>
  </div>
  <div class="main">
    {ref_block}
    {"".join(sections)}
  </div>
  <div class="footer">알구몬 통합 핫딜 · 키워드별 P25 자체 산출</div>
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
        if d.get("price_per_unit"):
            age = humanize_age(d.get("date"))
            src = d.get("source", "")
            age_s = f" · {age}" if age else ""
            src_s = f" · {src}" if src else ""
            lines.append(f"[{d['price_per_unit']:,}원/{d.get('unit','개')}{age_s}{src_s}] {d['title'][:40]}")
    lines.append(f"\n알림 기준: ≤ {threshold:,}원/{keyword.get('unit','개')}")
    if report_url:
        lines.append(f"리포트: {report_url}")

    actions = []
    if len(hits) >= 2:
        actions.append({"action": "view", "label": "🛒 #2 쇼핑몰", "url": hits[1].get("external") or hits[1]["url"]})
    if report_url:
        actions.append({"action": "view", "label": "📋 전체 리포트", "url": report_url})
    click = hits[0].get("external") if hits else (report_url or "https://www.algumon.com/")
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
    # utf-8-sig 로 BOM 처리 (PowerShell이 추가한 경우 대비)
    return json.loads(KEYWORDS_FILE.read_text(encoding="utf-8-sig"))


def main():
    now = datetime.now()
    force = os.environ.get("FORCE_RUN", "").lower() in ("1", "true", "yes")
    if not force and not (9 <= now.hour < 20):
        print(f"[SKIP] 운영 시간 외 ({now.strftime('%H:%M')})", flush=True)
        sys.exit(0)

    cfg_data = load_keywords()
    keywords = cfg_data.get("keywords", [])
    settings = cfg_data.get("scan_settings", {})
    timeout  = settings.get("request_timeout_sec", 15)
    delay    = settings.get("delay_between_keywords_sec", 1.5)
    max_posts = settings.get("max_posts_per_keyword", 30)

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    ntfy_topic = os.environ.get("NTFY_TOPIC") or config.get("ntfy_topic", "")
    gh_token   = os.environ.get("GH_TOKEN")   or config.get("github_token", "")
    gh_owner   = os.environ.get("GH_OWNER")   or config.get("github_owner", "")
    gh_repo    = os.environ.get("GH_REPO")    or config.get("github_repo", "")
    gh_path    = os.environ.get("GH_PATH")    or config.get("github_deals_path", "deals.html")

    print(f"\n[{now.strftime('%Y-%m-%d %H:%M')}] 알구몬 키워드 모니터 시작 — {len(keywords)} 키워드", flush=True)

    seen = load_seen()
    sections: list[str] = []
    ref_rows: list[dict] = []
    total_new_hits = 0

    for i, kw in enumerate(keywords):
        kid = kw["id"]
        queries = kw.get("search_queries") or [kw.get("search_query", "")]
        queries = [q for q in queries if q]
        excludes = [e.lower() for e in kw.get("exclude", [])]
        require_any = [t.lower() for t in kw.get("require_any", [])]
        single_item = kw.get("single_item", False)
        ppu_min = kw.get("ppu_min", DEFAULT_PPU_MIN)
        ppu_max = kw.get("ppu_max", DEFAULT_PPU_MAX)
        stale_days = kw.get("stale_days", 30)
        verify_links = kw.get("verify_links", True)
        print(f"\n[{kid}] 검색어 {queries}...", flush=True)
        cards = fetch_search_multi(queries, timeout=timeout, delay=1.0)[:max_posts]
        before = len(cards)
        if require_any:
            cards = [c for c in cards if any(t in c["title"].lower() for t in require_any)]
            if len(cards) != before:
                print(f"  카드 {before} → {len(cards)}건 (require_any 적용)", flush=True)
                before = len(cards)
        if excludes:
            cards = [c for c in cards if not any(e in c["title"].lower() for e in excludes)]
            if len(cards) != before:
                print(f"  카드 → {len(cards)}건 (제외어 적용)", flush=True)
        if not require_any and not excludes:
            print(f"  카드 {len(cards)}건 수집", flush=True)

        # 만료된 deal 제외 (알구몬이 ended=True 로 표시한 것)
        before = len(cards)
        cards = [c for c in cards if not c.get("ended", False)]
        if len(cards) != before:
            print(f"  만료 제외: {before} → {len(cards)}건", flush=True)

        # 날짜 필터 (stale_days 이내)
        cutoff = now - timedelta(days=stale_days)
        fresh = [c for c in cards if c.get("date") is None or c["date"] >= cutoff]
        if len(fresh) != len(cards):
            print(f"  날짜 필터 ({stale_days}일 이내): {len(cards)} → {len(fresh)}건", flush=True)
        cards = fresh

        # sanity 적용
        for c in cards:
            apply_sanity(c, single_item=single_item, ppu_min=ppu_min, ppu_max=ppu_max)
            c["unit"] = kw.get("unit", "개")

        unit_prices = [c["price_per_unit"] for c in cards if c.get("price_per_unit")]
        stats = stats_with_iqr(unit_prices, k=kw.get("outlier_iqr_k", 1.5))
        print(f"  단가 {len(unit_prices)}건, outlier 제거 후 {stats['n_clean']}건", flush=True)

        threshold = 0
        hits: list[dict] = []
        if stats["n_clean"] >= kw.get("min_samples", 6):
            threshold = int(stats["p25"] * kw.get("alert_pct", 80) / 100)
            print(f"  P25={stats['p25']:,} → 임계값 {threshold:,}원/{kw.get('unit','개')}", flush=True)
            for c in cards:
                if c.get("price_per_unit") and c["price_per_unit"] <= threshold and c["id"] not in seen:
                    hits.append(c)
            print(f"  후보 핫딜 {len(hits)}건", flush=True)

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

        sections.append(keyword_section_html(kw, cards, stats, threshold, hits))
        ref_rows.append({
            "id":          kid,
            "emoji":       kw.get("emoji", ""),
            "name":        kw.get("name", kid),
            "unit":        kw.get("unit", "개"),
            "p25":         stats["p25"],
            "n_clean":     stats["n_clean"],
            "min_samples": kw.get("min_samples", 6),
            "threshold":   threshold,
        })

        if hits:
            ntfy_ok = True
            if ntfy_topic:
                ntfy_ok = send_ntfy(ntfy_topic, kw, hits, threshold, "")

            push_ok = True
            if send_push:
                push_title = f"{kw['emoji']} {kw['name']} 핫딜 {len(hits)}건"
                lines = []
                for d in hits[:5]:
                    if d.get("price_per_unit"):
                        age = humanize_age(d.get("date"))
                        src = d.get("source", "")
                        age_s = f" · {age}" if age else ""
                        src_s = f" · {src}" if src else ""
                        lines.append(f"{d['price_per_unit']:,}원/{kw.get('unit','개')}{age_s}{src_s} — {d['title'][:30]}")
                push_body = "\n".join(lines) or "새 핫딜"
                push_actions = []
                if len(hits) >= 2:
                    push_actions.append({"action": "deal2", "title": "🛒 #2", "url": hits[1].get("external") or hits[1]["url"]})
                push_actions.append({"action": "report", "title": "📋 리포트", "url": "https://nothing881122-netizen.github.io/flight-deals/deals.html"})
                first_url = hits[0].get("external") or hits[0]["url"]
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
                print(f"  [WARN] 알림 채널 실패 — seen 미갱신", flush=True)

        if i < len(keywords) - 1:
            time.sleep(delay)

    checked_at = now.strftime("%Y-%m-%d %H:%M")
    html = generate_html(sections, checked_at, ref_rows=ref_rows)
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
