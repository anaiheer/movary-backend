from sqlalchemy import Column, DateTime, Enum, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime
import uuid
import enum
from app.db.session import Base


class InvitationStatus(str, enum.Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    EXPIRED = "EXPIRED"
    CANCELED = "CANCELED"


class Invitation(Base):
    __tablename__ = "invitations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token = Column(String(128), unique=True, nullable=False, index=True)
    invitee_email = Column(String(255), nullable=False)
    inviter_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    plan_id = Column(UUID(as_uuid=True), ForeignKey("plans.id"), nullable=True)
    initial_balance = Column(Numeric(18, 2), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    status = Column(Enum(InvitationStatus), default=InvitationStatus.PENDING)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Invitation {self.invitee_email}>"
