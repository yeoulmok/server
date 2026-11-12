INSERT INTO valleys (name, region, lat, lon)
VALUES ('금대리 계곡', '원주시 판부면', 37.278313, 128.019563);

INSERT INTO stations (source, code, name, kind, lat, lon) VALUES
  ('hrfco', '10064075', '원주시(신촌리)', 'rainfall',   37.296944, 127.971389),
  ('hrfco', '1006664',  '원주시(동부교)', 'waterlevel', NULL, NULL);

-- 11/12: 신촌리 우량계는 자료를 보내지 않아 계곡 연결에서 뺌 (수집은 계속). 상류 강수는 아래 신림 AWS
INSERT INTO valley_stations (valley_id, station_id, role)
SELECT v.id, s.id, 'downstream_water'
FROM valleys v, stations s
WHERE v.name = '금대리 계곡' AND s.source = 'hrfco' AND s.kind = 'waterlevel';

INSERT INTO valley_thresholds (valley_id, level, rain_1h_mm, water_rise_m)
SELECT v.id, t.level, t.rain, t.rise
FROM valleys v,
  (VALUES ('caution', 15.0, 0.10),
          ('danger', 25.0, 0.20),
          ('evacuate', 40.0, 0.30)) AS t(level, rain, rise)
WHERE v.name = '금대리 계곡';

-- 11/9: 신촌리 우량계(HRFCO)가 자료를 보내지 않아 기상청 신림 AWS를 상류 강수로 추가
INSERT INTO stations (source, code, name, kind) VALUES
  ('kma', '582', '원주 신림(AWS)', 'rainfall_1h');

INSERT INTO valley_stations (valley_id, station_id, role)
SELECT v.id, s.id, 'upstream_rain'
FROM valleys v, stations s
WHERE v.name = '금대리 계곡' AND s.source = 'kma' AND s.code = '582';
