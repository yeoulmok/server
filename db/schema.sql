CREATE TABLE valleys (
  id         SERIAL PRIMARY KEY,
  name       TEXT NOT NULL,
  region     TEXT NOT NULL,
  lat        NUMERIC(9,6) NOT NULL,
  lon        NUMERIC(9,6) NOT NULL,
  is_active  BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE stations (
  id     SERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  code   TEXT NOT NULL,
  name   TEXT NOT NULL,
  kind   TEXT NOT NULL,
  lat    NUMERIC(9,6),
  lon    NUMERIC(9,6),
  attwl  NUMERIC(5,2),
  wrnwl  NUMERIC(5,2),
  almwl  NUMERIC(5,2),
  srswl  NUMERIC(5,2),
  UNIQUE (source, code, kind)
);

CREATE TABLE valley_stations (
  valley_id  INT NOT NULL REFERENCES valleys(id),
  station_id INT NOT NULL REFERENCES stations(id),
  role       TEXT NOT NULL,
  PRIMARY KEY (valley_id, station_id)
);

CREATE TABLE valley_thresholds (
  valley_id    INT NOT NULL REFERENCES valleys(id),
  level        TEXT NOT NULL,
  rain_1h_mm   NUMERIC(5,1),
  water_rise_m NUMERIC(5,2),
  PRIMARY KEY (valley_id, level)
);

CREATE TABLE observations (
  station_id   INT NOT NULL REFERENCES stations(id),
  observed_at  TIMESTAMPTZ NOT NULL,
  value        NUMERIC(8,2),
  collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (station_id, observed_at)
);

CREATE INDEX observations_time_idx ON observations (observed_at DESC);

CREATE TABLE valley_status (
  id             SERIAL PRIMARY KEY,
  valley_id      INT NOT NULL REFERENCES valleys(id),
  level          TEXT NOT NULL,
  rain_1h_mm     NUMERIC(5,1),
  water_rise_m   NUMERIC(5,2),
  observed_at    TIMESTAMPTZ,
  ai_explanation TEXT,
  ai_fallback    BOOLEAN NOT NULL DEFAULT FALSE,
  changed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE devices (
  id           TEXT PRIMARY KEY,
  apns_token   TEXT,
  platform     TEXT NOT NULL DEFAULT 'ios',
  notify_on    BOOLEAN NOT NULL DEFAULT TRUE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ
);

CREATE TABLE subscriptions (
  device_id  TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  valley_id  INT NOT NULL REFERENCES valleys(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (device_id, valley_id)
);
