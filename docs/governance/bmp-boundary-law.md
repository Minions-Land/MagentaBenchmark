# BMP Boundary Law

Status: **BINDING FOR MAGENTABENCH.**

This document governs the boundary between the benchmark-side protocol (BMP)
and Magenta's Harness Component Protocol (HCP). BMP belongs to MagentaBench;
HCP belongs to the Magenta agent. The HCP documents cited
below are authoritative for HCP. BMP code, schemas, adapters, tests, and
configuration MUST comply with this law.

The links below are navigation references only and are pinned to Magenta commit
`065d9d0d3231ecd84e62f38511a16577214babfd`. Every experiment must bind the
actual Magenta/HCP source repository, commit, tree digest, and exported
sidecar bytes in its manifest; a mutable branch URL is never execution
evidence. A later Magenta commit is a new interface snapshot and must be
reviewed and recorded explicitly before it is used.

## A. HCP Invariants BMP Must Never Violate

Each row is a testable assertion. A failed assertion is a boundary violation,
not an implementation preference.

| ID | Testable assertion | Authoritative evidence |
|---|---|---|
| HCP-01 | Any assembled HCP path has exactly three role kinds: Client, Server, and Magnet. BMP MUST NOT define, simulate, wrap, or name a fourth role. | [`hcp-architecture.md:202`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/hcp-architecture.md#L202): "Client, Server, and Magnet are the only HCP roles." |
| HCP-02 | One HCP session has exactly one `HcpClient`; BMP MUST NOT instantiate an additional, alternate, benchmark, package, or per-module Client. | [`hcp-architecture.md:200`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/hcp-architecture.md#L200): "Each session owns exactly one `HcpClient`." |
| HCP-03 | Every assembled real Module is owned by its real `HcpServer` in `HcpServer.ts`; BMP MUST NOT create a substitute, facade, anonymous, or generated Server. | [`README.md:14`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/README.md#L14): "Every real Module owns a bare `HcpServer` class in `HcpServer.ts`." |
| HCP-04 | Every selected declared Source is owned by its Source-local `HcpMagnet` in `HcpMagnet.ts`; BMP MUST NOT create a substitute Magnet or select a Source by importing an implementation. | [`README.md:15`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/README.md#L15): "Every declared Source owns a bare `HcpMagnet` class in `HcpMagnet.ts`." |
| HCP-05 | Every returned Magnet exposes exactly one product in the disjoint union Tool, Capability, or Resource. | [`hcp-architecture.md:204`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/hcp-architecture.md#L204): "Every returned Magnet exposes exactly one Tool, Capability, or Resource." |
| HCP-06 | Fan-out, when explicitly allowed, is represented by sibling single-product Magnets with distinct selectors; a multi-product Magnet is invalid. | [`contract.md:18`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/contract.md#L18): "Explicit fan-out returns sibling single-product Magnets with distinct selectors." |
| HCP-07 | A Magnet never creates or returns a Server, and assembly never creates an anonymous Server. | [`hcp-architecture.md:203`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/hcp-architecture.md#L203): "No Magnet exposes `toHcpServer()` and assembly creates no anonymous Server." |
| HCP-08 | Selection, routing, replacement, and lifecycle remain Client-owned; BMP MUST NOT implement a parallel selector, router, lifecycle manager, or lookup service for HCP components. | [`contract.md:17`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/contract.md#L17): "Common selection, routing, replacement, and lifecycle behavior belongs to the Client." |
| HCP-09 | HCP ends after assembly and resolution. BMP MUST NOT put an HCP wrapper or middleware on individual Tool, Capability, or Resource calls. | [`contract.md:19`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/contract.md#L19): "HCP ends after assembly and resolution. Runtime consumers call products directly." |
| HCP-10 | Runtime consumers are Source-agnostic; BMP MUST identify a resolved product through sidecar evidence rather than a Source-specific runtime import or call path. | [`hcp-architecture.md:205`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/hcp-architecture.md#L205): "Consumers are Source-agnostic and HCP stays off the execution hot path." |
| HCP-11 | Repository TOML declarations and real role imports are authoritative for repository components; `sources.generated.ts` is only a disposable projection. BMP MUST NOT treat the projection as an extensibility API. | [`README.md:35`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/README.md#L35): "That file is a disposable projection, not a registry." |
| HCP-12 | BMP MUST NOT maintain a second Server map, Magnet list, default-Source map, product-builder table, central Source switch, or HCP Module/Source registry. | [`contract.md:70`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/contract.md#L70): consumers "must not maintain product-specific Source lists, builder maps, default-Source maps, or central Source switches." |
| HCP-13 | Generic HCP assembly MUST remain free of BMP, Package-acquisition, MCP-discovery, CLI-policy, and other host-specific branches. | [`contract.md:37`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/contract.md#L37): generic assembly "must not parse Package manifests, acquire GitHub releases, discover user MCP configuration, read CLI policy, or branch on Magenta host concepts." |
| HCP-14 | Infrastructure and transport code owns no Server and creates no alternate route or fourth management layer. BMP MUST NOT turn its adapter, evaluator, runner, backend, or transport into an HCP role. | [`hcp-architecture.md:209`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/hcp-architecture.md#L209): "Infrastructure and transports own no Server and create no alternate route." |
| HCP-15 | Dynamic schema-v2 Packages carry and load their own real roles and remain runtime inputs outside the repository-generated projection; BMP MUST NOT register their Modules or Sources itself. | [`hcp-architecture.md:174`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/hcp-architecture.md#L174): "Dynamic Package roles do not appear in `sources.generated.ts`; that file projects only this repository's TOML declarations." |
| HCP-16 | BMP and application code consume only stable package-level/public outputs and MUST NOT deep-import HCP implementation classes. | [`hcp-architecture.md:211`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/hcp-architecture.md#L211): "Application code consumes package-level public APIs, not deep implementation imports." |
| HCP-17 | Rejected, replaced, and unroutable live products are disposed by HCP ownership; BMP MUST NOT retain or revive such product instances. | [`hcp-architecture.md:210`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/hcp-architecture.md#L210): "Rejected, replaced, or unroutable live products are disposed." |
| HCP-18 | HCP names obey the entity tree: level 1 is `Hcp`, level 2 is exactly `Client`, `Server`, or `Magnet`, later capitalized levels require real parent entities, and role identity comes from the path while role files export bare role names. BMP MUST NOT invent HCP-specific type names outside that tree. | [`hcp-naming.md:10`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/hcp-naming.md#L10), [`hcp-naming.md:11`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/hcp-naming.md#L11), and [`hcp-naming.md:24`](https://github.com/Minions-Land/Magenta/blob/065d9d0d3231ecd84e62f38511a16577214babfd/HarnessComponentProtocol/docs/governance/hcp-naming.md#L24). |

## B. Forbidden Constructs

The following names and patterns MUST NOT occur in MagentaBench executable code,
schemas, registries, or importable tests. Documentation is excluded from the
automated content scan because this law must quote the forbidden names.
Patterns are PCRE2 expressions for `rg --pcre2` unless identified as path
patterns.

| ID | Forbidden construct | Exact audit pattern |
|---|---|---|
| BMP-HCP-B01 | Explicit fourth-role names, including the handoff examples | `\b(?:HcpBenchmarkServer|HcpEvaluator)\b` |
| BMP-HCP-B02 | Any HCP-prefixed class, interface, type, enum, protocol, or struct declared by BMP | `\b(?:class|interface|type|enum|protocol|struct)\s+Hcp[A-Za-z0-9_]*\b` |
| BMP-HCP-B03 | Direct use or construction of the three HCP role classes in BMP | `\b(?:HcpClient|HcpServer|HcpMagnet)\b` |
| BMP-HCP-B04 | Role-only assembly APIs or product conversion performed by BMP | `\b(?:toHcpServer|toTool|toCapability|toResource|registerModule)\s*\(` |
| BMP-HCP-B05 | A copied generated projection or direct dependence on projection internals | `\b(?:HCP_SERVERS|HCP_MAGNETS)\b|sources\.generated\.ts|generate-hcp-sources` |
| BMP-HCP-B06 | Registry/map/list/table/builder/default names for HCP Modules, Sources, Servers, or Magnets | `(?i)\b(?:hcp|magenta_hcp)[_-]?(?:module|source|server|magnet)[_-]?(?:registry|map|list|table|builders?|defaults?)\b` |
| BMP-HCP-B07 | HCP product builder/factory/selector/switch infrastructure in BMP | `(?i)\bhcp[_-]?(?:product[_-]?)?(?:builder|factory|selector|switch)(?:s|_map|_table)?\b` |
| BMP-HCP-B08 | Retired, inverted, or illegal role/subtype names | `\b(?:ModuleHcpServer|CapabilityHcpServer|UniversalMagnet|ProcessToolMagnet|PythonModuleToolMagnet|HcpProcessMagnet|CapabilitySourceMagnet|HcpRequest|HcpResponse|HcpContext|HcpResource)\b` |
| BMP-HCP-B09 | An HCP per-call middleware or Tool-call wrapper | `(?i)\bhcp[_-]?(?:tool[_-]?call[_-]?)?middleware\b|\bHcp(?:ToolCall)?Middleware\b` |
| BMP-HCP-B10 | TypeScript/JavaScript deep imports into `.HCP/`, `_magenta/`, role files, or `HcpClient.ts` | `(?:from\s+|import\s*\()\s*["'][^"']*HarnessComponentProtocol/(?:\.HCP|_magenta|HcpClient\.ts|[^"']+/Hcp(?:Server|Magnet)\.ts)` |
| BMP-HCP-B11 | Python deep imports into HarnessComponentProtocol implementation paths | `(?m)^\s*(?:from|import)\s+(?:Magenta\.)?HarnessComponentProtocol(?:\.|/)` |
| BMP-HCP-B12 | A recreated HCP ownership tree or retired wrapper layer in BMP (path scan) | `(?:^|/)(?:HarnessComponentProtocol|\.HCP)/(?:modules|hcp-client|hcp-contract|hcp-magnet|magnet|contract)(?:/|$)` |
| BMP-HCP-B13 | HCP role files copied into BMP (path scan) | `(?:^|/)Hcp(?:Client|Server|Magnet)\.(?:ts|tsx|js|mjs|cjs|py)$` |

Allowed boundary vocabulary is deliberately narrow: registry values such as
`adapter = "magenta_hcp"`, subject kind `hcp_harness`, and neutral BMP schema
fields that store the opaque resolved sidecar are allowed. This allowance does
not permit BMP to define HCP roles or reconstruct HCP resolution.

The grep rules are a minimum mechanical gate. Review MUST additionally reject a
semantically parallel HCP registry or selector hidden behind neutral names such
as `component_catalog` or `implementation_matrix`; a name change does not cure
the architecture violation.

## C. BMP/HCP Interface Contract

### Boundary shape

The only HCP-specific value that may cross into BMP core is the canonical,
resolved HCP assembly sidecar emitted by the `magenta_hcp` Subject adapter. The
adapter may invoke a stable Magenta public exporter or command and receive its
serialized output. BMP core may validate, hash, persist, compare, and cite that
sidecar. BMP core and the adapter MUST NOT import HCP implementation classes,
instantiate roles, choose Sources, resolve products, or rebuild the assembly.

The sidecar is evidence about an assembly that Magenta already resolved. It is
not a BMP declaration and is never an input registry for resolving a future HCP
assembly.

The historical design handoff that informed this contract is not tracked in
this repository and has no content-addressed locator. It is context, not
authority: do not cite its line numbers, private paths, or mutable copies as
evidence. The table below is the tracked BMP Phase 0 contract. Every listed
field is **required at schema-shape level**; if the pinned exporter or manifest
cannot provide a required value, validation and any dependent claim MUST fail
closed rather than treating the value as equal, empty, or inferred.

| Sidecar field | Phase 0 representation and requirement |
|---|---|
| `module / source / product` | **Required.** Each resolved component row has nonempty Module and Source identities and exactly one tagged product identity/kind: Tool, Capability, or Resource. |
| `slot / requires` | **Required.** `slot` is explicit; `requires` is an array and may be empty, never omitted. |
| `descriptor and settings digest` | **Required.** Two separate canonical digest values with named algorithms; raw secrets or secret-derived plaintext MUST NOT be embedded. |
| `Package provenance` | **Required key; nullable value.** Null/empty is allowed only for a built-in, non-Package component. A Package component requires selector/origin, schema version, content/checksum identity, and resolved local artifact identity without relying on acquisition location. |
| `resolved addresses` | **Required.** Canonically ordered addresses bound to resolved component/product rows. |
| `active tools and capabilities` | **Required.** Two canonically ordered arrays; either may be empty but neither may be omitted. Resources remain represented in component rows. |
| `system-prompt digest` | **Required.** The digest covers deterministic already-resolved prompt composition. Missing prompt evidence makes an HCP harness sidecar nonconformant. |
| `assembly diagnostics` | **Required.** A structured array that may be empty; errors and warnings MUST NOT be discarded. |
| `Magenta/runtime version` | **Required.** Separate Magenta commit/version and actual runtime identity/version fields. |
| `module activation receipt` | **Required.** Every claimed intervention target needs a receipt proving the intended Module/Source/product became active. Missing or negative receipts force `isolation_valid = false`. |
| `state/cache/workspace namespace` | **Required.** Separate explicit namespaces. Missing, reused, or unverified namespaces force `isolation_valid = false`. |

Phase 0 may permit forward-compatible extension fields. If
`canonical_assembly_digest` or `dependency_file_closure` is required by an
experiment, the pinned Magenta exporter must emit it and the manifest must bind
the bytes. BMP MUST NOT synthesize either value by deep-reading Magenta
internals. Missing or untracked source material is a failed isolation gate, not
proof of equality.

## D. Claim Gate Correctness Rules

Every run record reports all six booleans separately. A boolean is true only if
all of its preconditions are positively evidenced; missing or unknown evidence
is false, not "not applicable" and not success.

### 1. `execution_valid`

**Precondition:** input and output contracts are satisfied; the task,
container, and Agent terminate in an accepted state; and Trace, Checkpoint,
usage, status, and required output structures validate. Missing source or output
evidence is a failure, not a default.

**False blocks:** admission of the run to effect estimation or paired
comparison, any claim based on its output, and substitution of a numeric zero
for the invalid run. The raw terminal/failure status and evidence remain in the
bundle.

### 2. `protocol_valid`

**Precondition:** observed execution matches the resolved protocol exactly:
case order and seed, serial/parallel schedule, rollout/candidate count,
memory/state reset, candidate aggregation/selection, timeout/token/cost/wall
clock budgets, and checkpoint save/resume policy. The executed manifest digest
must match the planned resolved manifest digest; an untracked protocol
dimension fails this gate.

**False blocks:** aggregation under the named protocol, comparison with a run
that used that protocol, and every causal or protocol-level claim. The run may
remain as explicitly nonconformant operational evidence.

### 3. `isolation_valid`

**Precondition:** the resolved-manifest diff is confined to the allowed
intervention; activation receipts prove the target component was active;
workspace, cache, memory, state, and environment are isolated; task, verifier,
image, and runner digests match; and resume did not cross an unrecorded backend
or runtime version change. Missing or untracked isolation evidence fails this
gate.

**False blocks:** attribution to the intervention, Module/Source/Capability
claims, paired causal comparison, and promotion of the result as controlled
evidence. Whole-system descriptive output does not repair failed isolation.

### 4. `scoring_valid`

**Precondition:** the adapter resolved the benchmark-native verifier and
scoring semantics; the verifier/metric digest is the pinned one; it executes
successfully on a contract-valid output; it emits structurally valid evidence;
and no verifier/metric/infrastructure error is relabeled as subject failure.
Native benchmark semantics are authoritative, and the pinned verifier or metric
must be executable and persist its evidence.

**False blocks:** publication or aggregation of a numeric score, effect-size or
ranking computation, pass/fail claims, and any downstream claim. It produces a
distinct scoring/verifier failure state, not zero.

### 5. `statistics_valid`

**Precondition:** the design has the required paired repetitions and
counterbalanced order; valid train/validation/test/holdout separation; effect
sizes, intervals, cost, and variance; complete-process comparison for Evolver
and MetaEvolver subjects; and lineage for every candidate including rejected
and invalid candidates. Missing denominator or lineage evidence fails this gate.

**False blocks:** inferential, generalization, superiority, equivalence, and
causal claims. Descriptive statistics may be reported only with an explicit
`statistics_valid = false` qualification.

### 6. `claim_eligible`

The BMP contract reports six booleans and groups them into three gate classes:
Execution, Isolation, and Statistics. To make that decomposition executable,
BMP uses this binding:

```text
execution_gate_class = execution_valid AND protocol_valid AND scoring_valid
isolation_gate_class = isolation_valid
statistics_gate_class = statistics_valid

claim_eligible =
  execution_gate_class AND
  isolation_gate_class AND
  statistics_gate_class
```

Equivalently, `claim_eligible` is the conjunction of all five primitive validity
booleans. It MUST NOT be set independently, overridden manually, inferred from
a positive score, or made true by dropping failed runs. This implements the
rule that all three gate classes must pass before a causally meaningful claim is
generated.

**False blocks:** causal wording, benchmark winner/superiority declarations,
release or adoption decisions presented as evidence-backed, and any report
section labeled as a valid Claim. A noneligible run remains reportable as
failure or descriptive evidence with the failed gates visible.

### Failure preservation rule

Failures MUST retain their distinct raw states (`verified_fail`, `no_output`,
`invalid_output`, `timeout`, `agent_error`, `harness_fault`, `verifier_error`,
`infra_error`, `unsupported`, and any schema-approved extension). They MUST NOT
be collapsed into one zero score, one generic failure, or a filtered successful
subset. If the source taxonomy is unavailable, preserve the raw terminal state
and mark the result invalid rather than guessing.

## E. Runnable Audit Script Specification

The Runtime Builder MUST implement `scripts/audit_hcp_boundary.sh` with the
following behavior:

1. Run with `bash`, `set -uo pipefail`, and resolve the repository root from the
   script location rather than the caller's working directory.
2. Require `rg` with PCRE2 support. A missing tool is an audit failure, not a
   skipped check.
3. Content-scan only implementation/configuration extensions: `*.py`, `*.pyi`,
   `*.ts`, `*.tsx`, `*.js`, `*.mjs`, `*.cjs`, `*.json`, `*.toml`, `*.yaml`,
   and `*.yml`. Scan importable tests too. Exclude `.git/**`, `docs/**`, build
   output, caches, virtual environments, and vendored dependencies.
4. Run one labeled `rg --pcre2 -n` check for each content pattern B01 through
   B11. Collect all matches instead of exiting after the first one.
5. Run `rg --files` through one labeled `rg --pcre2` path check for each of B12
   and B13.
6. Treat every match as a violation. Do not maintain a source-file allowlist for
   forbidden architecture. Negative audit fixtures must encode forbidden
   strings in pieces so the repository audit still scans test code.
7. Print each violation with its rule ID, path, line number when available, and
   matched text. Print a final count.
8. Exit `0` only when the count is zero; exit nonzero for any match, scan error,
   missing scan root, or unavailable PCRE2 support.
9. Be callable locally and in CI as one command, and add a test that injects one
   representative violation for every Section B rule and proves the script
   exits nonzero.

The script is a fast structural gate, not proof of semantic compliance. Review
against Section A and the sidecar-only data flow remains mandatory, especially
for neutral-named registries, selectors, and reconstruction logic that grep
cannot classify reliably.
