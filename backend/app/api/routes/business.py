import uuid

from fastapi import APIRouter, Depends

from app.api.deps import TenantContext, get_tenant, require_owner
from app.schemas.api import (
    AgentConfigOut,
    AgentConfigPatch,
    BusinessOut,
    BusinessPatch,
    DeliveryZoneIn,
    DeliveryZoneOut,
    SettingsOut,
    SettingsPatch,
    WhatsAppAccountOut,
    WhatsAppConnectIn,
)
from app.services.business_service import BusinessConfigService

router = APIRouter(prefix="/api", tags=["business"])


def svc(ctx: TenantContext) -> BusinessConfigService:
    return BusinessConfigService(ctx.db, ctx.business_id)


@router.get("/business", response_model=BusinessOut)
def get_business(ctx: TenantContext = Depends(get_tenant)):
    return ctx.business


@router.patch("/business", response_model=BusinessOut)
def patch_business(body: BusinessPatch, ctx: TenantContext = Depends(require_owner)):
    b = svc(ctx).update_business(body.model_dump(exclude_unset=True))
    ctx.db.commit()
    return b


@router.get("/business/agent-config", response_model=AgentConfigOut)
def get_agent_config(ctx: TenantContext = Depends(get_tenant)):
    cfg = svc(ctx).agent_config()
    ctx.db.commit()
    return cfg


@router.patch("/business/agent-config", response_model=AgentConfigOut)
def patch_agent_config(body: AgentConfigPatch, ctx: TenantContext = Depends(require_owner)):
    cfg = svc(ctx).update_agent_config(body.model_dump(exclude_unset=True))
    ctx.db.commit()
    return cfg


@router.get("/business/settings", response_model=SettingsOut)
def get_settings(ctx: TenantContext = Depends(get_tenant)):
    s = svc(ctx).settings()
    ctx.db.commit()
    return s


@router.patch("/business/settings", response_model=SettingsOut)
def patch_settings(body: SettingsPatch, ctx: TenantContext = Depends(require_owner)):
    s = svc(ctx).update_settings(body.model_dump(exclude_unset=True))
    ctx.db.commit()
    return s


# Delivery zones ---------------------------------------------------------------
@router.get("/delivery-zones", response_model=list[DeliveryZoneOut])
def list_zones(ctx: TenantContext = Depends(get_tenant)):
    return svc(ctx).delivery_zones()


@router.post("/delivery-zones", response_model=DeliveryZoneOut, status_code=201)
def create_zone(body: DeliveryZoneIn, ctx: TenantContext = Depends(require_owner)):
    z = svc(ctx).upsert_delivery_zone(body.model_dump())
    ctx.db.commit()
    return z


@router.patch("/delivery-zones/{zone_id}", response_model=DeliveryZoneOut)
def update_zone(zone_id: uuid.UUID, body: DeliveryZoneIn, ctx: TenantContext = Depends(require_owner)):
    z = svc(ctx).upsert_delivery_zone(body.model_dump(), zone_id)
    ctx.db.commit()
    return z


@router.delete("/delivery-zones/{zone_id}", status_code=204)
def delete_zone(zone_id: uuid.UUID, ctx: TenantContext = Depends(require_owner)):
    svc(ctx).delete_delivery_zone(zone_id)
    ctx.db.commit()


# WhatsApp accounts -------------------------------------------------------------
def _acct(a) -> WhatsAppAccountOut:
    out = WhatsAppAccountOut.model_validate(a)
    out.has_access_token = bool(a.access_token_encrypted)
    return out


@router.get("/whatsapp/accounts", response_model=list[WhatsAppAccountOut])
def list_accounts(ctx: TenantContext = Depends(get_tenant)):
    return [_acct(a) for a in svc(ctx).whatsapp_accounts()]


@router.post("/whatsapp/accounts", response_model=WhatsAppAccountOut, status_code=201)
def connect_account(body: WhatsAppConnectIn, ctx: TenantContext = Depends(require_owner)):
    a = svc(ctx).connect_whatsapp(**body.model_dump())
    ctx.db.commit()
    return _acct(a)


@router.delete("/whatsapp/accounts/{account_id}", status_code=204)
def delete_account(account_id: uuid.UUID, ctx: TenantContext = Depends(require_owner)):
    svc(ctx).delete_whatsapp(account_id)
    ctx.db.commit()
