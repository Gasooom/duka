"""Concrete tenant repositories."""
from app.models import (
    AgentConfig,
    AgentRun,
    BusinessSettings,
    Cart,
    CartItem,
    Conversation,
    Customer,
    DeliveryZone,
    InventoryMovement,
    KnowledgeChunk,
    KnowledgeDocument,
    Message,
    Order,
    OrderItem,
    Payment,
    Product,
    ProductCategory,
    User,
    WebhookEvent,
    WhatsAppAccount,
)
from app.repositories.base import TenantRepository


class UserRepo(TenantRepository[User]):
    model = User


class SettingsRepo(TenantRepository[BusinessSettings]):
    model = BusinessSettings


class AgentConfigRepo(TenantRepository[AgentConfig]):
    model = AgentConfig


class WhatsAppAccountRepo(TenantRepository[WhatsAppAccount]):
    model = WhatsAppAccount


class DeliveryZoneRepo(TenantRepository[DeliveryZone]):
    model = DeliveryZone


class CategoryRepo(TenantRepository[ProductCategory]):
    model = ProductCategory


class ProductRepo(TenantRepository[Product]):
    model = Product


class InventoryRepo(TenantRepository[InventoryMovement]):
    model = InventoryMovement


class CustomerRepo(TenantRepository[Customer]):
    model = Customer


class ConversationRepo(TenantRepository[Conversation]):
    model = Conversation


class MessageRepo(TenantRepository[Message]):
    model = Message


class AgentRunRepo(TenantRepository[AgentRun]):
    model = AgentRun


class CartRepo(TenantRepository[Cart]):
    model = Cart


class CartItemRepo(TenantRepository[CartItem]):
    model = CartItem


class OrderRepo(TenantRepository[Order]):
    model = Order


class OrderItemRepo(TenantRepository[OrderItem]):
    model = OrderItem


class PaymentRepo(TenantRepository[Payment]):
    model = Payment


class KnowledgeDocumentRepo(TenantRepository[KnowledgeDocument]):
    model = KnowledgeDocument


class KnowledgeChunkRepo(TenantRepository[KnowledgeChunk]):
    model = KnowledgeChunk


class WebhookEventRepo(TenantRepository[WebhookEvent]):
    model = WebhookEvent
