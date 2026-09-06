from uuid import UUID

from app.models.enums import FlightClassType, SeatStatus
from app.models.flight_seat import FlightSeat


def generate_physical_seats(
    flight_id: UUID,
    first_seats_count: int,
    business_seats_count: int,
    economy_seats_count: int,
) -> list[FlightSeat]:
    """Generate physical seat records for a flight with a deterministic cabin layout.
    
    Cabin configurations:
      - FIRST Class: 4 seats per row (A, B, C, D)
      - BUSINESS Class: 6 seats per row (A, B, C, D, E, F)
      - ECONOMY Class: 6 seats per row (A, B, C, D, E, F)
    
    Rows are sequentially numbered across classes to ensure natural airplane layout
    and strict uniqueness of seat numbers across the flight.
    
    Returns:
        list[FlightSeat]: Exactly (first_seats_count + business_seats_count + economy_seats_count) seat records.
    """
    seats: list[FlightSeat] = []
    current_row = 1

    # Configuration for each cabin class
    cabin_configs = [
        (FlightClassType.FIRST, first_seats_count, ["A", "B", "C", "D"]),
        (FlightClassType.BUSINESS, business_seats_count, ["A", "B", "C", "D", "E", "F"]),
        (FlightClassType.ECONOMY, economy_seats_count, ["A", "B", "C", "D", "E", "F"]),
    ]

    for class_type, target_count, seat_letters in cabin_configs:
        seats_generated_for_class = 0
        seats_per_row = len(seat_letters)

        while seats_generated_for_class < target_count:
            for letter in seat_letters:
                if seats_generated_for_class >= target_count:
                    break
                seat_number = f"{current_row}{letter}"
                seat = FlightSeat(
                    flight_id=flight_id,
                    seat_number=seat_number,
                    class_type=class_type,
                    status=SeatStatus.AVAILABLE,
                    hold_expires_at=None,
                )
                seats.append(seat)
                seats_generated_for_class += 1
            current_row += 1

    return seats
