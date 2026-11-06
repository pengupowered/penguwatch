"""The sync engine: keeps the local library in step with the active library driver (drivers.base.LibrarySync).

It owns everything that does not depend on the service: the outbox of neutral operations (review mode, approval,
back-off, delivery in order per title, batching what the driver says may share a request), the pull bookkeeping
(the driver's cursor, staging for review, the titles with unsent changes that a pull must not overwrite, removals,
the events), enrichment from the catalogue chain and the schedule. Drivers talk to their service; nothing here
names one.
"""
