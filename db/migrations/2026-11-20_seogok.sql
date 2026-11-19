-- 서곡리 용수골 계곡 추가 (#28). 하류 수위 관측소가 없어서 강수 + AI 2단 알람으로 판정 (#12).
-- 좌표는 지도에서 확인한 계곡 입구 값으로 넣는다 (근처 계곡 거리 계산에 씀).
-- 실행: docker exec -i db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v lat=37.xxxxxx -v lon=127.xxxxxx' < db/migrations/2026-11-20_seogok.sql
-- 여러 번 실행해도 같은 결과.

\if :{?lat}
\else
  \echo '좌표가 없어요: -v lat=… -v lon=… 를 붙여 주세요'
  \quit
\endif

INSERT INTO stations (source, code, name, kind)
SELECT 'hrfco', '10064070', '원주시(서곡초교)', 'rainfall'
WHERE NOT EXISTS (SELECT 1 FROM stations WHERE source = 'hrfco' AND code = '10064070');

INSERT INTO valleys (name, region, lat, lon)
SELECT '서곡리 용수골 계곡', '원주시 판부면', :lat, :lon
WHERE NOT EXISTS (SELECT 1 FROM valleys WHERE name = '서곡리 용수골 계곡');

-- 상류 강수만 연결 (하류 수위 없음 → 판정 엔진이 AI 확인 단계를 씀)
INSERT INTO valley_stations (valley_id, station_id, role)
SELECT v.id, s.id, 'upstream_rain'
FROM valleys v, stations s
WHERE v.name = '서곡리 용수골 계곡' AND s.source = 'hrfco' AND s.code = '10064070'
  AND NOT EXISTS (SELECT 1 FROM valley_stations vs WHERE vs.valley_id = v.id AND vs.station_id = s.id);

-- 기준값은 금대리와 같게 (1시간 강수 15/25/40mm. 수위 기준은 이 계곡엔 안 쓰임)
INSERT INTO valley_thresholds (valley_id, level, rain_1h_mm, water_rise_m)
SELECT n.id, t.level, t.rain_1h_mm, t.water_rise_m
FROM valleys n JOIN valley_thresholds t ON t.valley_id = (SELECT id FROM valleys WHERE name = '금대리 계곡')
WHERE n.name = '서곡리 용수골 계곡'
  AND NOT EXISTS (SELECT 1 FROM valley_thresholds x WHERE x.valley_id = n.id);

SELECT v.id, v.name, v.lat, v.lon, string_agg(s.name || ' (' || vs.role || ')', ', ') AS stations
FROM valleys v JOIN valley_stations vs ON vs.valley_id = v.id JOIN stations s ON s.id = vs.station_id
WHERE v.name = '서곡리 용수골 계곡' GROUP BY v.id;
