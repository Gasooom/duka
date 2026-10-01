from app.integrations.payments.base import PaymentProvider, PaymentRequest, ProviderResult

_override: dict[str, PaymentProvider] = {}


def get_payment_provider(name: str) -> PaymentProvider:
    """Factory. Tests may register overrides via `register_provider_override`."""
    if name in _override:
        return _override[name]
    if name == "mock":
        from app.integrations.payments.mock import MockPaymentProvider
        return MockPaymentProvider()
    if name == "momo":
        from app.integrations.payments.momo import MoMoProvider
        return MoMoProvider()
    raise ValueError(f"Unknown payment provider '{name}'")


def register_provider_override(name: str, provider: PaymentProvider | None) -> None:
    if provider is None:
        _override.pop(name, None)
    else:
        _override[name] = provider


__all__ = ["PaymentProvider", "PaymentRequest", "ProviderResult", "get_payment_provider",
           "register_provider_override"]
