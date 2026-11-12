# Postman API 점검

`yeoulmok_api.postman_collection.json` — 명세 v0.2 중 구현된 8개 API를 확인하는 요청 17개와 자동 검사.

## 실행

1. Postman **Import** → 이 JSON 파일 선택
2. 컬렉션 `⋯` → **Run collection** → **Run**
3. 요청마다 통과(초록)·실패(빨강) 확인

변수: `baseUrl`(기본 `https://yeoulmok.ihaewon.com/api`), `valley_id`(기본 1), `device_id`(기기 등록 때 자동 저장)

## 구성

| 폴더 | 내용 |
|---|---|
| 1. 기본 조회 | `/health`, `/valleys`, `/valleys?q=`, `/valleys/{id}`, 없는 계곡 404 |
| 2. 기기·구독 흐름 | 기기 등록 → 알림 끄기·켜기 → 구독 → 내 계곡 → 해제 (순서대로 실행) |
| 3. 에러 처리 | 헤더 없음 400, 없는 기기 404, 없는 계곡 404 |

## 주의

- 실행하면 DB `devices`에 테스트 기기 1개가 남는다 (apns_token 없음 → 푸시 안 감, 구독은 마지막에 해제).
- 기기·구독 요청이 `{"detail":"Not Found"}`로 실패하면 서버에 최신 코드가 반영되지 않은 것 → 서버에서 `git pull && docker restart api judge`.
