-- ============================================================
--  TransitFlow PostgreSQL Schema
--  Seed data is loaded separately by: python skeleton/seed_postgres.py
--
--  TWO ROLES:
--    1. Relational  → dual-network transit data you design below
--    2. Vector      → policy documents for RAG (provided — do not modify)
-- ============================================================

-- ============================================================
--  STUDENT TASK — Design and create your relational tables here
--
--  Start from the mock data in train-mock-data/:
--    metro_stations.json, national_rail_stations.json
--    metro_schedules.json, national_rail_schedules.json
--    national_rail_seat_layouts.json
--    registered_users.json
--    bookings.json, metro_travel_history.json
--    payments.json, feedback.json
--
--  Think about:
--    - What tables do you need?
--    - What columns and data types?
--    - Which fields are primary keys? Which are foreign keys?
--    - What constraints make sense?
--
--  Apply your schema with:
--    docker-compose down -v && docker-compose up -d
-- ============================================================

-- ============================================================
--  1. 基礎建設層（獨立主表：車站與使用者）
-- ============================================================

CREATE TABLE IF NOT EXISTS metro_stations (
    -- VARCHAR PK: station IDs are domain-defined codes (e.g. MS01) assigned
    -- by the transit authority; a surrogate SERIAL/UUID would add no value and
    -- would make the agent's tool calls harder to debug.
    station_id    VARCHAR(10)  PRIMARY KEY,
    name          VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS national_rail_stations (
    -- Same rationale as metro_stations: natural business key, stable and short.
    station_id    VARCHAR(10) PRIMARY KEY,
    name          VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    -- VARCHAR PK: user IDs come from the source JSON (e.g. RU01) and are
    -- referenced by the LLM agent directly; a SERIAL would break those references.
    user_id         VARCHAR(10)  PRIMARY KEY,
    first_name      VARCHAR(100) NOT NULL,
    surname         VARCHAR(100) NOT NULL,
    email           VARCHAR(255) UNIQUE NOT NULL,
    -- Stores bcrypt hash (60 chars); VARCHAR(255) gives headroom for future algorithms.
    password_hash   VARCHAR(255) NOT NULL,
    phone           VARCHAR(20),
    date_of_birth   DATE,
    secret_question VARCHAR(255),
    secret_answer   VARCHAR(255),
    -- Soft delete flag: set FALSE instead of deleting the row (see DELETION STRATEGY above).
    is_active       BOOLEAN      DEFAULT TRUE,
    registered_at   TIMESTAMPTZ  DEFAULT NOW()
);

-- ============================================================
--  2. 時刻表層
-- ============================================================

CREATE TABLE IF NOT EXISTS national_rail_schedules (
    -- VARCHAR PK: schedule IDs are domain codes (e.g. NR_SCH01) used by the agent.
    schedule_id                    VARCHAR(20) PRIMARY KEY,
    line                           VARCHAR(10) NOT NULL,          -- 'NR1', 'NR2'
    service_type                   VARCHAR(20) NOT NULL,          -- 'normal', 'express'
    direction                      VARCHAR(20) NOT NULL,          -- 'northbound', 'southbound'
    -- RESTRICT: deleting a station that a schedule originates/terminates at should
    -- be blocked — the schedule is meaningless without its endpoints.
    origin_station_id              VARCHAR(10) REFERENCES national_rail_stations(station_id) ON DELETE RESTRICT,
    destination_station_id         VARCHAR(10) REFERENCES national_rail_stations(station_id) ON DELETE RESTRICT,
    first_train_time               TIME NOT NULL,
    last_train_time                TIME NOT NULL,
    frequency_min                  INT NOT NULL,
    standard_base_fare_usd         DECIMAL(10, 2) NOT NULL,
    standard_per_stop_rate_usd     DECIMAL(10, 2) NOT NULL,
    first_base_fare_usd            DECIMAL(10, 2) NOT NULL,
    first_per_stop_rate_usd        DECIMAL(10, 2) NOT NULL
);

-- Junction table: normalises the many-to-many between schedules and stations.
-- stop_order makes the sequence queryable without parsing arrays.
CREATE TABLE IF NOT EXISTS national_rail_schedule_stops (
    -- CASCADE: if a schedule is removed, its stop records are meaningless alone.
    schedule_id                 VARCHAR(20) REFERENCES national_rail_schedules(schedule_id) ON DELETE CASCADE,
    -- RESTRICT: removing a station that is a stop on a live schedule should be blocked.
    station_id                  VARCHAR(10) REFERENCES national_rail_stations(station_id) ON DELETE RESTRICT,
    stop_order                  INT NOT NULL,
    travel_time_from_origin_min INT NOT NULL DEFAULT 0,
    PRIMARY KEY (schedule_id, station_id)
);

-- Composite PK (schedule_id, seat_id): seat codes like 'B05' repeat across schedules,
-- so the seat is only unique within a given schedule.
CREATE TABLE IF NOT EXISTS national_rail_seat_layouts (
    -- CASCADE: seat layout is owned by the schedule; delete schedule → delete layout.
    schedule_id    VARCHAR(20) REFERENCES national_rail_schedules(schedule_id) ON DELETE CASCADE,
    seat_id        VARCHAR(10) NOT NULL,                  -- 'A01', 'B05'
    coach          VARCHAR(5)  NOT NULL,
    fare_class     VARCHAR(20) NOT NULL,
    row            INT,
    col            VARCHAR(5),
    PRIMARY KEY (schedule_id, seat_id)
);

-- Normalises operates_on from the source JSON (was an array) into rows.
CREATE TABLE IF NOT EXISTS schedule_operating_days (
    -- CASCADE: operating day records are owned by the schedule.
    schedule_id    VARCHAR(20) REFERENCES national_rail_schedules(schedule_id) ON DELETE CASCADE,
    day            VARCHAR(3)  NOT NULL,                  -- 'mon'~'sun'
    PRIMARY KEY (schedule_id, day)
);

-- ----

CREATE TABLE IF NOT EXISTS metro_schedules (
    -- VARCHAR PK: same rationale as national_rail_schedules.
    schedule_id            VARCHAR(20) PRIMARY KEY,
    line                   VARCHAR(10) NOT NULL,          -- 'M1', 'M2'
    direction              VARCHAR(20) NOT NULL,          -- 'northbound', 'southbound'
    -- RESTRICT: schedule endpoints must exist.
    origin_station_id      VARCHAR(10) REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
    destination_station_id VARCHAR(10) REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
    first_train_time       TIME NOT NULL,
    last_train_time        TIME NOT NULL,
    frequency_min          INT NOT NULL,
    base_fare_usd          DECIMAL(10, 2) NOT NULL,
    per_stop_rate_usd      DECIMAL(10, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS metro_schedule_stops (
    -- CASCADE: stop records are owned by the schedule.
    schedule_id                 VARCHAR(20) REFERENCES metro_schedules(schedule_id) ON DELETE CASCADE,
    -- RESTRICT: a station that is an active stop cannot be deleted.
    station_id                  VARCHAR(10) REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
    stop_order                  INT NOT NULL,
    travel_time_from_origin_min INT NOT NULL DEFAULT 0,
    PRIMARY KEY (schedule_id, station_id)
);

CREATE TABLE IF NOT EXISTS metro_schedule_operating_days (
    -- CASCADE: operating day records are owned by the schedule.
    schedule_id    VARCHAR(20) REFERENCES metro_schedules(schedule_id) ON DELETE CASCADE,
    day            VARCHAR(3)  NOT NULL,
    PRIMARY KEY (schedule_id, day)
);

-- ============================================================
--  3. 交易與互動層
-- ============================================================

CREATE TABLE IF NOT EXISTS national_rail_bookings (
    -- VARCHAR PK: booking IDs are generated with a readable prefix (BK-XXXXXX)
    -- so agents and support staff can reference them in conversation.
    booking_id             VARCHAR(20) PRIMARY KEY,
    -- SET NULL on user delete: booking records must be retained for accounting
    -- compliance even after a user requests account deletion (soft delete first,
    -- then anonymise; SET NULL is the last resort if the user row is hard-deleted).
    user_id                VARCHAR(10) REFERENCES users(user_id) ON DELETE SET NULL,
    -- RESTRICT: bookings reference live schedules; orphaning a booking is unsafe.
    schedule_id            VARCHAR(20) REFERENCES national_rail_schedules(schedule_id) ON DELETE RESTRICT,
    -- RESTRICT: station references must remain valid for journey reconstruction.
    origin_station_id      VARCHAR(10) REFERENCES national_rail_stations(station_id) ON DELETE RESTRICT,
    destination_station_id VARCHAR(10) REFERENCES national_rail_stations(station_id) ON DELETE RESTRICT,
    travel_date            DATE        NOT NULL,
    departure_time         TIME        NOT NULL,
    ticket_type            VARCHAR(20) NOT NULL,           -- 'single', 'return'
    fare_class             VARCHAR(20) NOT NULL,           -- 'standard', 'first'
    coach                  VARCHAR(5),
    seat_id                VARCHAR(10),
    stops_travelled        INT,
    amount_usd             DECIMAL(10, 2) NOT NULL,
    -- Soft delete: status = 'cancelled' instead of deleting the row.
    status                 VARCHAR(20) NOT NULL,           -- 'confirmed', 'cancelled', 'completed'
    booked_at              TIMESTAMPTZ,
    travelled_at           TIMESTAMPTZ,
    FOREIGN KEY (schedule_id, seat_id) REFERENCES national_rail_seat_layouts(schedule_id, seat_id)
);

CREATE TABLE IF NOT EXISTS metro_travel_history (
    -- VARCHAR PK: trip IDs use a readable prefix (MT-XXXXXX) for the same reason as bookings.
    trip_id                VARCHAR(20) PRIMARY KEY,
    -- SET NULL: retain travel history for analytics even if user is removed.
    user_id                VARCHAR(10) REFERENCES users(user_id) ON DELETE SET NULL,
    -- RESTRICT: schedule must exist to validate the journey record.
    schedule_id            VARCHAR(20) REFERENCES metro_schedules(schedule_id) ON DELETE RESTRICT,
    -- RESTRICT: station references must stay valid.
    origin_station_id      VARCHAR(10) REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
    destination_station_id VARCHAR(10) REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
    travel_date            DATE        NOT NULL,
    ticket_type            VARCHAR(20) NOT NULL,           -- 'single', 'day_pass'
    stops_travelled        INT,
    amount_usd             DECIMAL(10, 2) NOT NULL,
    status                 VARCHAR(20) NOT NULL,           -- 'completed'
    purchased_at           TIMESTAMPTZ,
    travelled_at           TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS payments (
    -- VARCHAR PK: payment IDs use a readable prefix (PM-XXXXXX).
    -- booking_id is intentionally NOT a FK: payments can reference both national_rail_bookings
    -- (BK-*) and metro_travel_history (MT-*), and PostgreSQL FKs cannot span two tables.
    -- Referential integrity is enforced at the application layer in execute_booking().
    payment_id  VARCHAR(20)    PRIMARY KEY,
    booking_id  VARCHAR(20)    NOT NULL,
    amount_usd  DECIMAL(10, 2) NOT NULL,
    method      VARCHAR(20)    NOT NULL,                   -- 'credit_card', 'ewallet', 'debit_card'
    status      VARCHAR(20)    NOT NULL,                   -- 'paid', 'refunded'
    paid_at     TIMESTAMPTZ    NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    -- VARCHAR PK: feedback IDs come from the source JSON (e.g. FB001).
    feedback_id VARCHAR(10) PRIMARY KEY,
    -- booking_id is not a FK for the same reason as in payments (cross-table reference).
    booking_id  VARCHAR(20),
    -- SET NULL: retain feedback for service quality analysis even if user is removed.
    user_id     VARCHAR(10) REFERENCES users(user_id) ON DELETE SET NULL,
    rating      INT CHECK (rating BETWEEN 1 AND 5),
    comment     TEXT,
    submitted_at TIMESTAMPTZ DEFAULT NOW()
);





-- ============================================================
--  VECTOR SCHEMA  (RAG / Help Desk) — do not modify
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS policy_documents (
    id          SERIAL       PRIMARY KEY,
    title       VARCHAR(200) NOT NULL,
    category    VARCHAR(50)  NOT NULL,  -- 'refund', 'booking', 'conduct'
    content     TEXT         NOT NULL,
    -- 768-dim  → Ollama nomic-embed-text (default)
    -- 3072-dim → Gemini gemini-embedding-001
    -- If you switch LLM_PROVIDER to gemini, change to vector(3072) and reset the database.
    embedding   vector(768),
    source_file VARCHAR(200),
    created_at  TIMESTAMPTZ  DEFAULT NOW()
);

-- Index for fast cosine similarity search
CREATE INDEX IF NOT EXISTS ON policy_documents USING hnsw (embedding vector_cosine_ops);
