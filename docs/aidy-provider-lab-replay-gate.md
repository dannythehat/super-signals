# AIDY Provider Lab replay release gate

This feature remains release-gated until the Super Signals API and web suites and the AIDY provider-feed suites are green on their respective feature heads.

The lifecycle watermark is deliberately independent from the M1 market cursor: `aidy_lifecycle_applied_count` plus its chronological prefix digest records only lifecycle events actually consumed by deterministic replay. Market-data progress cannot advance that prefix.
