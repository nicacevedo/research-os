-- A replication's execution manifest (docs/ARCHITECTURE_INVARIANTS.md, INV-07).
--
-- The only guard against "the variation did not reach the measurement" was a
-- comparison of whole-file output digests with the primary's. A replication
-- whose program ignored its new seed but stamped a timestamp produced
-- different bytes and the same measurement, and was recorded as an
-- independent REPLICATION with SUPPORTS -- the final adversarial review of
-- 37e8afe reproduced it (H6). Different bytes are not evidence that anything
-- the replication was meant to vary reached the computation.
--
-- So a replication now has a manifest frozen before it runs: the parent
-- experiment and its specification and contract, the code identity (the
-- workspace's base commit and the declared command), the immutable inputs,
-- the environment, the independence variables -- every seed, parameter,
-- composed input or command that differs from the primary's frozen
-- specification -- and the execution identity. It is stored by content hash
-- in the artifact store and named here. Independence is then established
-- only by the computation's own receipt of what it consumed, checked against
-- this manifest; agreement with the primary is assessed separately.
alter table idea_experiments
    add column if not exists execution_manifest_artifact_id text
        references artifacts(artifact_id) on delete set null;
