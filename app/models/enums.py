from enum import Enum


class UserRole(str, Enum):
    PASSENGER = "PASSENGER"
    OPS_AGENT = "OPS_AGENT"
    SUPER_ADMIN = "SUPER_ADMIN"


class FlightStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    DELAYED = "DELAYED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class FlightClassType(str, Enum):
    FIRST = "FIRST"
    BUSINESS = "BUSINESS"
    ECONOMY = "ECONOMY"


class SeatStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    HELD = "HELD"
    BOOKED = "BOOKED"
    BLOCKED = "BLOCKED"


class FareType(str, Enum):
    BASIC = "BASIC"
    FLEXIBLE = "FLEXIBLE"


class BookingStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class BookingItemStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class RefundStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RefundType(str, Enum):
    MONETARY = "MONETARY"
    CREDIT = "CREDIT"
    NONE = "NONE"


class RefundReason(str, Enum):
    CUSTOMER_CANCELLATION = "CUSTOMER_CANCELLATION"
    AIRLINE_CANCELLATION = "AIRLINE_CANCELLATION"
    SCHEDULE_CHANGE = "SCHEDULE_CHANGE"
    BOOKING_CHANGE = "BOOKING_CHANGE"
    OTHER = "OTHER"


class WaitlistStatus(str, Enum):
    WAITING = "WAITING"
    PROMOTED = "PROMOTED"
    CLAIMED = "CLAIMED"
    CONVERTED = "CONVERTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class WaitlistEntryType(str, Enum):
    WAITLIST = "WAITLIST"
    STANDBY = "STANDBY"


class NotificationType(str, Enum):
    CHECK_IN_REMINDER = "CHECK_IN_REMINDER"
    PRICE_DROP_ALERT = "PRICE_DROP_ALERT"
    FLIGHT_CANCELLATION = "FLIGHT_CANCELLATION"
    SCHEDULE_CHANGE = "SCHEDULE_CHANGE"
    BOOKING_CONFIRMATION = "BOOKING_CONFIRMATION"


class NotificationStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SENT = "SENT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NotificationChannel(str, Enum):
    EMAIL = "EMAIL"
    SMS = "SMS"
    IN_APP = "IN_APP"


class PriceChangeReason(str, Enum):
    MANUAL_UPDATE = "MANUAL_UPDATE"
    DYNAMIC_PRICING = "DYNAMIC_PRICING"
    PROMOTION = "PROMOTION"
    INITIAL_CONFIG = "INITIAL_CONFIG"




