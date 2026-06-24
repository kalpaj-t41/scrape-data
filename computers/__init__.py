"""
computers package.

Importing this package imports every computer module, firing each module's
@registry.register side-effect so the singleton MetricRegistry is fully
populated. batch_runner / validate import `registry` and `ComputeContext`
from here.
"""

# Import every computer module to register it with the singleton registry.
from computers import (  # noqa: F401
    adoption,
    agent_hours,
    parallel_agents,
    depth,
    harness,
    skills,
    trust,
    outcomes,
    velocity,
    consistency,
    efficiency,
    usefulness,
    agent_quality,
    qaah,
    composite,
    equity,
)

from computers.base import ComputeContext  # noqa: F401
from computers.registry import registry  # noqa: F401
