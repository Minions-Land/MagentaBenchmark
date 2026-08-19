# NatureBench Candidate Blocker

`Minions-Land/AOSEBench-NatureBench@4b512029f3ad37746502ce377e4fcc2027fd46db`
is represented in the historical ledger only through its approved
`typed-results-only` projection. Its tracked source descriptor declares
`visibility=private`, `license_status=not-detected`, and no redistributable
raw report or artifact closure.

Therefore this repository must not materialize NatureBench result bytes from
that candidate yet. A future adapter needs a public or explicitly authorized
immutable artifact source, a license/provenance decision covering those exact
bytes, and declarations for every report/index/manifest/evidence/artifact
digest before it can use the materializer. The public fixture beside this
blocker validates the contract without implying that such closure exists.

The existing eight NatureBench declaration/evaluated-summary records remain
in `imports/aosebench-naturebench-4b51202/` as `typed-results-only`, always
`claim_eligible=false`. The concrete next action is for `PoorOtterBob` to
authorize an immutable byte source and redistribution scope, after which a
NatureBench-specific provider adapter can populate this generic manifest and
run the original evaluator without modifying the NatureBench repository.
