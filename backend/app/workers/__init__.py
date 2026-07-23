"""Background workers.

Workers own their own database sessions and their own lifetimes. Nothing here is
request-scoped: a backup outlives the HTTP request that asked for it, which is precisely why
the request records *intent* in the database and a worker acts on it later.
"""
