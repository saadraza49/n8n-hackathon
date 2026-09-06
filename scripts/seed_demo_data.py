"""
FMS — Production-Grade Database Seeding Script
================================================

Idempotent, safe, and additive seed system for all project phases (1A–6A).

Features:
- Deterministic UUIDs for idempotent re-runs (never duplicates)
- Skip-if-exists logic for all records
- Realistic airline/booking/notification data
- Covers: Users, Flights, FlightClasses, Seats, FareRules, Bookings,
  BookingItems, Cancellations, Refunds, Waitlists, Notifications, PriceHistory
- Safe: never destroys or overwrites existing data

Usage:
    python -m scripts.seed_demo_data
"""

import sys
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

# Ensure the project root is on the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.orm import Session
from sqlalchemy import inspect as sa_inspect

from app.database import SessionLocal, engine, Base
from app.core.security import hash_password
from app.models.enums import (
    BookingItemStatus,
    BookingStatus,
    FareType,
    FlightClassType,
    FlightStatus,
    NotificationChannel,
    NotificationStatus,
    NotificationType,
    PriceChangeReason,
    RefundReason,
    RefundStatus,
    RefundType,
    SeatStatus,
    UserRole,
    WaitlistEntryType,
    WaitlistStatus,
)
from app.models.user import User
from app.models.flight import Flight
from app.models.flight_class import FlightClass
from app.models.flight_seat import FlightSeat
from app.models.fare_rule import FareRule
from app.models.booking import Booking, BookingItem, Refund
from app.models.waitlist import WaitlistEntry
from app.models.notification import Notification
from app.models.price_history import PriceHistory


# ────────────────────────────────────────────────────────────────
# Deterministic UUID factory  (namespace-based for idempotency)
# ────────────────────────────────────────────────────────────────
SEED_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def seed_uuid(label: str) -> uuid.UUID:
    """Generate a deterministic UUID from a human-readable label."""
    return uuid.uuid5(SEED_NAMESPACE, label)


# ────────────────────────────────────────────────────────────────
# Helper: skip-if-exists upsert
# ────────────────────────────────────────────────────────────────
def get_or_create(db: Session, model, id_value: uuid.UUID, **kwargs):
    """Return existing record or create a new one. Never overwrites."""
    existing = db.get(model, id_value)
    if existing:
        return existing, False
    obj = model(id=id_value, **kwargs)
    db.add(obj)
    return obj, True


def log(emoji: str, message: str):
    print(f"  {emoji} {message}")


# ────────────────────────────────────────────────────────────────
# 1. SEED USERS
# ────────────────────────────────────────────────────────────────
def seed_users(db: Session) -> dict:
    print("\n🔐 Seeding Users...")
    pw_hash = hash_password("Demo@12345")

    users_spec = [
        ("user-super-admin",    "Super Admin",       "admin@fms-demo.com",        UserRole.SUPER_ADMIN),
        ("user-ops-agent-1",    "Ops Agent Alpha",   "ops.alpha@fms-demo.com",    UserRole.OPS_AGENT),
        ("user-ops-agent-2",    "Ops Agent Bravo",   "ops.bravo@fms-demo.com",    UserRole.OPS_AGENT),
        ("user-passenger-1",    "Alice Traveller",   "alice@fms-demo.com",        UserRole.PASSENGER),
        ("user-passenger-2",    "Bob Explorer",      "bob@fms-demo.com",          UserRole.PASSENGER),
        ("user-passenger-3",    "Charlie Nomad",     "charlie@fms-demo.com",      UserRole.PASSENGER),
        ("user-passenger-4",    "Diana Voyager",     "diana@fms-demo.com",        UserRole.PASSENGER),
        ("user-passenger-5",    "Eve Wanderer",      "eve@fms-demo.com",          UserRole.PASSENGER),
    ]

    user_map = {}
    for label, name, email, role in users_spec:
        uid = seed_uuid(label)
        obj, created = get_or_create(
            db, User, uid,
            name=name, email=email, password_hash=pw_hash, role=role,
        )
        user_map[label] = obj
        status = "✅ created" if created else "⏩ exists"
        log("👤", f"{name} ({role.value}) — {status}")

    db.flush()
    return user_map


# ────────────────────────────────────────────────────────────────
# 2. SEED FLIGHTS  (6 flights covering various scenarios)
# ────────────────────────────────────────────────────────────────
def seed_flights(db: Session, user_map: dict) -> dict:
    print("\n✈️  Seeding Flights...")
    ops_agent = user_map["user-ops-agent-1"]
    now = datetime.now(timezone.utc)

    flights_spec = [
        # label, number, origin, dest, orig_tz, dest_tz, depart_offset_hours, duration_hours, capacity, status
        ("flight-lhr-jfk",  "FMS-101", "LHR", "JFK", "Europe/London",     "America/New_York",    24,  8, 30, FlightStatus.SCHEDULED),
        ("flight-dxb-sin",  "FMS-202", "DXB", "SIN", "Asia/Dubai",        "Asia/Singapore",      48,  7, 30, FlightStatus.SCHEDULED),
        ("flight-sfo-nrt",  "FMS-303", "SFO", "NRT", "America/Los_Angeles","Asia/Tokyo",          72, 11, 30, FlightStatus.SCHEDULED),
        ("flight-cdg-ist",  "FMS-404", "CDG", "IST", "Europe/Paris",      "Europe/Istanbul",      6,  3, 30, FlightStatus.SCHEDULED),   # departing soon (check-in eligible)
        ("flight-cancelled", "FMS-505", "LAX", "ORD", "America/Los_Angeles","America/Chicago",    96,  4, 30, FlightStatus.CANCELLED),
        ("flight-completed", "FMS-606", "BOM", "DEL", "Asia/Kolkata",     "Asia/Kolkata",        -48,  2, 30, FlightStatus.COMPLETED),
    ]

    flight_map = {}
    for label, number, origin, dest, orig_tz, dest_tz, dep_offset, dur, cap, fstatus in flights_spec:
        fid = seed_uuid(label)
        departure = now + timedelta(hours=dep_offset)
        arrival = departure + timedelta(hours=dur)

        obj, created = get_or_create(
            db, Flight, fid,
            flight_number=number,
            origin=origin,
            destination=dest,
            origin_timezone=orig_tz,
            destination_timezone=dest_tz,
            departure_at=departure,
            arrival_at=arrival,
            total_capacity=cap,
            status=fstatus,
            created_by=ops_agent.id,
        )
        flight_map[label] = obj
        status = "✅ created" if created else "⏩ exists"
        log("🛫", f"{number} {origin}→{dest} ({fstatus.value}) — {status}")

    db.flush()
    return flight_map


# ────────────────────────────────────────────────────────────────
# 3. SEED FLIGHT CLASSES
# ────────────────────────────────────────────────────────────────
def seed_flight_classes(db: Session, flight_map: dict) -> dict:
    print("\n💺 Seeding Flight Classes...")

    # Each flight: 4 First, 8 Business, 18 Economy = 30 total
    class_configs = [
        (FlightClassType.FIRST,    4),
        (FlightClassType.BUSINESS, 8),
        (FlightClassType.ECONOMY, 18),
    ]

    fc_map = {}
    for flight_label, flight_obj in flight_map.items():
        for class_type, total_seats in class_configs:
            label = f"fc-{flight_label}-{class_type.value}"
            fc_id = seed_uuid(label)
            obj, created = get_or_create(
                db, FlightClass, fc_id,
                flight_id=flight_obj.id,
                class_type=class_type,
                total_seats=total_seats,
                available_seats=total_seats,
            )
            fc_map[label] = obj
            if created:
                log("🪑", f"{flight_obj.flight_number} {class_type.value}: {total_seats} seats — ✅ created")

    db.flush()
    return fc_map


# ────────────────────────────────────────────────────────────────
# 4. SEED PHYSICAL SEATS
# ────────────────────────────────────────────────────────────────
def seed_seats(db: Session, flight_map: dict) -> dict:
    print("\n🔢 Seeding Physical Seats...")
    from app.services.seat_generator import generate_physical_seats

    seat_map = {}
    for flight_label, flight_obj in flight_map.items():
        # Check if seats already exist
        existing_count = db.query(FlightSeat).filter(FlightSeat.flight_id == flight_obj.id).count()
        if existing_count > 0:
            log("⏩", f"{flight_obj.flight_number}: {existing_count} seats already exist")
            # Load existing seats into map
            existing_seats = db.query(FlightSeat).filter(FlightSeat.flight_id == flight_obj.id).order_by(FlightSeat.seat_number).all()
            for s in existing_seats:
                key = f"seat-{flight_label}-{s.seat_number}"
                seat_map[key] = s
            continue

        # Generate new seats: 4 First, 8 Business, 18 Economy
        new_seats = generate_physical_seats(flight_obj.id, 4, 8, 18)
        for s in new_seats:
            db.add(s)
            key = f"seat-{flight_label}-{s.seat_number}"
            seat_map[key] = s
        log("✅", f"{flight_obj.flight_number}: {len(new_seats)} seats generated")

    db.flush()

    # Re-load seats into map with proper IDs
    for flight_label, flight_obj in flight_map.items():
        existing_seats = db.query(FlightSeat).filter(
            FlightSeat.flight_id == flight_obj.id
        ).order_by(FlightSeat.seat_number).all()
        for s in existing_seats:
            key = f"seat-{flight_label}-{s.seat_number}"
            seat_map[key] = s

    return seat_map


# ────────────────────────────────────────────────────────────────
# 5. SEED FARE RULES
# ────────────────────────────────────────────────────────────────
def seed_fare_rules(db: Session, flight_map: dict) -> dict:
    print("\n💰 Seeding Fare Rules...")

    # price, changes_allowed, seat_select, refundable, credit_only, cancel_cutoff
    fare_configs = {
        (FlightClassType.FIRST, FareType.BASIC):     (Decimal("1200.00"), False, True,  False, False, 120),
        (FlightClassType.FIRST, FareType.FLEXIBLE):   (Decimal("1800.00"), True,  True,  True,  False,  0),
        (FlightClassType.BUSINESS, FareType.BASIC):    (Decimal("600.00"),  False, True,  False, True,   60),
        (FlightClassType.BUSINESS, FareType.FLEXIBLE): (Decimal("900.00"),  True,  True,  True,  False,  0),
        (FlightClassType.ECONOMY, FareType.BASIC):     (Decimal("200.00"),  False, False, False, False, 180),
        (FlightClassType.ECONOMY, FareType.FLEXIBLE):  (Decimal("350.00"),  True,  True,  True,  False,  0),
    }

    fr_map = {}
    for flight_label, flight_obj in flight_map.items():
        for (class_type, fare_type), (price, changes, seat_sel, refundable, credit, cutoff) in fare_configs.items():
            label = f"fr-{flight_label}-{class_type.value}-{fare_type.value}"
            fr_id = seed_uuid(label)
            obj, created = get_or_create(
                db, FareRule, fr_id,
                flight_id=flight_obj.id,
                class_type=class_type,
                fare_type=fare_type,
                price=price,
                currency="USD",
                changes_allowed=changes,
                seat_selection_allowed=seat_sel,
                refundable=refundable,
                credit_only=credit,
                cancellation_cutoff_minutes=cutoff,
            )
            fr_map[label] = obj
            if created:
                log("💵", f"{flight_obj.flight_number} {class_type.value}/{fare_type.value}: ${price} — ✅")

    db.flush()
    return fr_map


# ────────────────────────────────────────────────────────────────
# 6. SEED BOOKINGS + BOOKING ITEMS
# ────────────────────────────────────────────────────────────────
def seed_bookings(db: Session, user_map: dict, flight_map: dict, seat_map: dict) -> dict:
    print("\n📋 Seeding Bookings...")

    booking_specs = [
        # Seat layout: First=1A-1D, Business=2A-2F/3A-3B, Economy=4A-4F/5A-5F/6A-6F
        # label, user_label, flight_label, seat_number, class, fare, passenger_name, price, status, ref
        ("bk-alice-lhr",   "user-passenger-1", "flight-lhr-jfk",  "1A", FlightClassType.FIRST,    FareType.FLEXIBLE, "Alice Traveller", Decimal("1800.00"), BookingStatus.CONFIRMED, "BKALC001"),
        ("bk-bob-lhr",     "user-passenger-2", "flight-lhr-jfk",  "2A", FlightClassType.BUSINESS, FareType.BASIC,    "Bob Explorer",    Decimal("600.00"),  BookingStatus.CONFIRMED, "BKBOB001"),
        ("bk-charlie-dxb", "user-passenger-3", "flight-dxb-sin",  "4A", FlightClassType.ECONOMY,  FareType.FLEXIBLE, "Charlie Nomad",   Decimal("350.00"),  BookingStatus.CONFIRMED, "BKCHR001"),
        ("bk-diana-cdg",   "user-passenger-4", "flight-cdg-ist",  "4B", FlightClassType.ECONOMY,  FareType.BASIC,    "Diana Voyager",   Decimal("200.00"),  BookingStatus.CONFIRMED, "BKDNA001"),
        ("bk-eve-cdg",     "user-passenger-5", "flight-cdg-ist",  "4C", FlightClassType.ECONOMY,  FareType.FLEXIBLE, "Eve Wanderer",    Decimal("350.00"),  BookingStatus.CONFIRMED, "BKEVE001"),
        ("bk-alice-sfo",   "user-passenger-1", "flight-sfo-nrt",  "1B", FlightClassType.FIRST,    FareType.BASIC,    "Alice Traveller", Decimal("1200.00"), BookingStatus.CONFIRMED, "BKALC002"),
        ("bk-bob-cancel",  "user-passenger-2", "flight-cancelled", "4D", FlightClassType.ECONOMY,  FareType.FLEXIBLE, "Bob Explorer",    Decimal("350.00"),  BookingStatus.CANCELLED, "BKBOB002"),
        ("bk-charlie-done","user-passenger-3", "flight-completed", "4E", FlightClassType.ECONOMY,  FareType.BASIC,    "Charlie Nomad",   Decimal("200.00"),  BookingStatus.CONFIRMED, "BKCHR002"),
    ]

    booking_map = {}
    for (label, user_label, flight_label, seat_num, class_type, fare_type,
         pax_name, price, bk_status, ref) in booking_specs:

        bk_id = seed_uuid(label)
        user_obj = user_map[user_label]
        flight_obj = flight_map[flight_label]

        # Resolve seat
        seat_key = f"seat-{flight_label}-{seat_num}"
        seat_obj = seat_map.get(seat_key)
        if not seat_obj:
            log("⚠️", f"Seat {seat_num} not found for {flight_label}, skipping booking {label}")
            continue

        # Create booking
        idemp_key = f"seed-{label}"
        bk_obj, bk_created = get_or_create(
            db, Booking, bk_id,
            booking_reference=ref,
            user_id=user_obj.id,
            flight_id=flight_obj.id,
            status=bk_status,
            total_amount=price,
            currency="USD",
            idempotency_key=idemp_key,
        )
        booking_map[label] = bk_obj

        # Create booking item
        bi_id = seed_uuid(f"bi-{label}")
        bi_status = BookingItemStatus.CONFIRMED if bk_status == BookingStatus.CONFIRMED else BookingItemStatus.CANCELLED
        _, bi_created = get_or_create(
            db, BookingItem, bi_id,
            booking_id=bk_obj.id,
            flight_id=flight_obj.id,
            seat_id=seat_obj.id,
            class_type=class_type,
            fare_type=fare_type,
            status=bi_status,
            passenger_name=pax_name,
            price=price,
            currency="USD",
        )

        # Mark seat as booked (if booking confirmed)
        if bk_created and bk_status == BookingStatus.CONFIRMED:
            seat_obj.status = SeatStatus.BOOKED

        status_str = "✅ created" if bk_created else "⏩ exists"
        log("📝", f"{ref} {pax_name} on {flight_obj.flight_number} ({bk_status.value}) — {status_str}")

    db.flush()

    # Update available_seats on FlightClass for confirmed bookings
    for label, user_label, flight_label, seat_num, class_type, fare_type, pax_name, price, bk_status, ref in booking_specs:
        if bk_status == BookingStatus.CONFIRMED:
            flight_obj = flight_map[flight_label]
            fc = db.query(FlightClass).filter(
                FlightClass.flight_id == flight_obj.id,
                FlightClass.class_type == class_type,
            ).first()
            if fc:
                booked_count = db.query(FlightSeat).filter(
                    FlightSeat.flight_id == flight_obj.id,
                    FlightSeat.class_type == class_type,
                    FlightSeat.status == SeatStatus.BOOKED,
                ).count()
                fc.available_seats = fc.total_seats - booked_count

    db.flush()
    return booking_map


# ────────────────────────────────────────────────────────────────
# 7. SEED REFUNDS  (for cancelled booking)
# ────────────────────────────────────────────────────────────────
def seed_refunds(db: Session, booking_map: dict):
    print("\n💸 Seeding Refunds...")

    refund_specs = [
        # label, booking_label, amount, type, status, reason
        ("refund-bob-cancel", "bk-bob-cancel", Decimal("350.00"), RefundType.MONETARY, RefundStatus.COMPLETED, RefundReason.CUSTOMER_CANCELLATION),
    ]

    for label, bk_label, amount, r_type, r_status, reason in refund_specs:
        bk_obj = booking_map.get(bk_label)
        if not bk_obj:
            log("⚠️", f"Booking {bk_label} not found, skipping refund")
            continue

        r_id = seed_uuid(label)
        _, created = get_or_create(
            db, Refund, r_id,
            booking_id=bk_obj.id,
            amount=amount,
            currency="USD",
            refund_type=r_type,
            status=r_status,
            reason=reason,
            idempotency_key=f"seed-{label}",
            notes="Seeded demo refund",
        )
        status_str = "✅ created" if created else "⏩ exists"
        log("💰", f"Refund ${amount} for {bk_label} ({r_status.value}) — {status_str}")

    db.flush()


# ────────────────────────────────────────────────────────────────
# 8. SEED WAITLIST ENTRIES
# ────────────────────────────────────────────────────────────────
def seed_waitlist(db: Session, user_map: dict, flight_map: dict):
    print("\n⏳ Seeding Waitlist Entries...")

    waitlist_specs = [
        # label, user_label, flight_label, class, entry_type, status, priority
        ("wl-diana-lhr", "user-passenger-4", "flight-lhr-jfk", FlightClassType.FIRST,    WaitlistEntryType.WAITLIST, WaitlistStatus.WAITING,  100),
        ("wl-eve-dxb",   "user-passenger-5", "flight-dxb-sin", FlightClassType.BUSINESS, WaitlistEntryType.STANDBY,  WaitlistStatus.WAITING,   50),
    ]

    for label, user_label, flight_label, class_type, entry_type, wl_status, priority in waitlist_specs:
        wl_id = seed_uuid(label)
        user_obj = user_map[user_label]
        flight_obj = flight_map[flight_label]

        _, created = get_or_create(
            db, WaitlistEntry, wl_id,
            flight_id=flight_obj.id,
            passenger_id=user_obj.id,
            class_type=class_type,
            entry_type=entry_type,
            status=wl_status,
            priority_score=priority,
            idempotency_key=f"seed-{label}",
            notes="Seeded waitlist entry",
        )
        status_str = "✅ created" if created else "⏩ exists"
        log("⏳", f"{user_map[user_label].name} on {flight_obj.flight_number} ({entry_type.value}) — {status_str}")

    db.flush()


# ────────────────────────────────────────────────────────────────
# 9. SEED NOTIFICATIONS  (check-in reminders + price drop alerts)
# ────────────────────────────────────────────────────────────────
def seed_notifications(db: Session, user_map: dict, flight_map: dict, booking_map: dict):
    print("\n🔔 Seeding Notifications...")
    now = datetime.now(timezone.utc)

    notification_specs = [
        # label, user_label, type, channel, flight_label, booking_label, status, dedup_key, scheduled_offset_hrs
        (
            "notif-alice-checkin", "user-passenger-1",
            NotificationType.CHECK_IN_REMINDER, NotificationChannel.EMAIL,
            "flight-lhr-jfk", "bk-alice-lhr",
            NotificationStatus.PENDING,
            None, 0  # scheduled for now
        ),
        (
            "notif-diana-checkin", "user-passenger-4",
            NotificationType.CHECK_IN_REMINDER, NotificationChannel.EMAIL,
            "flight-cdg-ist", "bk-diana-cdg",
            NotificationStatus.PENDING,
            None, 0
        ),
        (
            "notif-eve-checkin", "user-passenger-5",
            NotificationType.CHECK_IN_REMINDER, NotificationChannel.EMAIL,
            "flight-cdg-ist", "bk-eve-cdg",
            NotificationStatus.PENDING,
            None, 0
        ),
        (
            "notif-bob-pricedrop", "user-passenger-2",
            NotificationType.PRICE_DROP_ALERT, NotificationChannel.EMAIL,
            "flight-lhr-jfk", None,
            NotificationStatus.SENT,
            None, -2  # sent 2 hours ago
        ),
        (
            "notif-charlie-confirm", "user-passenger-3",
            NotificationType.BOOKING_CONFIRMATION, NotificationChannel.EMAIL,
            "flight-dxb-sin", "bk-charlie-dxb",
            NotificationStatus.SENT,
            None, -24  # sent a day ago
        ),
    ]

    for (label, user_label, n_type, channel, flight_label, booking_label,
         n_status, _, sched_offset) in notification_specs:

        n_id = seed_uuid(label)
        user_obj = user_map[user_label]
        flight_obj = flight_map[flight_label]
        booking_obj = booking_map.get(booking_label)

        # Build deduplication key using the same scheme as automation_service
        if n_type == NotificationType.CHECK_IN_REMINDER:
            bk_id = booking_obj.id if booking_obj else "none"
            dedup_key = f"checkin:{bk_id}:{flight_obj.id}:{user_obj.id}"
        elif n_type == NotificationType.PRICE_DROP_ALERT:
            dedup_key = f"pricedrop:{flight_obj.id}:ECONOMY:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        else:
            dedup_key = f"seed:{label}"

        scheduled_for = now + timedelta(hours=sched_offset)
        sent_at = scheduled_for if n_status == NotificationStatus.SENT else None

        _, created = get_or_create(
            db, Notification, n_id,
            user_id=user_obj.id,
            notification_type=n_type,
            channel=channel,
            recipient=user_obj.email,
            status=n_status,
            flight_id=flight_obj.id,
            booking_id=booking_obj.id if booking_obj else None,
            deduplication_key=dedup_key,
            payload={"source": "seed", "label": label},
            scheduled_for=scheduled_for,
            sent_at=sent_at,
            attempt_count=1 if n_status == NotificationStatus.SENT else 0,
        )
        status_str = "✅ created" if created else "⏩ exists"
        log("🔔", f"{n_type.value} → {user_obj.name} ({n_status.value}) — {status_str}")

    db.flush()


# ────────────────────────────────────────────────────────────────
# 10. SEED PRICE HISTORY  (simulate fare changes for price-drop detection)
# ────────────────────────────────────────────────────────────────
def seed_price_history(db: Session, flight_map: dict, fr_map: dict, user_map: dict):
    print("\n📈 Seeding Price History...")
    now = datetime.now(timezone.utc)
    ops_agent = user_map["user-ops-agent-1"]

    # Simulate price changes: Economy BASIC on flight-lhr-jfk dropped from $250→$200 (20% drop)
    # and Business FLEXIBLE on flight-dxb-sin dropped from $1100→$900 (18.2% drop)
    price_history_specs = [
        # label, flight_label, class, fare, old, new, delta, pct_drop, reason, hours_ago
        ("ph-lhr-eco-drop",  "flight-lhr-jfk", FlightClassType.ECONOMY,  FareType.BASIC,    Decimal("250.00"), Decimal("200.00"), Decimal("-50.00"), Decimal("-20.00"), PriceChangeReason.MANUAL_UPDATE, 12),
        ("ph-dxb-biz-drop",  "flight-dxb-sin", FlightClassType.BUSINESS, FareType.FLEXIBLE, Decimal("1100.00"),Decimal("900.00"), Decimal("-200.00"),Decimal("-18.18"), PriceChangeReason.DYNAMIC_PRICING, 6),
        ("ph-sfo-eco-rise",  "flight-sfo-nrt", FlightClassType.ECONOMY,  FareType.BASIC,    Decimal("180.00"), Decimal("200.00"), Decimal("20.00"),  None,              PriceChangeReason.MANUAL_UPDATE, 48),
        ("ph-cdg-first-promo","flight-cdg-ist", FlightClassType.FIRST,    FareType.FLEXIBLE, Decimal("2000.00"),Decimal("1800.00"),Decimal("-200.00"),Decimal("-10.00"), PriceChangeReason.PROMOTION, 3),
    ]

    for (label, flight_label, class_type, fare_type, old_price, new_price,
         delta, pct_drop, reason, hours_ago) in price_history_specs:

        ph_id = seed_uuid(label)
        flight_obj = flight_map[flight_label]

        # Resolve fare rule
        fr_label = f"fr-{flight_label}-{class_type.value}-{fare_type.value}"
        fr_obj = fr_map.get(fr_label)
        if not fr_obj:
            log("⚠️", f"Fare rule {fr_label} not found, skipping price history")
            continue

        _, created = get_or_create(
            db, PriceHistory, ph_id,
            fare_rule_id=fr_obj.id,
            flight_id=flight_obj.id,
            class_type=class_type,
            fare_type=fare_type,
            old_price=old_price,
            new_price=new_price,
            price_delta=delta,
            percentage_drop=pct_drop,
            currency="USD",
            changed_by=ops_agent.id,
            reason=reason,
        )
        status_str = "✅ created" if created else "⏩ exists"
        direction = "📉 drop" if delta < 0 else "📈 rise"
        log(direction[0:1], f"{flight_obj.flight_number} {class_type.value}/{fare_type.value}: ${old_price}→${new_price} ({reason.value}) — {status_str}")

    db.flush()


# ────────────────────────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ────────────────────────────────────────────────────────────────
def run_seed():
    print("=" * 60)
    print("  FMS — Production Database Seeder  ")
    print("=" * 60)
    print(f"  Database: {os.getenv('DATABASE_URL', '(from .env)')[:60]}...")
    print(f"  Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    db: Session = SessionLocal()
    try:
        # Phase 1: Core entities
        user_map = seed_users(db)
        flight_map = seed_flights(db, user_map)
        fc_map = seed_flight_classes(db, flight_map)
        seat_map = seed_seats(db, flight_map)
        fr_map = seed_fare_rules(db, flight_map)

        # Phase 2: Transactional entities
        booking_map = seed_bookings(db, user_map, flight_map, seat_map)
        seed_refunds(db, booking_map)
        seed_waitlist(db, user_map, flight_map)

        # Phase 3: Automation entities (Phase 6A)
        seed_notifications(db, user_map, flight_map, booking_map)
        seed_price_history(db, flight_map, fr_map, user_map)

        # Commit all changes atomically
        db.commit()

        print("\n" + "=" * 60)
        print("  ✅  SEED COMPLETED SUCCESSFULLY  ")
        print("=" * 60)
        print_summary(db)

    except Exception as e:
        db.rollback()
        print(f"\n❌ SEED FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        db.close()


def print_summary(db: Session):
    """Print a summary count for every seeded table."""
    print("\n📊 Database Summary:")
    counts = {
        "Users":           db.query(User).count(),
        "Flights":         db.query(Flight).count(),
        "Flight Classes":  db.query(FlightClass).count(),
        "Seats":           db.query(FlightSeat).count(),
        "Fare Rules":      db.query(FareRule).count(),
        "Bookings":        db.query(Booking).count(),
        "Booking Items":   db.query(BookingItem).count(),
        "Refunds":         db.query(Refund).count(),
        "Waitlist Entries": db.query(WaitlistEntry).count(),
        "Notifications":   db.query(Notification).count(),
        "Price History":   db.query(PriceHistory).count(),
    }
    for table, count in counts.items():
        print(f"  {table:20s}: {count}")
    print()


if __name__ == "__main__":
    run_seed()
