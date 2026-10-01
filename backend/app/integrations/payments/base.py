from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class PaymentRequest:
    payment_id: str  # our internal id, sent to the provider as external id
    order_number: str
    amount: Decimal
    currency: str
    payer_phone: str
    description: str


@dataclass
class ProviderResult:
    reference: str
    status: str  # pending | successful | failed
    raw: dict[str, Any] = field(default_factory=dict)
    failure_reason: str | None = None


class PaymentProvider(ABC):
    """Provider-agnostic payment interface.

    Contract: `request_payment` only *initiates* a payment and must return status
    'pending' unless the provider synchronously confirms. A payment is only considered
    successful after `get_status` (or a verified callback) reports success.
    """

    name: str

    @abstractmethod
    def request_payment(self, req: PaymentRequest) -> ProviderResult: ...

    @abstractmethod
    def get_status(self, reference: str) -> ProviderResult: ...

    # Whether the provider's callbacks must be re-verified by calling get_status.
    verify_callbacks_via_status: bool = True
