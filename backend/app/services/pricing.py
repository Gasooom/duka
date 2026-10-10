"""Price list for usage metering: USAGE_PRICING_FILE, a JSON file the operator keeps up to date from each provider's
own pricing page. Duka ships no prices: without the file, or for a model/message it does not list, usage is still
recorded, only unpriced (cost_micros NULL).

    {
      "version": "2026-10-08",
      "currency": "USD",
      "llm": [
        {"provider": "openai_compat", "model": "<model>", "input_per_1m": "<price>", "output_per_1m": "<price>"},
        {"provider": "openai_compat", "model": "<model name prefix>", "match": "prefix",
         "input_per_1m": "<price>", "output_per_1m": "<price>"}
      ],
      "whatsapp": {
        "billable_statuses": ["success"],
        "templates": [{"name": "<approved template name>", "category": "<category>"}],
        "rules": [
          {"markets": ["<country calling code>"], "message_kind": "free_form", "price_per_message": "<price>"},
          {"markets": ["<country calling code>"], "message_kind": "template", "category": "<category>",
           "price_per_message": "<price>"}
        ]
      }
    }

Prices are per 1M tokens, in `currency`, as JSON strings or numbers; both are read as exact decimals (never floats).
`version` is stored on every priced event, so a later price change never rewrites what was recorded. `match` is
"exact" (default) or "prefix". A call is priced by the model the provider says it served, else by the model Duka
asked for; per model, an exact entry wins over prefix entries and the longest prefix over shorter ones.

WhatsApp (`whatsapp`, optional): a price per message, in `currency`, chosen by the recipient's market (the country
calling code stored on the event, e.g. "250"), the kind of message and, for a template, its category. Duka does not
know a template's category: the operator declares it per template name under `templates`, and a template that is not
declared (or an event with no market) matches no rule and stays unpriced. `billable_statuses` lists which send
outcomes ("success", "failed") incur the price; any other outcome of a real send costs 0, a send that was only
simulated costs 0 (nothing reached WhatsApp), a late failure adds 0 (it corrects an earlier event, never a new send)
and an interrupted send ("unknown") is left unpriced because whether it was delivered is not known. Inbound messages
have no price rule and stay unpriced.
"""
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.core.config import settings


class PricingError(ValueError):
    """USAGE_PRICING_FILE cannot be used (unreadable, not JSON, or not a valid price list)."""


class LLMPrice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=100)
    match: Literal["exact", "prefix"] = "exact"
    input_per_1m: Decimal = Field(ge=0)
    output_per_1m: Decimal = Field(ge=0)


class WhatsAppTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=40)


class WhatsAppRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    markets: tuple[Annotated[str, Field(pattern=r"^[1-9][0-9]{0,2}$")], ...] = Field(min_length=1)
    message_kind: Literal["free_form", "template"]
    category: str | None = Field(None, min_length=1, max_length=40)
    price_per_message: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def _category_only_for_templates(self) -> "WhatsAppRule":
        if (self.message_kind == "template") != (self.category is not None):
            raise ValueError("a template rule needs a category and a free_form rule must not have one")
        return self


class WhatsAppPricing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    billable_statuses: tuple[Literal["success", "failed"], ...]
    templates: tuple[WhatsAppTemplate, ...] = ()
    rules: tuple[WhatsAppRule, ...] = ()

    @model_validator(mode="after")
    def _unambiguous(self) -> "WhatsAppPricing":
        names = [t.name for t in self.templates]
        if len(set(names)) != len(names):
            raise ValueError("whatsapp.templates: a template name is listed twice")
        seen: set[tuple[str, str, str | None]] = set()
        for rule in self.rules:
            for market in rule.markets:
                key = (market, rule.message_kind, rule.category)
                if key in seen:
                    raise ValueError(f"whatsapp.rules: market {market} {rule.message_kind}"
                                     f"{' ' + rule.category if rule.category else ''} is priced twice")
                seen.add(key)
        return self

    def rule(self, market: str, message_kind: str, category: str | None) -> WhatsAppRule | None:
        return next((r for r in self.rules
                     if market in r.markets and r.message_kind == message_kind and r.category == category), None)

    def category_of(self, template_name: str | None) -> str | None:
        return next((t.category for t in self.templates if t.name == template_name), None)


class PriceList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1, max_length=40)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    llm: tuple[LLMPrice, ...] = ()
    whatsapp: WhatsAppPricing | None = None

    @model_validator(mode="after")
    def _one_price_per_entry(self) -> "PriceList":
        seen: set[tuple[str, str, str]] = set()
        for p in self.llm:
            if (p.provider, p.model, p.match) in seen:
                raise ValueError(f"llm: {p.provider} {p.match} {p.model!r} is listed twice")
            seen.add((p.provider, p.model, p.match))
        return self

    def llm_price(self, provider: str | None, model: str | None) -> LLMPrice | None:
        if not provider or not model:
            return None
        own = [p for p in self.llm if p.provider == provider]
        exact = next((p for p in own if p.match == "exact" and p.model == model), None)
        if exact is not None:
            return exact
        return max((p for p in own if p.match == "prefix" and model.startswith(p.model)),
                   key=lambda p: len(p.model), default=None)


@dataclass(frozen=True)
class Price:
    """What is stored on an event: cost in millionths of `currency` (None = unpriced) and the price list version."""
    cost_micros: int | None = None
    currency: str | None = None
    price_version: str | None = None


UNPRICED = Price()


def load_price_list(path: str) -> PriceList:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"), parse_float=Decimal)
        return PriceList.model_validate(raw)
    except OSError as exc:
        raise PricingError(f"USAGE_PRICING_FILE {path}: cannot be read ({exc.strerror or exc})") from exc
    except json.JSONDecodeError as exc:
        raise PricingError(f"USAGE_PRICING_FILE {path}: not valid JSON ({exc})") from exc
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(x) for x in e['loc']) or 'file'}: {e['msg']}" for e in exc.errors())
        raise PricingError(f"USAGE_PRICING_FILE {path}: {problems}") from exc


@lru_cache(maxsize=8)
def _price_list(path: str) -> PriceList:
    return load_price_list(path)


def current_price_list() -> PriceList | None:
    """The configured price list (read once per process), None when USAGE_PRICING_FILE is not set."""
    path = settings.usage_pricing_file
    return _price_list(path) if path else None


def llm_call_price(*, provider: str | None, model: str | None, configured_model: str | None,
                   input_tokens: int | None, output_tokens: int | None, failed: bool) -> Price:
    """Estimated cost of one AI model call from the tokens the provider reported.

    Unpriced (cost None): no price list, no entry for the served or the configured model, or a successful call
    whose usage the provider did not report. A failed call (no response) costs 0: providers do not bill failed
    requests (one that timed out after the provider did the work may still have been billed; that is not knowable
    here)."""
    price_list = current_price_list()
    if price_list is None:
        return UNPRICED
    entry = price_list.llm_price(provider, model) or price_list.llm_price(provider, configured_model)
    if entry is None or (not failed and (input_tokens is None or output_tokens is None)):
        return Price(price_version=price_list.version)
    if failed:
        return Price(0, price_list.currency, price_list.version)
    # A price per 1M tokens in `currency` is exactly the price of one token in millionths of `currency`.
    with localcontext() as ctx:
        ctx.prec = 60
        micros = input_tokens * entry.input_per_1m + output_tokens * entry.output_per_1m
        return Price(int(micros.to_integral_value(rounding=ROUND_HALF_EVEN)), price_list.currency, price_list.version)


def whatsapp_price(*, kind: str, status: str, is_real: bool | None, message_kind: str | None, template_name: str | None,
                   market: str | None) -> Price:  # is_real None (an unknown outcome) is never priced
    """Estimated cost of one WhatsApp event from the operator's rules; see the module docstring for what is priced
    at 0 (not a send, or not a real one), what is left unpriced (not knowable, or no matching rule) and what
    the rules decide."""
    price_list = current_price_list()
    if price_list is None:
        return UNPRICED
    unpriced = Price(price_version=price_list.version)
    zero = Price(0, price_list.currency, price_list.version)
    config = price_list.whatsapp
    if config is None or kind == "wa_in" or status == "unknown" or is_real is None:
        return unpriced
    if status == "late_failed" or not is_real or status not in config.billable_statuses:
        return zero
    if message_kind is None or market is None:
        return unpriced
    category = config.category_of(template_name) if message_kind == "template" else None
    rule = config.rule(market, message_kind, category)
    if rule is None:
        return unpriced
    with localcontext() as ctx:
        ctx.prec = 60
        micros = rule.price_per_message * 1_000_000
        return Price(int(micros.to_integral_value(rounding=ROUND_HALF_EVEN)), price_list.currency, price_list.version)
