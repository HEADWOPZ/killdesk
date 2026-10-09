"""Live, animated, read-only dashboard over desk.db.

`killdesk dashboard` serves one HTML page (no CDN, no build step) plus a JSON
snapshot and a Server-Sent Events stream. It never writes to the book.
"""
