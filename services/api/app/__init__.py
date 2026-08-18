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

from app.deterministic_first_ai import install_deterministic_first_ai

install_deterministic_first_ai()

from app.market_at_best_price_override import install_market_at_best_price_override

install_market_at_best_price_override()

from app.bare_gold_now_override import install_bare_gold_now_override

install_bare_gold_now_override()

from app.preserve_broker_protection_override import install_preserve_broker_protection_override

install_preserve_broker_protection_override()

from app.breakeven_tp_repair import install_breakeven_tp_repair

install_breakeven_tp_repair()

from app.paper_live_entry_guard import install_paper_live_entry_guard

install_paper_live_entry_guard()

from app.provider_entry_reliability_overrides import install_provider_entry_reliability_overrides

install_provider_entry_reliability_overrides()
