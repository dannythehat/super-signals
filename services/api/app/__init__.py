"""Super Signals API package.

Explicit present-tense provider protection/exit instructions are production safety
policy, not test helpers. Install deterministic policy overrides as soon as the
application package is imported so the live listener, V1 policy and regression tests
all run the same broker-facing rules.
"""

from app.literal_management_overrides import install_literal_management_overrides
from app.plain_range_order_type_override import install_plain_range_order_type_override

install_literal_management_overrides()
install_plain_range_order_type_override()
