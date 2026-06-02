#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web Push 알림 모듈 (PWA용)
- VAPID 키와 구독 정보를 읽어 pywebpush로 발송
- topic 필드를 payload에 포함 → PWA Service Worker가 필터링

설정 우선순위:
1. 환경변수 (GitHub Actions용)
   - WEB_PUSH_VAPID_PRIVATE  : VAPID 비공개키 PEM (개행 그대로)
   - WEB_PUSH_SUBSCRIPTIONS  : 구독 JSON 배열 문자열
                                예) [{"endpoint":"...","keys":{"p256dh":"...","auth":"..."}}, ...]
   - WEB_PUSH_SUBJECT        : VAPID claim subject (예: "mailto:foo@bar.com")
2. 로컬 파일 (개발/수동 실행용)
   - pwa/.vapid_keys.json    : private_key_pem + _subject 키
   - pwa/subscriptions.json  : 구독 배열
"""

from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from typing import Iterable

# Windows 콘솔 cp949 회피
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from pywebpush import webpush, WebPushException
    from py_vapid import Vapid01
except ImportError:
    webpush = None
    Vapid01 = None
    WebPushException = Exception

ROOT = Path(__file__).parent

def _find_pwa_dir() -> Path | None:
    """./pwa/ 또는 ../pwa/ 중 VAPID 키가 있는 곳을 찾음."""
    for candidate in [ROOT / "pwa", ROOT.parent / "pwa"]:
        if (candidate / ".vapid_keys.json").exists() or (candidate / "subscriptions.json").exists():
            return candidate
    return None

_PWA = _find_pwa_dir()
VAPID_FILE = (_PWA / ".vapid_keys.json") if _PWA else (ROOT / "pwa" / ".vapid_keys.json")
SUBS_FILE  = (_PWA / "subscriptions.json") if _PWA else (ROOT / "pwa" / "subscriptions.json")


def _load_config() -> tuple[str, str, list[dict]]:
    """VAPID 비공개키, subject, 구독 리스트를 로드. 못 찾으면 빈 값."""
    # 우선 환경변수
    vapid_pem = os.environ.get("WEB_PUSH_VAPID_PRIVATE", "")
    subject   = os.environ.get("WEB_PUSH_SUBJECT", "")
    subs_str  = os.environ.get("WEB_PUSH_SUBSCRIPTIONS", "")
    subs: list[dict] = []
    if subs_str:
        try:
            subs = json.loads(subs_str)
            if isinstance(subs, dict):  # 단일 구독을 dict로 받은 경우
                subs = [subs]
        except json.JSONDecodeError:
            print(f"  [WARN] WEB_PUSH_SUBSCRIPTIONS 파싱 실패", flush=True)

    # fallback: 로컬 파일
    if not vapid_pem and VAPID_FILE.exists():
        data = json.loads(VAPID_FILE.read_text(encoding="utf-8"))
        vapid_pem = data.get("private_key_pem", "")
        subject   = subject or data.get("_subject", "")
    if not subs and SUBS_FILE.exists():
        try:
            subs = json.loads(SUBS_FILE.read_text(encoding="utf-8"))
            if isinstance(subs, dict):
                subs = [subs]
        except json.JSONDecodeError:
            subs = []

    return vapid_pem, subject or "mailto:admin@example.com", subs


def send_push(
    topic: str,
    title: str,
    body: str,
    url: str = "",
    actions: list[dict] | None = None,
    require_interaction: bool = False,
) -> tuple[int, int]:
    """
    모든 구독자에게 push 발송.
    Returns: (성공 개수, 실패 개수)
    """
    if webpush is None:
        print("  [WARN] pywebpush 미설치 — push 스킵", flush=True)
        return (0, 0)

    vapid_pem, subject, subs = _load_config()
    if not vapid_pem:
        print("  [INFO] VAPID 키 없음 — push 스킵 (PWA 셋업 전)", flush=True)
        return (0, 0)
    if not subs:
        print("  [INFO] 구독자 없음 — push 스킵", flush=True)
        return (0, 0)

    payload = json.dumps({
        "topic":   topic,
        "title":   title,
        "body":    body,
        "url":     url,
        "actions": actions or [],
        "requireInteraction": require_interaction,
    }, ensure_ascii=False)

    # pywebpush 는 PEM 문자열을 직접 받지 못함 → Vapid 객체로 변환
    vapid_obj = Vapid01.from_pem(vapid_pem.encode("utf-8"))
    vapid_claims = {"sub": subject}
    ok, fail = 0, 0
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=vapid_obj,
                vapid_claims=dict(vapid_claims),  # 매 호출마다 새 dict (pywebpush가 mutate함)
                ttl=43200,  # 12h
            )
            ok += 1
        except WebPushException as e:
            fail += 1
            code = getattr(e.response, "status_code", "?") if hasattr(e, "response") else "?"
            print(f"  [WARN] push 실패 (HTTP {code}): {str(e)[:120]}", flush=True)
            # 410 Gone → 구독 만료, 정리 필요
            if hasattr(e, "response") and getattr(e.response, "status_code", 0) == 410:
                print(f"         → 만료된 구독, subscriptions.json에서 제거 권장", flush=True)
        except Exception as e:
            fail += 1
            print(f"  [WARN] push 예외: {e}", flush=True)

    if ok:
        print(f"  [OK] web push 발송 완료 ({ok}/{ok+fail}건)", flush=True)
    return (ok, fail)


def log_notify(system: str, topic: str, count: int, ok: int, fail: int,
               log_path: str | None = None) -> None:
    """알람 발송 1건을 notify_log.jsonl 에 append (append-only 대장).
    날짜별 누적 집계 / 아침 보고용. best-effort — 실패해도 모니터 흐름엔 영향 없음.

    system : "price" | "flight" | "scout" ...
    topic  : 토픽 id (spam, super, flight ...)
    count  : 그 push 에 담긴 deal/특가 수 (0 = '특가 없음' 류 알림)
    ok/fail: 구독자 발송 성공/실패 수
    """
    from datetime import datetime
    try:
        path = Path(log_path) if log_path else (ROOT / "notify_log.jsonl")
        now = datetime.now()
        row = {
            "ts":    now.replace(microsecond=0).isoformat(),
            "date":  now.strftime("%Y-%m-%d"),
            "sys":   system,
            "topic": topic,
            "count": count,
            "ok":    ok,
            "fail":  fail,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"  [WARN] notify_log 기록 실패: {e}", flush=True)


if __name__ == "__main__":
    # 직접 실행 시 테스트 푸시
    import sys
    topic = sys.argv[1] if len(sys.argv) > 1 else "test"
    title = sys.argv[2] if len(sys.argv) > 2 else "테스트 알림"
    body  = sys.argv[3] if len(sys.argv) > 3 else f"{topic} 토픽으로 보낸 테스트입니다."
    ok, fail = send_push(topic, title, body, url="https://nothing881122-netizen.github.io/flight-deals/app/")
    print(f"결과: 성공 {ok}, 실패 {fail}")
