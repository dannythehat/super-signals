"""Super Signals API package.

Explicit present-tense provider protection/exit instructions are a production safety
policy, not a test helper. Install the deterministic management overrides as soon as
the application package is imported so the live listener, V1 policy and AI pipeline
all use the same close/SL/BE grammar exercised by regression tests.
"""

from app.literal_management_overrides import install_literal_management_overrides

install_literal_management_overrides()
