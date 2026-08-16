from sqlalchemy import Column, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime
import uuid
from app.db.session import Base


class BalanceTransaction(Base):
    __tablename__ = "balance_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    operator_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    delta = Column(Numeric(18, 2), nullable=False)
    before_balance = Column(Numeric(18, 2), nullable=False)
    after_balance = Column(Numeric(18, 2), nullable=False)
    reason = Column(String(64), nullable=False, default="ADMIN_ADJUST")
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<BalanceTransaction {self.user_id} {self.delta}>"
