"""Fachlich getrennte Routenmodule des Backoffice.

Jedes Modul bringt einen eigenen prefixlosen `APIRouter` mit; die
Komposition zu genau einem `/backoffice`-Router passiert ausschließlich
in `backoffice/app.py`. Kein Routenmodul importiert ein anderes
Routenmodul oder `app` - gemeinsame Abhängigkeiten kommen über
`backoffice.dependencies`, gemeinsame Helfer über `backoffice.auth` bzw.
`backoffice.routes.shared`.
"""
