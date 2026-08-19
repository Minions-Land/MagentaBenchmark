# Memory Baseline Capability Matrix

This is a stable planning inventory, not a live run-status board or result
claim. A cell states what is needed to execute that family through the public
native benchmark boundary. Exact reasons and requirements live in
`capability-matrix.json`.

Status meanings:

- `pilot-ready`: the released benchmark path and required public artifacts are available.
- `needs-driver`, `needs-corpus`, `needs-artifact`, `needs-data-prep`: the cell needs the named integration input.
- `service-required`: execution needs an external isolated service.
- `unsupported`: the method contract does not target that benchmark.
- `blocked`: no audited public implementation is bundled; do not substitute a different method under the same name.

## Baseline Families

| ID | Family | Equivalence | LoCoMo | LongMemEval | SpreadsheetBench | ALFWorld | AppWorld |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `no-memory` | No memory | `generic-baseline` | `needs-driver` | `needs-driver` | `needs-driver` | `needs-driver` | `needs-driver` |
| `full-text` | Full-text memory | `generic-baseline` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` |
| `frozen-demos` | Frozen demonstrations | `generic-static-demo-control` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` |
| `recency-window` | Recency window | `common-bounded-context-control` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` |
| `random-retrieval` | Random retrieval | `budget-matched-retrieval-negative-control` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` |
| `naive-rag` | Naive sparse RAG | `local-sparse-adaptation` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` |
| `dense-rag` | Dense RAG | `local-dense-retrieval-adaptation` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` |
| `hybrid-rag` | Hybrid RAG | `local-sparse-dense-retrieval-adaptation` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` |
| `raw-trajectory-rag` | Raw trajectory RAG | `coding-agent-sparse-trajectory-baseline` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` |
| `raw-trajectory-dense-rag` | Raw trajectory dense RAG | `coding-agent-dense-trajectory-baseline` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` | `needs-corpus` |
| `rolling-summary` | Rolling summary memory | `generic-bounded-summary-adaptation` | `needs-driver` | `needs-driver` | `needs-driver` | `needs-driver` | `needs-driver` |
| `structured-event-memory` | Structured event memory | `generic-event-fact-extraction-adaptation` | `needs-driver` | `needs-driver` | `needs-driver` | `needs-driver` | `needs-driver` |
| `static-skill` | Static skill | `generic-baseline` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` |
| `human-skill` | Human-authored skill | `runtime-adapter-for-human-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` |
| `llm-skill` | One-shot LLM skill | `runtime-adapter-for-one-shot-llm-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` |
| `tool-native` | Tool-native memory | `coding-agent-baseline` | `needs-driver` | `needs-driver` | `needs-driver` | `needs-driver` | `needs-driver` |
| `skillopt-static` | SkillOpt frozen skill | `runtime-adapter-for-official-artifact` | `unsupported` | `unsupported` | `pilot-ready` | `pilot-ready` | `unsupported` |
| `trace2skill-static` | Trace2Skill frozen skill | `runtime-adapter-for-official-artifact` | `unsupported` | `unsupported` | `pilot-ready` | `unsupported` | `unsupported` |
| `textgrad-static` | TextGrad frozen prompt | `runtime-adapter-only-optimizer-not-run` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` |
| `gepa-static` | GEPA frozen prompt or skill | `runtime-adapter-only-optimizer-not-run` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` |
| `evoskill-static` | EvoSkill frozen skill | `runtime-adapter-only-optimizer-not-run` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` |
| `expel-static` | ExpeL frozen insights | `runtime-adapter-only-distillation-not-run` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` |
| `awm-static` | AWM frozen workflow | `runtime-adapter-only-distillation-not-run` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` |
| `reactive-update-static` | Reactive update artifact | `artifact-contract-only-no-reactive-training` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` |
| `maa-offline` | MAA offline artifact | `artifact-contract-only-no-official-code` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` | `needs-artifact` |
| `mempro-gated` | MemPro gated recall | `coding-agent-adaptation-not-paper-reproduction` | `needs-driver` | `needs-driver` | `needs-driver` | `needs-driver` | `needs-driver` |
| `memskill-gated` | MemSkill gated recall | `coding-agent-adaptation-not-trained-controller` | `needs-driver` | `needs-driver` | `needs-driver` | `needs-driver` | `needs-driver` |
| `invmem-gated` | InvMem service recall | `service-adapter-for-public-repository` | `service-required` | `service-required` | `service-required` | `service-required` | `service-required` |
| `coral-shared` | CORAL shared files | `shared-memory-adaptation-not-multi-agent-reproduction` | `needs-driver` | `needs-driver` | `needs-driver` | `needs-driver` | `needs-driver` |
| `structmem` | StructMem | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `memzero` | MemZero | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `memzero-graph` | MemZero Graph | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `con` | CoN | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `readagent` | ReadAgent | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `memorybank` | MemoryBank | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `a-mem` | A-Mem | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `mem0` | Mem0 | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `langmem` | LangMem | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `memoryos` | MemoryOS | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `lightmem` | LightMem | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `simplemem` | SimpleMem | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `gam` | GAM | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `metamem` | MetaMem | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `openevolve` | OpenEvolve | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `shinkaevolve` | ShinkaEvolve | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `evox` | EvoX | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `coral-best-of-4` | CORAL best of 4 | `unavailable-not-substituted` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |
| `memorax-v0.5` | MemoraX v0.5 | `unavailable` | `blocked` | `blocked` | `blocked` | `blocked` | `blocked` |

## Paper-Native Paths

| ID | Family | Equivalence | LoCoMo | LongMemEval | SpreadsheetBench | ALFWorld | AppWorld |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `mempro-official` | MemPro official program | `paper-native-reproduction` | `pilot-ready` | `pilot-ready` | `unsupported` | `unsupported` | `unsupported` |
| `memskill-official` | MemSkill official controller | `paper-native-reproduction` | `pilot-ready` | `pilot-ready` | `unsupported` | `needs-data-prep` | `unsupported` |
| `skillopt-official` | SkillOpt official evaluator | `paper-native-reproduction` | `unsupported` | `unsupported` | `pilot-ready` | `pilot-ready` | `unsupported` |
| `trace2skill-official` | Trace2Skill official evaluator | `paper-native-reproduction` | `unsupported` | `unsupported` | `pilot-ready` | `unsupported` | `unsupported` |
