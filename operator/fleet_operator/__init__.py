"""Fleet Operator: Kopf controllers reconciling the dronekube.io/v1alpha1 CRDs.

Exercised live in the declarative (B) variant of the E0/E1/E2/P2/E4/S1/S3/S4/
U1/U2 scenarios; evidence under results/ (gitignored, local), described in
operator/DEV_SMOKE_TEST.md and in the evidence registry of
docs/PROPOSAL_COMPLETION_CHECKLIST.md. What each CRD field actually does, and
what is only declared, is in docs/CRD_CONTRACT_AUDIT.md -- check it before
citing behaviour from this code: a live PASS on one revision does not
certify every field or the current worktree.
"""
