# MagentaBenchmark Skills

This directory contains optional, repository-local skills for project management
and experiment execution. They supplement the canonical MagentaBenchmark entry
points; they do not replace `AGENTS.md`, `TOAGENT.md`, `bmp-lab`, GitHub Issues,
or the generated experiment ledger.

## Available Skills

### Project Management
- **project-management**: End-to-end management for research, benchmarks, and multi-agent coding projects
- **benchmark-operations**: Operate long-running benchmarks with frozen protocols and evidence-backed receipts
- **experiment-infrastructure**: Safely manage shared experiment infrastructure including APIs, GPUs, and dependencies
- **experiment-integrity**: Ordered sentinels for completeness, provenance, and claim validation

## Usage

These skills provide structured workflows for:
- Establishing project boundaries and resource contracts
- Freezing experiment protocols before execution
- Managing work packages and distributed execution
- Collecting receipts and verifying evidence
- Coordinating multi-operator work with proper handoffs

See individual SKILL.md files in each subdirectory for detailed instructions.

## Integration with MagentaBenchmark

These skills complement MagentaBenchmark's existing experiment collaboration and lab operations workflows. Repository-local authority, leases, evidence rules, and review policy always win when a generic skill differs. The skills provide:

1. **Enhanced project lifecycle management** - from alignment through delivery
2. **Strict resource and boundary enforcement** - preventing accidental modifications
3. **Multi-role coordination** - owner, worker, and advisory-review patterns
4. **Evidence-based validation** - sentinel gates for experiment integrity
5. **Reproducible experiment contracts** - frozen protocols and provenance tracking

For MagentaBenchmark-specific workflows, start with `AGENTS.md`, `TOAGENT.md`,
and `TOHUMAN.md`. `PoorOtterBob` remains the sole accountable final reviewer;
other review output is advisory. Use these skills only when their additional
project structure helps a complex multi-stage experiment.
