# MagentaBenchmark Skills

This directory contains project management and experiment execution skills integrated from ZHE_SKILL.

## Available Skills

### Project Management
- **project-management**: End-to-end management for research, benchmarks, and multi-agent coding projects
- **benchmark-operations**: Operate long-running benchmarks with frozen protocols and evidence-backed receipts
- **experiment-infrastructure**: Safely manage shared experiment infrastructure including APIs, GPUs, and dependencies
- **experiment-integrity**: Five sentinel gates for completeness, provenance, and claim validation

## Usage

These skills provide structured workflows for:
- Establishing project boundaries and resource contracts
- Freezing experiment protocols before execution
- Managing work packages and distributed execution
- Collecting receipts and verifying evidence
- Coordinating multi-operator work with proper handoffs

See individual SKILL.md files in each subdirectory for detailed instructions.

## Integration with MagentaBenchmark

These skills complement MagentaBenchmark's existing experiment collaboration and lab operations workflows. They provide:

1. **Enhanced project lifecycle management** - from alignment through delivery
2. **Strict resource and boundary enforcement** - preventing accidental modifications
3. **Multi-role coordination** - OWNER, WORKER, REVIEWER patterns
4. **Evidence-based validation** - sentinel gates for experiment integrity
5. **Reproducible experiment contracts** - frozen protocols and provenance tracking

For MagentaBenchmark-specific workflows, continue to follow TOAGENT.md and TOHUMAN.md. Use these skills when you need additional project management structure or are coordinating complex multi-stage experiments.
