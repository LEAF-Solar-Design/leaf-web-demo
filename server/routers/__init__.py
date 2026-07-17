"""FastAPI routers, one per concern. Sibling sessions edit their OWN router file:

- routers/session.py  -> auth0-identity-signup
- routers/tools.py    -> dynamic-tool-loader
- routers/jobs.py     -> async-broker-catalog-envelopes (this session)
- routers/author.py   -> (this session)
- routers/projects.py -> project-job-schema (adds it)
"""
