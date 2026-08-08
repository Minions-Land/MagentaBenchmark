# Retroactive validity notice

Every benchmark artifact already stored in this directory is retroactively
invalid as positive MagentaBench evidence. The files are intentionally retained
unchanged as regression inputs and historical counterexamples.

In particular, these records do not establish that MagentaBench has completed a
real benchmark through the current protocol. Depending on the artifact, they
lack one or more of: positive network observations, exact authoritative-metric
binding, complete parent-run/child-attempt lineage, byte-verified referenced
artifacts, a content-addressed record index, and a standalone-verifiable report.
Several reports also encode gate behavior that has since been rejected.

Do not repair these historical JSON files in place. A valid future execution
must be written as a new immutable record and must pass the current standalone
verifier from persisted bytes. Until then, the only legitimate use of these
files is to prove that current gates continue to reject known-bad evidence.

