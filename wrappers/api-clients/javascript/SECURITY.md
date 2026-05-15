# Reporting Security Issues

**Do not open a public GitHub issue for a security vulnerability.**

Email `security@checkrd.io`. A machine-readable `security.txt` (RFC
9116) is published at `https://checkrd.io/.well-known/security.txt`.
Reach out by email if you need an encrypted channel and we will
arrange one.

We acknowledge within 2 business days, triage within 5, and follow a
90-day disclosure window after which we publish a CVE and GitHub
Security Advisory. Reporter credit is included unless you ask
otherwise.

For cryptographic issues (Ed25519 signing, DSSE envelope verification,
webhook HMAC), please mention "crypto" in the subject — we treat
key-custody and signature-verification regressions as P0.

Thanks for helping make Checkrd safer for everyone.
