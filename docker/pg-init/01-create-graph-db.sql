-- Create the cortex graph database on the `pg` service. The meta database
-- (mnemoseed_meta) is created by POSTGRES_DB; the graph database must exist too
-- because the graph and meta drivers keep separate schema_version tables (they
-- share the `pg` instance but never the migrations bookkeeping).
--
-- Runs once during first initialization of the empty data volume, before the
-- server accepts connections.

CREATE DATABASE mnemoseed_graph;
