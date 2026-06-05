"""
TransitFlow — PostgreSQL / Relational Database Layer
=====================================================
This module handles all queries to PostgreSQL.

TWO ROLES ARE SERVED HERE:
  1. Relational  → dual-network transit (metro + national rail),
                   availability, fares, bookings, seat selection
  2. Vector      → policy document similarity search (pgvector)

STUDENT TASK
------------
Design your schema in databases/relational/schema.sql, seed it with
skeleton/seed_postgres.py, then implement the query functions below.

Functions prefixed with `query_`  are read-only lookups called by the agent.
Functions prefixed with `execute_` are write operations (booking/cancellation).

The vector functions (query_policy_vector_search, store_policy_document)
are already implemented — do not modify them.
"""

from __future__ import annotations

import json
import random
import string
from datetime import datetime, timezone
from typing import Optional

import bcrypt
import psycopg2
import psycopg2.extras

from skeleton.config import PG_DSN, VECTOR_TOP_K, VECTOR_SIMILARITY_THRESHOLD


def _connect():
    """Return a new psycopg2 connection with autocommit enabled."""
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


def _gen_booking_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"BK-{suffix}"


def _gen_payment_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"PM-{suffix}"


# ── Example ───────────────────────────────────────────────────────────────────
# The block below shows the query pattern: open a cursor, run SQL, return rows.
# Use _connect() for read-only queries; for write operations use a manual
# connection with conn.commit() / conn.rollback() (see execute_booking below).

def example_query() -> dict:
    """Example: returns the name of the connected database."""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT current_database() AS db;")
            return dict(cur.fetchone())

# TODO: Implement the query_ and execute_ functions below.
# ─────────────────────────────────────────────────────────────────────────────


# ── NATIONAL RAIL AVAILABILITY ────────────────────────────────────────────────

def query_national_rail_availability(
    origin_id: str,
    destination_id: str,
    travel_date: Optional[str] = None,
) -> list[dict]:
    """
    Return national rail schedules that serve both origin and destination stations
    in the correct order, along with seat occupancy for the requested travel date.

    Args:
        origin_id:       e.g. "NR01"
        destination_id:  e.g. "NR05"
        travel_date:     e.g. "2025-06-01" — used to count bookings; omit for general info
    """
    sql = """
        SELECT
            s.schedule_id,
            s.line,
            s.service_type,
            s.direction,
            s.first_train_time,
            s.last_train_time,
            s.frequency_min,
            orig.station_id          AS origin_station_id,
            dest.station_id          AS destination_station_id,
            dest.stop_order - orig.stop_order AS stops_travelled,
            dest.travel_time_from_origin_min - orig.travel_time_from_origin_min AS journey_time_min,
            COUNT(b.booking_id)      AS seats_booked
        FROM national_rail_schedules s
        JOIN national_rail_schedule_stops AS orig
             ON orig.schedule_id = s.schedule_id AND orig.station_id = %s
        JOIN national_rail_schedule_stops AS dest
             ON dest.schedule_id = s.schedule_id AND dest.station_id = %s
        LEFT JOIN national_rail_bookings b
             ON b.schedule_id = s.schedule_id
            AND b.travel_date = %s
            AND b.status != 'cancelled'
        WHERE orig.stop_order < dest.stop_order
        GROUP BY s.schedule_id, s.line, s.service_type, s.direction,
                 s.first_train_time, s.last_train_time, s.frequency_min,
                 orig.station_id, dest.station_id,
                 orig.stop_order, dest.stop_order,
                 orig.travel_time_from_origin_min, dest.travel_time_from_origin_min
        ORDER BY s.line, s.first_train_time
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (origin_id, destination_id, travel_date))
            return [dict(row) for row in cur.fetchall()]


def query_national_rail_fare(
    schedule_id: str,
    fare_class: str,
    stops_travelled: int,
) -> Optional[dict]:
    """
    Calculate the fare for a national rail journey.

    Args:
        schedule_id:     e.g. "NR_SCH01"
        fare_class:      "standard" or "first"
        stops_travelled: number of stops between origin and destination (inclusive)

    Returns:
        dict with fare_class, base_fare_usd, per_stop_rate_usd, total_fare_usd
    """
    sql = """
        SELECT standard_base_fare_usd, standard_per_stop_rate_usd,
               first_base_fare_usd, first_per_stop_rate_usd
        FROM national_rail_schedules
        WHERE schedule_id = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (schedule_id,))
            row = cur.fetchone()
            if not row:
                return None
            if fare_class == "standard":
                base = float(row["standard_base_fare_usd"])
                per_stop = float(row["standard_per_stop_rate_usd"])
            elif fare_class == "first":
                base = float(row["first_base_fare_usd"])
                per_stop = float(row["first_per_stop_rate_usd"])
            else:
                return None
            return {
                "fare_class": fare_class,
                "base_fare_usd": base,
                "per_stop_rate_usd": per_stop,
                "total_fare_usd": round(base + per_stop * stops_travelled, 2),
            }


# ── METRO SCHEDULES & FARE ────────────────────────────────────────────────────

def query_metro_schedules(origin_id: str, destination_id: str) -> list[dict]:
    """
    Return metro schedules that serve both origin and destination in the correct order.

    Args:
        origin_id:       e.g. "MS01"
        destination_id:  e.g. "MS09"
    """
    sql = """
        SELECT
            s.schedule_id,
            s.line,
            s.direction,
            s.first_train_time,
            s.last_train_time,
            s.frequency_min,
            s.base_fare_usd,
            s.per_stop_rate_usd,
            orig.station_id AS origin_station_id,
            dest.station_id AS destination_station_id,
            dest.stop_order - orig.stop_order AS stops_travelled,
            dest.travel_time_from_origin_min - orig.travel_time_from_origin_min AS journey_time_min
        FROM metro_schedules s
        JOIN metro_schedule_stops AS orig
             ON orig.schedule_id = s.schedule_id AND orig.station_id = %s
        JOIN metro_schedule_stops AS dest
             ON dest.schedule_id = s.schedule_id AND dest.station_id = %s
        WHERE orig.stop_order < dest.stop_order
        ORDER BY s.line, s.first_train_time
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (origin_id, destination_id))
            return [dict(row) for row in cur.fetchall()]


def query_metro_fare(schedule_id: str, stops_travelled: int) -> Optional[dict]:
    """
    Calculate the metro fare for a single-ticket journey.

    Args:
        schedule_id:     e.g. "MS_SCH01"
        stops_travelled: number of stops between origin and destination

    Returns:
        dict with base_fare_usd, per_stop_rate_usd, total_fare_usd
    """
    sql = """
        SELECT base_fare_usd, per_stop_rate_usd
        FROM metro_schedules
        WHERE schedule_id = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (schedule_id,))
            row = cur.fetchone()
            if not row:
                return None
            base = float(row["base_fare_usd"])
            per_stop = float(row["per_stop_rate_usd"])
            return {
                "base_fare_usd": base,
                "per_stop_rate_usd": per_stop,
                "total_fare_usd": round(base + per_stop * stops_travelled, 2),
            }


# ── SEAT SELECTION ────────────────────────────────────────────────────────────

def query_available_seats(
    schedule_id: str,
    travel_date: str,
    fare_class: str,
) -> list[dict]:
    """
    Return available seats for a national rail journey on a given date.

    Args:
        schedule_id:  e.g. "NR_SCH01"
        travel_date:  e.g. "2025-06-01"
        fare_class:   "standard" or "first"

    Returns:
        List of dicts: {seat_id, coach, row, col}
    """
    # LEFT JOIN anti-join: join every seat in the layout against bookings for
    # this date, then keep only rows where no booking matched (b.booking_id IS NULL).
    # This avoids a subquery and lets the planner use the composite index on
    # (schedule_id, seat_id) for both sides of the join.
    sql = """
        SELECT sl.seat_id, sl.coach, sl.row, sl.col
        FROM national_rail_seat_layouts sl
        LEFT JOIN national_rail_bookings b
               ON b.schedule_id = sl.schedule_id
              AND b.seat_id     = sl.seat_id
              AND b.travel_date = %s
              AND b.status     != 'cancelled'
        WHERE sl.schedule_id = %s
          AND sl.fare_class  = %s
          AND b.booking_id IS NULL
        ORDER BY sl.coach, sl.row, sl.col
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (travel_date, schedule_id, fare_class))
            return [dict(row) for row in cur.fetchall()]


def auto_select_adjacent_seats(available_seats: list[dict], count: int) -> list[str]:
    """
    Select `count` seats that are as close together as possible (same row preferred,
    then adjacent rows). Returns a list of seat_ids.

    Args:
        available_seats: output of query_available_seats()
        count:           number of seats needed
    """
    if not available_seats or count <= 0:
        return []
    if count >= len(available_seats):
        return [s["seat_id"] for s in available_seats[:count]]

    from collections import defaultdict
    # Key includes coach so seats from different coaches never appear "adjacent".
    rows: dict[tuple, list[dict]] = defaultdict(list)
    for seat in available_seats:
        rows[(seat["coach"], seat["row"])].append(seat)

    for row_seats in sorted(rows.values(), key=lambda s: (s[0]["coach"], s[0]["row"])):
        if len(row_seats) >= count:
            return [s["seat_id"] for s in row_seats[:count]]

    sorted_seats = sorted(available_seats, key=lambda s: (s["coach"], s["row"], s["col"]))
    return [s["seat_id"] for s in sorted_seats[:count]]


# ── USER & BOOKING QUERIES ────────────────────────────────────────────────────

def query_user_profile(user_email: str) -> Optional[dict]:
    """Return a user's profile by email."""
    sql = """
        SELECT user_id, first_name, surname, email, phone, is_active, registered_at,
               EXTRACT(YEAR FROM date_of_birth)::int AS year_of_birth
        FROM users
        WHERE email = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (user_email,))
            row = cur.fetchone()
            return dict(row) if row else None


def query_user_bookings(user_email: str) -> dict:
    """
    Return a user's combined booking history (national rail + metro).

    Returns:
        dict with keys 'national_rail' (list) and 'metro' (list)
    """
    user = query_user_profile(user_email)
    if not user:
        return {"national_rail": [], "metro": []}

    nr_sql = """
        SELECT b.booking_id, b.schedule_id, b.travel_date, b.departure_time,
               b.ticket_type, b.fare_class, b.coach, b.seat_id,
               b.stops_travelled, b.amount_usd, b.status, b.booked_at,
               orig.name AS origin_name, dest.name AS destination_name
        FROM national_rail_bookings b
        JOIN national_rail_stations orig ON orig.station_id = b.origin_station_id
        JOIN national_rail_stations dest ON dest.station_id = b.destination_station_id
        WHERE b.user_id = %s
        ORDER BY b.travel_date DESC
    """
    metro_sql = """
        SELECT t.trip_id, t.schedule_id, t.travel_date, t.ticket_type,
               t.stops_travelled, t.amount_usd, t.status, t.travelled_at,
               orig.name AS origin_name, dest.name AS destination_name
        FROM metro_travel_history t
        JOIN metro_stations orig ON orig.station_id = t.origin_station_id
        JOIN metro_stations dest ON dest.station_id = t.destination_station_id
        WHERE t.user_id = %s
        ORDER BY t.travel_date DESC
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(nr_sql, (user["user_id"],))
            national_rail = [dict(row) for row in cur.fetchall()]
            cur.execute(metro_sql, (user["user_id"],))
            metro = [dict(row) for row in cur.fetchall()]
    return {"national_rail": national_rail, "metro": metro}


def query_payment_info(booking_id: str) -> Optional[dict]:
    """Return payment record for a booking or metro trip."""
    sql = "SELECT * FROM payments WHERE booking_id = %s"
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (booking_id,))
            row = cur.fetchone()
            return dict(row) if row else None


# ── TRANSACTIONAL OPERATIONS ──────────────────────────────────────────────────

def execute_booking(
    user_id: str,
    schedule_id: str,
    origin_station_id: str,
    destination_station_id: str,
    travel_date: str,
    fare_class: str,
    seat_id: str,
    ticket_type: str = "single",
) -> tuple[bool, dict | str]:
    """
    Create a national rail booking for a logged-in user.

    Args:
        user_id:                e.g. "RU01" — must match the logged-in user
        schedule_id:            e.g. "NR_SCH01"
        origin_station_id:      e.g. "NR01"
        destination_station_id: e.g. "NR05"
        travel_date:            e.g. "2025-06-01"
        fare_class:             "standard" or "first"
        seat_id:                e.g. "B05" (or "any" to auto-assign)
        ticket_type:            "single" (default) or "return"

    Returns:
        (True, booking_dict)   on success
        (False, error_message) on failure
    """
    # Single connection for all SELECTs and the final INSERT/commit — avoids
    # opening 3 separate connections and keeps the entire booking flow in one
    # database session.
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = False
    try:
        # 1. 取得 stops_travelled 和 departure_time
        stops_sql = """
            SELECT orig.stop_order, dest.stop_order AS dest_order,
                   s.first_train_time
            FROM national_rail_schedules s
            JOIN national_rail_schedule_stops orig
                 ON orig.schedule_id = s.schedule_id AND orig.station_id = %s
            JOIN national_rail_schedule_stops dest
                 ON dest.schedule_id = s.schedule_id AND dest.station_id = %s
            WHERE s.schedule_id = %s AND orig.stop_order < dest.stop_order
        """
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(stops_sql, (origin_station_id, destination_station_id, schedule_id))
            route = cur.fetchone()
        if not route:
            return False, "route not found for given schedule and stations"

        stops_travelled = route["dest_order"] - route["stop_order"]
        departure_time  = route["first_train_time"]

        # 2. 計算票價（opens its own connection internally — acceptable helper call）
        fare = query_national_rail_fare(schedule_id, fare_class, stops_travelled)
        if not fare:
            return False, f"invalid fare class: {fare_class}"

        # 3. 選座位（opens its own connection internally — acceptable helper call）
        if seat_id == "any":
            available = query_available_seats(schedule_id, travel_date, fare_class)
            selected  = auto_select_adjacent_seats(available, 1)
            if not selected:
                return False, "no seats available"
            seat_id = selected[0]
        else:
            available = query_available_seats(schedule_id, travel_date, fare_class)
            if not any(s["seat_id"] == seat_id for s in available):
                return False, f"seat {seat_id} is not available"

        # 4. 取得 coach
        coach_sql = "SELECT coach FROM national_rail_seat_layouts WHERE schedule_id = %s AND seat_id = %s"
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(coach_sql, (schedule_id, seat_id))
            seat_row = cur.fetchone()
        coach = seat_row["coach"] if seat_row else None

        # 4.5. Re-verify seat inside the transaction with a row-level lock.
        # FOR UPDATE on the seat layout row serialises concurrent booking requests:
        # the second transaction blocks here until the first commits, then the
        # conflict check finds the seat taken and returns False cleanly.
        lock_sql = """
            SELECT 1 FROM national_rail_seat_layouts
            WHERE schedule_id = %s AND seat_id = %s
            FOR UPDATE
        """
        conflict_sql = """
            SELECT 1 FROM national_rail_bookings
            WHERE schedule_id = %s AND travel_date = %s AND seat_id = %s
              AND status != 'cancelled'
        """
        with conn.cursor() as cur:
            cur.execute(lock_sql, (schedule_id, seat_id))
            cur.execute(conflict_sql, (schedule_id, travel_date, seat_id))
            if cur.fetchone():
                return False, f"seat {seat_id} is no longer available"

        # 5. INSERT booking + payment (atomic: both succeed or both roll back)
        booking_id  = _gen_booking_id()
        payment_id  = _gen_payment_id()
        amount      = fare["total_fare_usd"]
        booked_at   = datetime.now(timezone.utc)

        booking_sql = """
            INSERT INTO national_rail_bookings
                (booking_id, user_id, schedule_id, origin_station_id, destination_station_id,
                 travel_date, departure_time, ticket_type, fare_class, coach, seat_id,
                 stops_travelled, amount_usd, status, booked_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'confirmed',%s)
        """
        payment_sql = """
            INSERT INTO payments (payment_id, booking_id, amount_usd, method, status, paid_at)
            VALUES (%s, %s, %s, 'credit_card', 'paid', %s)
        """
        with conn.cursor() as cur:
            cur.execute(booking_sql, (
                booking_id, user_id, schedule_id, origin_station_id, destination_station_id,
                travel_date, departure_time, ticket_type, fare_class, coach, seat_id,
                stops_travelled, amount, booked_at
            ))
            cur.execute(payment_sql, (payment_id, booking_id, amount, booked_at))
        conn.commit()
        return True, {
            "booking_id": booking_id,
            "schedule_id": schedule_id,
            "travel_date": travel_date,
            "seat_id": seat_id,
            "coach": coach,
            "fare_class": fare_class,
            "amount_usd": amount,
            "status": "confirmed",
        }
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()


def execute_cancellation(booking_id: str, user_id: str) -> tuple[bool, dict | str]:
    """
    Cancel a national rail booking owned by the given user.

    Calculates the refund amount according to the booking's service type:
      - Normal service: RF001 windows (100% / 75% / 50% / 0%)
      - Express service: RF002 windows (100% / 50% / 0%)

    Args:
        booking_id: e.g. "BK001"
        user_id:    must match the booking's user_id

    Returns:
        (True, result_dict)  with refund_amount_usd and policy note
        (False, error_msg)
    """
    # 1. 查 booking
    fetch_sql = """
        SELECT b.booking_id, b.user_id, b.travel_date, b.amount_usd, b.status,
               s.service_type
        FROM national_rail_bookings b
        JOIN national_rail_schedules s ON s.schedule_id = b.schedule_id
        WHERE b.booking_id = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(fetch_sql, (booking_id,))
            booking = cur.fetchone()

    if not booking:
        return False, "booking not found"
    if booking["user_id"] != user_id:
        return False, "booking does not belong to this user"
    if booking["status"] == "cancelled":
        return False, "booking is already cancelled"

    # 2. 計算退款比例
    today = datetime.now(timezone.utc).date()
    days_until = (booking["travel_date"] - today).days
    service    = booking["service_type"]
    amount     = float(booking["amount_usd"])

    if service == "normal":
        if days_until >= 7:
            pct, note = 1.00, "RF001: 100% refund (7+ days)"
        elif days_until >= 3:
            pct, note = 0.75, "RF001: 75% refund (3–6 days)"
        elif days_until >= 1:
            pct, note = 0.50, "RF001: 50% refund (1–2 days)"
        else:
            pct, note = 0.00, "RF001: no refund (same day)"
    else:  # express
        if days_until >= 7:
            pct, note = 1.00, "RF002: 100% refund (7+ days)"
        elif days_until >= 3:
            pct, note = 0.50, "RF002: 50% refund (3–6 days)"
        else:
            pct, note = 0.00, "RF002: no refund (< 3 days)"

    refund = round(amount * pct, 2)

    # 3. UPDATE booking + INSERT refund payment
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            # Atomic guard: only cancels rows still in 'confirmed' state.
            # rowcount == 0 means a concurrent request already cancelled it.
            cur.execute(
                "UPDATE national_rail_bookings SET status = 'cancelled' WHERE booking_id = %s AND status = 'confirmed'",
                (booking_id,)
            )
            if cur.rowcount == 0:
                return False, "booking already cancelled or not found"
            if refund > 0:
                cur.execute(
                    """INSERT INTO payments (payment_id, booking_id, amount_usd, method, status, paid_at)
                       VALUES (%s, %s, %s, 'credit_card', 'refunded', %s)""",
                    (_gen_payment_id(), booking_id, refund, datetime.now(timezone.utc))
                )
        conn.commit()
        return True, {
            "booking_id": booking_id,
            "status": "cancelled",
            "refund_amount_usd": refund,
            "policy_note": note,
        }
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()


# ── AUTHENTICATION QUERIES ────────────────────────────────────────────────────

def register_user(
    email: str,
    first_name: str,
    surname: str,
    year_of_birth: int,
    password: str,
    secret_question: str,
    secret_answer: str,
) -> tuple[bool, str]:
    """
    Register a new user.
    Returns (True, user_id) on success or (False, error_message) on failure.
    """
    user_id = "RU-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    # bcrypt with cost factor 12: slow enough to resist brute-force, fast enough for login UX
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    # year_of_birth (int) → DATE: store Jan 1 of that year to satisfy the DATE column type
    formatted_dob = f"{year_of_birth}-01-01"
    sql = """
        INSERT INTO users (user_id, first_name, surname, email, password_hash,
                           date_of_birth, secret_question, secret_answer, is_active)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
    """
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, first_name, surname, email, password_hash,
                              formatted_dob, secret_question, secret_answer))
        conn.commit()
        return True, user_id
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return False, "email already registered"
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()


def login_user(email: str, password: str) -> Optional[dict]:
    """
    Verify credentials. Returns a user dict on success or None on failure.
    Dict keys: user_id, email, first_name, surname, phone, date_of_birth, is_active.
    """
    sql = """
        SELECT user_id, email, first_name, surname, phone, date_of_birth, is_active,
               password_hash
        FROM users
        WHERE email = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (email,))
            row = cur.fetchone()
    if not row:
        return None
    # bcrypt.checkpw handles both bcrypt hashes and rejects plain-text mismatches safely
    try:
        match = bcrypt.checkpw(password.encode(), row["password_hash"].encode())
    except Exception:
        match = False
    if not match:
        return None
    row = dict(row)
    row.pop("password_hash")
    return row


def get_user_secret_question(email: str) -> Optional[str]:
    """Return the secret question for a registered email, or None if not found."""
    sql = "SELECT secret_question FROM users WHERE email = %s"
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (email,))
            row = cur.fetchone()
            return row["secret_question"] if row else None


def verify_secret_answer(email: str, answer: str) -> bool:
    """Return True if the provided answer matches the stored secret answer (case-insensitive)."""
    sql = "SELECT secret_answer FROM users WHERE email = %s"
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (email,))
            row = cur.fetchone()
            if not row or not row["secret_answer"]:
                return False
            return row["secret_answer"].lower() == answer.lower()


def update_password(email: str, new_password: str) -> bool:
    """Update the password for a user. Returns True if the row was updated."""
    # Hash with the same cost factor as register_user so login_user's
    # bcrypt.checkpw works correctly after a password change.
    password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(rounds=12)).decode()
    sql = "UPDATE users SET password_hash = %s WHERE email = %s"
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (password_hash, email))
            updated = cur.rowcount > 0
        conn.commit()
        return updated
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


# ── VECTOR / RAG QUERIES — do not modify ─────────────────────────────────────

def query_policy_vector_search(embedding: list[float], top_k: int = VECTOR_TOP_K) -> list[dict]:
    """
    Find the most relevant policy documents for a given query embedding.

    Args:
        embedding: Query vector from llm.embed(user_question)
        top_k:     Number of results to return

    Returns:
        List of dicts with title, category, content, and similarity score
    """
    sql = """
        SELECT
            title,
            category,
            content,
            1 - (embedding <=> %s::vector) AS similarity
        FROM policy_documents
        WHERE 1 - (embedding <=> %s::vector) > %s
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (vec_str, vec_str, VECTOR_SIMILARITY_THRESHOLD, vec_str, top_k))
            return [dict(row) for row in cur.fetchall()]


def store_policy_document(
    title: str,
    category: str,
    content: str,
    embedding: list[float],
    source_file: str = "",
) -> int:
    """
    Insert a policy document with its embedding into the database.
    Used by skeleton/seed_vectors.py — students don't need to call this directly.

    Returns:
        The new document's id
    """
    sql = """
        INSERT INTO policy_documents (title, category, content, embedding, source_file)
        VALUES (%s, %s, %s, %s::vector, %s)
        RETURNING id
    """
    vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (title, category, content, vec_str, source_file))
            return cur.fetchone()[0]
