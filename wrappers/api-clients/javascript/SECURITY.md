# Reporting Security Issues

This SDK is part of a larger project. To report a security issue, please follow the directions at [https://github.com/checkrd/checkrd/blob/main/SECURITY.md](https://github.com/checkrd/checkrd/blob/main/SECURITY.md).

## Reporting Non-SDK Related Security Issues

If you find a security vulnerability in the Checkrd Control Plane API or any other Checkrd service, please email `security@checkrd.io`. We follow a 90-day disclosure window, after which we publish a CVE.

For cryptographic issues (Ed25519 signing, DSSE envelope verification, webhook HMAC), please CC `security@checkrd.io` and reference the relevant section of [`KEY-CUSTODY.md`](https://github.com/checkrd/checkrd/blob/main/KEY-CUSTODY.md). We treat key-custody and signature-verification regressions as P0 incidents.

Thanks for helping make Checkrd safer for everyone.
