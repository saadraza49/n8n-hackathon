from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.enums import PriceChangeReason
from app.models.fare_rule import FareRule
from app.models.flight import Flight
from app.models.flight_class import FlightClass
from app.models.price_history import PriceHistory
from app.schemas.fare_rule import FareRuleCreate, FareRuleUpdate



class FareService:
    @staticmethod
    def create_fare_rule(
        db: Session, flight_id: UUID, payload: FareRuleCreate, actor_id: UUID
    ) -> FareRule:
        """Create a new fare rule for a flight class and record an audit log."""
        # 1. Verify flight exists
        flight = db.query(Flight).filter(Flight.id == flight_id).first()
        if not flight:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Flight with id '{flight_id}' not found.",
            )

        # 2. Verify class exists on the flight
        flight_class = (
            db.query(FlightClass)
            .filter(
                FlightClass.flight_id == flight_id,
                FlightClass.class_type == payload.class_type,
            )
            .first()
        )
        if not flight_class:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Class '{payload.class_type.value}' is not offered on flight '{flight.flight_number}'.",
            )

        # 3. Check unique constraint: (flight_id, class_type, fare_type)
        existing_rule = (
            db.query(FareRule)
            .filter(
                FareRule.flight_id == flight_id,
                FareRule.class_type == payload.class_type,
                FareRule.fare_type == payload.fare_type,
            )
            .first()
        )
        if existing_rule:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Fare rule for class '{payload.class_type.value}' and fare type '{payload.fare_type.value}' "
                    f"already exists on this flight."
                ),
            )

        # 4. Create and persist fare rule
        rule = FareRule(
            flight_id=flight_id,
            class_type=payload.class_type,
            fare_type=payload.fare_type,
            price=payload.price,
            currency=payload.currency.strip().upper(),
            changes_allowed=payload.changes_allowed,
            seat_selection_allowed=payload.seat_selection_allowed,
            refundable=payload.refundable,
            credit_only=payload.credit_only,
            cancellation_cutoff_minutes=payload.cancellation_cutoff_minutes,
        )
        db.add(rule)
        db.flush()

        # 5. Audit log
        audit = AuditLog(
            user_id=actor_id,
            action="CREATE_FARE_RULE",
            entity_type="FARE_RULE",
            entity_id=rule.id,
            old_values=None,
            new_values={
                "flight_id": str(flight_id),
                "class_type": rule.class_type.value,
                "fare_type": rule.fare_type.value,
                "price": str(rule.price),
                "currency": rule.currency,
                "changes_allowed": rule.changes_allowed,
                "seat_selection_allowed": rule.seat_selection_allowed,
                "refundable": rule.refundable,
                "credit_only": rule.credit_only,
                "cancellation_cutoff_minutes": rule.cancellation_cutoff_minutes,
            },
        )
        db.add(audit)

        db.commit()
        db.refresh(rule)
        return rule

    @staticmethod
    def list_fare_rules(db: Session, flight_id: UUID) -> list[FareRule]:
        """List all fare rules configured for a flight."""
        flight = db.query(Flight).filter(Flight.id == flight_id).first()
        if not flight:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Flight with id '{flight_id}' not found.",
            )
        return (
            db.query(FareRule)
            .filter(FareRule.flight_id == flight_id)
            .order_by(FareRule.class_type.asc(), FareRule.fare_type.asc())
            .all()
        )

    @staticmethod
    def update_fare_rule(
        db: Session, flight_id: UUID, fare_rule_id: UUID, payload: FareRuleUpdate, actor_id: UUID
    ) -> FareRule:
        """Update an existing fare rule and record an audit log."""
        rule = (
            db.query(FareRule)
            .filter(FareRule.id == fare_rule_id, FareRule.flight_id == flight_id)
            .first()
        )
        if not rule:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Fare rule with id '{fare_rule_id}' not found on flight '{flight_id}'.",
            )

        old_values = {
            "price": str(rule.price),
            "currency": rule.currency,
            "changes_allowed": rule.changes_allowed,
            "seat_selection_allowed": rule.seat_selection_allowed,
            "refundable": rule.refundable,
            "credit_only": rule.credit_only,
            "cancellation_cutoff_minutes": rule.cancellation_cutoff_minutes,
        }

        new_values = {}
        if payload.price is not None:
            if payload.price != rule.price:
                old_p = rule.price
                new_p = payload.price
                delta = new_p - old_p
                pct_drop = None
                if new_p < old_p and old_p > 0:
                    pct_drop = round(((old_p - new_p) / old_p) * 100, 2)

                price_hist = PriceHistory(
                    fare_rule_id=rule.id,
                    flight_id=rule.flight_id,
                    class_type=rule.class_type,
                    fare_type=rule.fare_type,
                    old_price=old_p,
                    new_price=new_p,
                    price_delta=delta,
                    percentage_drop=pct_drop,
                    currency=rule.currency,
                    changed_by=actor_id,
                    reason=PriceChangeReason.MANUAL_UPDATE,
                )
                db.add(price_hist)

            rule.price = payload.price
            new_values["price"] = str(rule.price)
        if payload.currency is not None:
            rule.currency = payload.currency.strip().upper()
            new_values["currency"] = rule.currency
        if payload.changes_allowed is not None:
            rule.changes_allowed = payload.changes_allowed
            new_values["changes_allowed"] = rule.changes_allowed
        if payload.seat_selection_allowed is not None:
            rule.seat_selection_allowed = payload.seat_selection_allowed
            new_values["seat_selection_allowed"] = rule.seat_selection_allowed
        if payload.refundable is not None:
            rule.refundable = payload.refundable
            new_values["refundable"] = rule.refundable
        if payload.credit_only is not None:
            rule.credit_only = payload.credit_only
            new_values["credit_only"] = rule.credit_only
        if payload.cancellation_cutoff_minutes is not None:
            rule.cancellation_cutoff_minutes = payload.cancellation_cutoff_minutes
            new_values["cancellation_cutoff_minutes"] = rule.cancellation_cutoff_minutes

        if new_values:
            audit = AuditLog(
                user_id=actor_id,
                action="UPDATE_FARE_RULE",
                entity_type="FARE_RULE",
                entity_id=rule.id,
                old_values=old_values,
                new_values=new_values,
            )
            db.add(audit)

        db.commit()
        db.refresh(rule)
        return rule

    @staticmethod
    def delete_fare_rule(db: Session, flight_id: UUID, fare_rule_id: UUID, actor_id: UUID) -> None:
        """Delete a fare rule and record an audit log."""
        rule = (
            db.query(FareRule)
            .filter(FareRule.id == fare_rule_id, FareRule.flight_id == flight_id)
            .first()
        )
        if not rule:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Fare rule with id '{fare_rule_id}' not found on flight '{flight_id}'.",
            )

        old_values = {
            "flight_id": str(rule.flight_id),
            "class_type": rule.class_type.value,
            "fare_type": rule.fare_type.value,
            "price": str(rule.price),
            "currency": rule.currency,
        }

        db.delete(rule)

        audit = AuditLog(
            user_id=actor_id,
            action="DELETE_FARE_RULE",
            entity_type="FARE_RULE",
            entity_id=fare_rule_id,
            old_values=old_values,
            new_values=None,
        )
        db.add(audit)

        db.commit()
