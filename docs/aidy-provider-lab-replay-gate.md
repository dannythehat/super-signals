# AIDY Provider Lab replay release gate

This feature remains release-gated until the Super Signals API and web suites and the AIDY provider-feed suites are green on their respective feature heads.

The lifecycle watermark is deliberately independent from the M1 market cursor: `aidy_lifecycle_applied_count` plus its chronological prefix digest records only lifecycle events actually consumed by deterministic replay. Market-data progress cannot advance that prefix.

The Provider Lab runtime also limits each replay request to one hour even though AIDY's read-only endpoint supports a 48-hour bounded window. This prevents a persistent missing M1 minute from turning overlap retries into another D1 read-budget amplifier.