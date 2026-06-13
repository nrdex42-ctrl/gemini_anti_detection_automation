"""Prometheus metrics and observability for GraphQL posting tier."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

try:
    from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry
except ImportError:
    Counter = Histogram = Gauge = CollectorRegistry = None

logger = logging.getLogger(__name__)


@dataclass
class RequestContext:
    """Request correlation context for distributed tracing."""
    
    request_id: str
    trace_id: str
    account_id: str
    page_id: str
    operation: str
    started_at: float
    
    def __post_init__(self):
        if not self.request_id:
            self.request_id = str(uuid.uuid4())
        if not self.trace_id:
            self.trace_id = str(uuid.uuid4())
    
    @staticmethod
    def create(account_id: str, page_id: str, operation: str = 'post') -> 'RequestContext':
        """Create a new request context with generated IDs."""
        return RequestContext(
            request_id=str(uuid.uuid4()),
            trace_id=str(uuid.uuid4()),
            account_id=account_id,
            page_id=page_id,
            operation=operation,
            started_at=time.time(),
        )
    
    def elapsed_ms(self) -> float:
        """Return elapsed time in milliseconds."""
        return (time.time() - self.started_at) * 1000
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging."""
        return asdict(self)
    
    def to_log_context(self) -> Dict[str, Any]:
        """Convert to structured logging context."""
        return {
            'request_id': self.request_id,
            'trace_id': self.trace_id,
            'account_id': self.account_id,
            'page_id': self.page_id,
            'operation': self.operation,
            'elapsed_ms': self.elapsed_ms(),
        }


class GraphQLMetrics:
    """Prometheus metrics for GraphQL posting operations."""
    
    def __init__(self, registry: Optional[Any] = None):
        """Initialize metrics with optional custom registry."""
        if Counter is None:
            logger.warning("prometheus_client not installed; metrics disabled")
            self.enabled = False
            return
        
        self.enabled = True
        self.registry = registry or CollectorRegistry()
        
        # Request latency
        self.request_latency = Histogram(
            'graphql_request_duration_seconds',
            'GraphQL request latency in seconds',
            buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
            labelnames=['mutation', 'result'],
            registry=self.registry,
        )
        
        # Error counter
        self.errors_total = Counter(
            'graphql_errors_total',
            'Total GraphQL errors',
            labelnames=['error_code', 'mutation'],
            registry=self.registry,
        )
        
        # Payload size distribution
        self.request_size = Histogram(
            'graphql_request_payload_bytes',
            'GraphQL request payload size',
            buckets=(100, 500, 1000, 5000, 10000, 50000),
            labelnames=['post_type'],
            registry=self.registry,
        )
        
        # Response time by error code
        self.response_time_by_error = Histogram(
            'graphql_response_time_by_error_seconds',
            'Response time by error code',
            labelnames=['error_code'],
            registry=self.registry,
        )
        
        # Success counter
        self.posts_successful = Counter(
            'graphql_posts_successful_total',
            'Total successful posts',
            labelnames=['post_type'],
            registry=self.registry,
        )
        
        # Failed posts counter
        self.posts_failed = Counter(
            'graphql_posts_failed_total',
            'Total failed posts',
            labelnames=['post_type', 'reason'],
            registry=self.registry,
        )
        
        # Idempotency cache hits
        self.idempotency_hits = Counter(
            'graphql_idempotency_hits_total',
            'Total idempotency cache hits',
            registry=self.registry,
        )
        
        # Token rotations
        self.token_rotations = Counter(
            'graphql_token_rotations_total',
            'Total token rotations',
            labelnames=['reason'],
            registry=self.registry,
        )
        
        # Fallback invocations
        self.fallbacks_invoked = Counter(
            'graphql_fallbacks_invoked_total',
            'Total fallback strategy invocations',
            labelnames=['strategy', 'reason'],
            registry=self.registry,
        )
        
        # In-flight requests
        self.in_flight_requests = Gauge(
            'graphql_in_flight_requests',
            'Current in-flight requests',
            labelnames=['operation'],
            registry=self.registry,
        )
    
    async def record_request(
        self,
        ctx: RequestContext,
        mutation: str,
        duration_sec: float,
        success: bool,
        error_code: Optional[str] = None,
        payload_size: Optional[int] = None,
    ) -> None:
        """Record metrics for a GraphQL request."""
        if not self.enabled:
            return
        
        result = 'success' if success else 'error'
        
        # Latency
        self.request_latency.labels(
            mutation=mutation,
            result=result,
        ).observe(duration_sec)
        
        # Error code
        if error_code:
            self.errors_total.labels(
                error_code=error_code,
                mutation=mutation,
            ).inc()
            self.response_time_by_error.labels(error_code=error_code).observe(duration_sec)
        
        # Payload size
        if payload_size:
            post_type = 'image' if payload_size > 5000 else 'text'
            self.request_size.labels(post_type=post_type).observe(payload_size)
        
        # Log structured event
        log_ctx = ctx.to_log_context()
        log_ctx.update({
            'event': 'graphql_request',
            'mutation': mutation,
            'result': result,
            'error_code': error_code,
            'payload_size': payload_size,
            'duration_ms': duration_sec * 1000,
        })
        logger.info(json.dumps(log_ctx, default=str))
    
    async def record_post_success(self, ctx: RequestContext, post_type: str = 'text') -> None:
        """Record successful post."""
        if not self.enabled:
            return
        self.posts_successful.labels(post_type=post_type).inc()
    
    async def record_post_failure(self, ctx: RequestContext, post_type: str, reason: str) -> None:
        """Record failed post."""
        if not self.enabled:
            return
        self.posts_failed.labels(post_type=post_type, reason=reason).inc()
    
    async def record_idempotency_hit(self, ctx: RequestContext) -> None:
        """Record idempotency cache hit."""
        if not self.enabled:
            return
        self.idempotency_hits.inc()
    
    async def record_token_rotation(self, account_id: str, reason: str) -> None:
        """Record token rotation."""
        if not self.enabled:
            return
        self.token_rotations.labels(reason=reason).inc()
        logger.info(json.dumps({
            'event': 'token_rotation',
            'account_id': account_id,
            'reason': reason,
        }))
    
    async def record_fallback(self, ctx: RequestContext, strategy: str, reason: str) -> None:
        """Record fallback strategy invocation."""
        if not self.enabled:
            return
        self.fallbacks_invoked.labels(strategy=strategy, reason=reason).inc()
        log_ctx = ctx.to_log_context()
        log_ctx.update({
            'event': 'fallback_invoked',
            'strategy': strategy,
            'reason': reason,
        })
        logger.warning(json.dumps(log_ctx))
    
    def set_in_flight(self, operation: str, count: int) -> None:
        """Set current in-flight request count."""
        if not self.enabled:
            return
        self.in_flight_requests.labels(operation=operation).set(count)
    
    def get_metrics(self) -> Optional[Any]:
        """Return the Prometheus registry (for export)."""
        if not self.enabled:
            return None
        return self.registry


# Global metrics instance
_global_metrics: Optional[GraphQLMetrics] = None


def get_metrics() -> Optional[GraphQLMetrics]:
    """Get or create global metrics instance."""
    global _global_metrics
    if _global_metrics is None:
        try:
            _global_metrics = GraphQLMetrics()
        except Exception as e:
            logger.warning(f"Failed to initialize metrics: {e}")
    return _global_metrics


def init_metrics(registry: Optional[Any] = None) -> GraphQLMetrics:
    """Initialize global metrics with optional custom registry."""
    global _global_metrics
    _global_metrics = GraphQLMetrics(registry)
    return _global_metrics
