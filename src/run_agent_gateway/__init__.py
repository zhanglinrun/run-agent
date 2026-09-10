"""Optional durable Gateway host and its channel extension contract."""

from run_agent_gateway.contracts import (
    AdmissionReceipt,
    AdmissionRejected,
    Assignment,
    GatewayLimits,
    GatewayOwner,
    RouteIdentity,
    Submission,
)
from run_agent_gateway.extensions import (
    GATEWAY_EXTENSION_API_VERSION,
    GatewayExtensionAPI,
    GatewayExtensionError,
    GatewayExtensionHost,
)
from run_agent_gateway.gateway import (
    AgentGateway,
    BoundedIngress,
    GatewayAdapter,
    InboundMessage,
    QueueGatewayAdapter,
)
from run_agent_gateway.identity import IdentityPolicy, IdentityRule
from run_agent_gateway.outbox import Delivery
from run_agent_gateway.repository import GatewayRepository
from run_agent_gateway.scheduler import GatewayScheduler

__all__ = [
    "GATEWAY_EXTENSION_API_VERSION",
    "AdmissionReceipt",
    "AdmissionRejected",
    "AgentGateway",
    "Assignment",
    "BoundedIngress",
    "Delivery",
    "GatewayAdapter",
    "GatewayExtensionAPI",
    "GatewayExtensionError",
    "GatewayExtensionHost",
    "GatewayLimits",
    "GatewayOwner",
    "GatewayRepository",
    "GatewayScheduler",
    "IdentityPolicy",
    "IdentityRule",
    "InboundMessage",
    "QueueGatewayAdapter",
    "RouteIdentity",
    "Submission",
]
