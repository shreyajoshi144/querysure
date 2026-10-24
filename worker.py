"""QuerySure RQ worker entrypoint."""
from rq import Queue, Worker
import redis
from config import REDIS_URL
from jobs import QUEUE_NAME
from observability.logging_setup import configure_logging
from observability import errors as obs_errors, gauges as obs_gauges

if not REDIS_URL:
    raise RuntimeError("REDIS_URL is required for the RQ worker")

configure_logging()
obs_errors.init_sentry()
# Heartbeat document (qs:g:worker:<host>:<pid>, short TTL): its presence is the worker-liveness signal used by /api/ready's
# degraded state, the ops dashboard and the worker_down alert. A dead worker disappears on its own when the TTL expires.
obs_gauges.start_publisher("worker", interval=2.0)

connection = redis.Redis.from_url(REDIS_URL)
Worker([Queue(QUEUE_NAME, connection=connection)], connection=connection).work()
