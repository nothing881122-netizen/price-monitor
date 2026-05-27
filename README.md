# price-monitor

알구몬 기반 키워드 핫딜 모니터 — GitHub Actions 로 KST 09~19시 매 2시간마다 자동 실행.

- **데이터 소스**: algumon.com (여러 핫딜 커뮤니티 통합)
- **수집 방식**: SvelteKit `__data.json` endpoint (HTML 보다 데이터 풍부 + 만료 여부 포함)
- **임계값 산출**: 키워드별 단가 통계 IQR outlier 제거 → P25 × alert_pct% 자동
- **알림**: PWA Web Push (`push_notify.py` 모듈)
- **리포트**: GitHub Pages 의 `flight-deals/deals.html` 자동 push

## 주요 파일
| 파일 | 역할 |
|---|---|
| `price_monitor.py` | 메인 스크립트 |
| `push_notify.py` | PWA Web Push 발송 모듈 (pywebpush) |
| `keywords.json` | 모니터링할 키워드 목록 + 키워드별 옵션 |
| `seen_ids.json` | 알림 중복 방지 — GitHub Actions가 자동 커밋 |
| `.github/workflows/monitor.yml` | Actions cron 정의 |
| `config.json` | 로컬 수동 실행용 토큰 (gitignored) |

## 키워드 옵션 (`keywords.json`)
- `search_queries`: 검색어 배열 (한·영 혼용 가능)
- `require_any`: 제목에 반드시 포함되어야 할 단어 (양성 필터)
- `exclude`: 제외어
- `single_item`: 가전제품 등 1개 단위 상품 → 수량 무시, 총가를 단가로
- `ppu_min` / `ppu_max`: 단가 sanity 범위
- `alert_pct`: 임계값 = P25 × pct / 100 (기본 80)
- `min_samples`: 통계 산출 최소 표본 수
- `stale_days`: 며칠 이내 게시물만 통계에 사용
- `verify_links`: 알림 직전 GET 검증 (죽은 링크 제외)
