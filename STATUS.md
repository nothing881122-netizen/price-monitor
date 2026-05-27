# STATUS — price-monitor

> 모바일/원격에서 바로 보는 라이브 진행상황. 작업 끝나면 갱신.
> 전체 시스템 맥락은 [`notifiers/WORK_LOG.md`](https://github.com/nothing881122-netizen/notifiers/blob/main/WORK_LOG.md) 참고.

**Last updated:** 2026-05-27

## Now (운영 중)
- GitHub Actions cron — KST **09 / 11 / 13 / 15 / 17 / 19** 시
- 데이터 소스: algumon.com (`__data.json`)
- 알림: PWA Web Push (`spam`, `tuna`, `garmin970`, `garmin570`, `mx-master-4` …)
- 결과물: [`flight-deals/deals.html`](https://nothing881122-netizen.github.io/flight-deals/deals.html) 으로 자동 push

## Next (작업 요청은 여기에 추가)
- [ ]

## Blocked
- 없음

## 빠른 링크
- Actions: https://github.com/nothing881122-netizen/price-monitor/actions
- 리포트: https://nothing881122-netizen.github.io/flight-deals/deals.html
- 키워드 설정: [`keywords.json`](./keywords.json)
- 관련 repo: [notifiers](https://github.com/nothing881122-netizen/notifiers) · [flight-deals](https://github.com/nothing881122-netizen/flight-deals)

## 키워드 추가/수정하는 법 (모바일에서도 가능)
1. [`keywords.json`](./keywords.json) 우측 상단 ✏
2. 항목 추가 — `search_queries`, `require_any`, `exclude`, `single_item`, `ppu_min`, `ppu_max`, `alert_pct` 등
3. Commit → 다음 cron 부터 반영

## 모바일에서 작업요청하는 법
1. 이 파일 우측 상단 ✏ → `Next` 섹션에 `- [ ] 할 일` 추가 → Commit
2. 또는 [Issues](https://github.com/nothing881122-netizen/price-monitor/issues/new) 생성
